import atexit
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

test_data_dir = Path(tempfile.mkdtemp(prefix="agent-farm-tests-"))
atexit.register(shutil.rmtree, test_data_dir, ignore_errors=True)

os.environ["DATABASE_URL"] = f"sqlite:///{(test_data_dir / 'agent_farm.db').as_posix()}"
os.environ["CREDENTIAL_PROVIDER"] = "mock"
os.environ["SCHEDULER_INTERVAL_SECONDS"] = "3600"

from fastapi.testclient import TestClient

import backend.app.agent_runtime as agent_runtime_module
import backend.app.main as main_module
from backend.app.config import settings
from backend.app.database import SessionLocal
from backend.app.main import agent_runtime, app
from backend.app.model_provider import FakeModelProvider, ModelProviderError
from backend.app.models import Plot


def test_credential_gate_and_full_demo_flow() -> None:
    with TestClient(app) as client:
        assert client.post("/api/demo/reset").status_code == 200

        missing = client.get("/api/owners/owner-chen/dashboard").json()
        assert missing["credential"] is None

        blocked = client.post("/api/agents/agent-nova/run").json()
        assert blocked["dashboard"]["actions"][0]["status"] == "BLOCKED"
        assert blocked["dashboard"]["actions"][0]["credentialStatus"] == "MISSING"

        applied = client.post(
            "/api/owners/owner-chen/agent/credential/apply"
        ).json()
        assert applied["credential"]["status"] == "ACTIVE"
        assert applied["credential"]["aic"].startswith("AIC-CM-")
        assert len(applied["credentialEvents"]) == 4

        allowed = client.post("/api/agents/agent-nova/run").json()
        assert allowed["actionCount"] >= 1
        assert any(
            action["status"] == "SUCCESS"
            for action in allowed["dashboard"]["actions"]
        )

        revoked = client.post(
            "/api/agents/agent-nova/credential/simulate-status",
            json={"status": "REVOKED"},
        ).json()
        assert revoked["credential"]["status"] == "REVOKED"

        blocked_again = client.post("/api/agents/agent-nova/run").json()
        assert blocked_again["dashboard"]["actions"][0]["status"] == "BLOCKED"
        assert blocked_again["dashboard"]["actions"][0]["credentialStatus"] == "REVOKED"


def test_seeded_agents_and_persistent_farm_state() -> None:
    with TestClient(app) as client:
        client.post("/api/demo/reset")
        owners = client.get("/api/owners").json()
        assert len(owners) == 3
        assert sum(
            owner["agent"]["credentialStatus"] == "ACTIVE" for owner in owners
        ) == 2

        first = client.get("/api/owners/owner-lin/dashboard").json()
        before_coins = first["farm"]["coins"]
        response = client.post("/api/agents/agent-sprout/run")
        assert response.status_code == 200
        after = response.json()["dashboard"]
        assert len(after["farm"]["plots"]) == 6
        assert after["farm"]["coins"] >= before_coins


def test_llm_runtime_uses_allow_listed_skills_and_records_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_model = FakeModelProvider(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "inspect_my_farm", "args": {}, "id": "call-observe"}
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "plant_crop",
                        "args": {
                            "plot_id": "farm-1-plot-3",
                            "crop_type": "CARROT",
                        },
                        "id": "call-plant",
                    }
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "steal_crop",
                        "args": {"plot_id": "farm-2-plot-1"},
                        "id": "call-steal",
                    }
                ],
            ),
            AIMessage(content="已完成农场观察、补种和一次合规社交采摘。"),
        ]
    )
    monkeypatch.setattr(settings, "agent_runtime_mode", "llm")
    monkeypatch.setattr(settings, "agent_max_tool_rounds", 4)
    monkeypatch.setattr(
        agent_runtime_module, "build_model_provider", lambda: fake_model
    )

    with TestClient(app) as client:
        client.post("/api/demo/reset")
        response = client.post("/api/agents/agent-sprout/run")
        assert response.status_code == 200
        payload = response.json()
        assert payload["run"]["runtimeMode"] == "LLM"
        assert payload["run"]["provider"] == "deepseek"
        assert payload["run"]["status"] == "SUCCESS"

        actions = payload["dashboard"]["actions"]
        action_types = {action["actionType"] for action in actions}
        assert {"MODEL_DECISION", "PLANT", "STEAL"}.issubset(action_types)
        assert any(
            action["executionMode"] == "LLM" and action["source"] == "LLM_MANUAL"
            for action in actions
        )
        latest_run = payload["dashboard"]["recentRuns"][0]
        assert latest_run["toolCallCount"] == 3
        assert "社交采摘" in latest_run["decisionSummary"]


def test_llm_runtime_blocks_before_model_and_never_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_model() -> FakeModelProvider:
        raise AssertionError("Credential-blocked agent must not call a model")

    monkeypatch.setattr(settings, "agent_runtime_mode", "llm")
    monkeypatch.setattr(agent_runtime_module, "build_model_provider", unexpected_model)

    with TestClient(app) as client:
        client.post("/api/demo/reset")
        response = client.post("/api/agents/agent-nova/run")
        assert response.status_code == 200
        payload = response.json()
        assert payload["run"]["status"] == "BLOCKED"
        assert payload["dashboard"]["actions"][0]["actionType"] == "ACCESS"
        assert payload["dashboard"]["actions"][0]["status"] == "BLOCKED"


def test_llm_configuration_failure_is_audited_without_rule_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "agent_runtime_mode", "llm")
    monkeypatch.setattr(
        agent_runtime_module,
        "build_model_provider",
        lambda: (_ for _ in ()).throw(ModelProviderError("test API unavailable")),
    )

    with TestClient(app) as client:
        client.post("/api/demo/reset")
        response = client.post("/api/agents/agent-sprout/run")
        assert response.status_code == 200
        payload = response.json()
        assert payload["run"]["status"] == "FAILED"
        actions = payload["dashboard"]["actions"]
        assert actions[0]["actionType"] == "AGENT_ERROR"
        assert not any(
            action["actionType"] in {"PLANT", "HARVEST", "STEAL"}
            and action["source"] == "LLM_MANUAL"
            for action in actions
        )


def test_slow_scheduled_llm_does_not_block_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = threading.Event()
    release = threading.Event()

    def slow_scheduled_run(_db, _agent_id: str, source: str = "MANUAL"):
        assert source == "SCHEDULER"
        started.set()
        release.wait(timeout=2)

    monkeypatch.setattr(settings, "scheduler_interval_seconds", 0.01)
    monkeypatch.setattr(main_module.agent_runtime, "run", slow_scheduled_run)

    try:
        with TestClient(app) as client:
            client.post("/api/demo/reset")
            assert started.wait(timeout=2)

            started_at = time.perf_counter()
            response = client.get("/health")
            elapsed = time.perf_counter() - started_at

            assert response.status_code == 200
            assert elapsed < 1.0
    finally:
        release.set()


def test_llm_skill_progress_is_committed_before_turn_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    waiting_after_skill = threading.Event()
    release_model = threading.Event()

    class ProgressiveProvider:
        name = "test-progressive"

        def __init__(self) -> None:
            self.calls = 0

        def bind_tools(self, _tools):
            provider = self

            class BoundModel:
                def invoke(self, _messages):
                    provider.calls += 1
                    if provider.calls == 1:
                        return AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "name": "plant_crop",
                                    "args": {
                                        "plot_id": "farm-1-plot-3",
                                        "crop_type": "CARROT",
                                    },
                                    "id": "call-progressive-plant",
                                }
                            ],
                        )
                    waiting_after_skill.set()
                    release_model.wait(timeout=2)
                    return AIMessage(content="种植完成。")

            return BoundModel()

    monkeypatch.setattr(settings, "agent_runtime_mode", "llm")
    monkeypatch.setattr(
        agent_runtime_module,
        "build_model_provider",
        lambda: ProgressiveProvider(),
    )

    with TestClient(app) as client:
        client.post("/api/demo/reset")
        error: list[Exception] = []

        def run_agent_turn() -> None:
            try:
                with SessionLocal() as db:
                    agent_runtime.run(db, "agent-sprout", source="MANUAL")
            except Exception as exc:
                error.append(exc)

        worker = threading.Thread(target=run_agent_turn)
        worker.start()
        try:
            assert waiting_after_skill.wait(timeout=2)
            with SessionLocal() as db:
                plot = db.get(Plot, "farm-1-plot-3")
                assert plot is not None
                assert plot.crop_type == "CARROT"
        finally:
            release_model.set()
            worker.join(timeout=3)

        assert not worker.is_alive()
        assert not error
