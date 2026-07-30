import atexit
import json
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path

import pytest
import httpx
from langchain_core.messages import AIMessage

test_data_dir = Path(tempfile.mkdtemp(prefix="agent-farm-tests-"))
atexit.register(shutil.rmtree, test_data_dir, ignore_errors=True)

os.environ["DATABASE_URL"] = f"sqlite:///{(test_data_dir / 'agent_farm.db').as_posix()}"
os.environ["CREDENTIAL_PROVIDER"] = "mock"
os.environ["SCHEDULER_INTERVAL_SECONDS"] = "3600"
os.environ["CMCC_ADMISSION_MODE"] = "off"
os.environ["AGENT_RUNTIME_MODE"] = "rules"

from fastapi.testclient import TestClient

import backend.app.agent_runtime as agent_runtime_module
import backend.app.main as main_module
from backend.app.config import settings
from backend.app.database import SessionLocal
from backend.app.main import agent_runtime, app
from backend.app.model_provider import FakeModelProvider, ModelProviderError
from backend.app.models import Agent, Plot
from backend.app.seed import ensure_seeded
from backend.app.credentials import CmccCrypto
from backend.tools.cmcc_credential_probe import sanitize_records
from backend.tools.cmcc_agent_issue import (
    CmccBusinessError,
    IssueConfig,
    create_and_issue_agent,
)


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
        assert all(
            owner["agent"]["automationEnabled"] is False for owner in owners
        )

        with SessionLocal() as db:
            for agent_id in ("agent-sprout", "agent-orbit", "agent-nova"):
                db.get(Agent, agent_id).automation_enabled = True
            db.commit()
            ensure_seeded(db)
            assert db.get(Agent, "agent-sprout").automation_enabled is False
            assert db.get(Agent, "agent-orbit").automation_enabled is False
            assert db.get(Agent, "agent-nova").automation_enabled is True

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
            client.patch(
                "/api/agents/agent-sprout/automation",
                json={"enabled": True},
            )
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


def test_cmcc_demo_admission_is_used_by_game_gate_and_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"code": 0, "data": []})

    monkeypatch.setattr(settings, "cmcc_admission_mode", "demo")
    monkeypatch.setattr(
        settings,
        "cmcc_admission_agent_mappings",
        "agent-sprout:agent1,agent-nova:agent2,agent-orbit:agent3",
    )
    monkeypatch.setattr(settings, "cmcc_admission_agent_id", "agent-sprout")
    monkeypatch.setattr(settings, "cmcc_admission_agent_name", "agent1")
    monkeypatch.setattr(settings, "cmcc_base_url", "https://vctest.cmccsign.com")
    monkeypatch.setattr(settings, "cmcc_app_id", "test-app-id")
    monkeypatch.setattr(
        settings,
        "cmcc_app_key",
        "0123456789abcdef0123456789abcdef",
    )
    monkeypatch.setattr(settings, "cmcc_demo_phone", "19812345092")
    monkeypatch.setattr(settings, "cmcc_agent_template_id", "test-template")
    monkeypatch.setattr(settings, "cmcc_admission_cache_seconds", 10)
    monkeypatch.setattr(
        main_module.admission_verifier,
        "transport",
        httpx.MockTransport(handler),
    )
    main_module.admission_verifier.clear()

    with TestClient(app) as client:
        assert client.post("/api/demo/reset").status_code == 200
        pending = client.get("/api/agents/agent-sprout/admission").json()
        assert pending["mode"] == "DENIED"
        assert pending["upstreamStatus"] == "NOT_CHECKED"

        execution = client.post("/api/agents/agent-sprout/run")
        assert execution.status_code == 200
        payload = execution.json()
        assert payload["actionCount"] > 0
        assert all(
            action["admissionMode"] == "DEMO_CONNECTED"
            for action in payload["dashboard"]["actions"]
            if action["agentId"] == "agent-sprout"
        )

        cached = client.get("/api/agents/agent-sprout/admission").json()
        assert cached["allowed"] is True
        assert cached["mode"] == "DEMO_CONNECTED"
        assert cached["cached"] is True
        assert calls == 1

        refreshed = client.post(
            "/api/agents/agent-sprout/admission/verify"
        ).json()
        assert refreshed["allowed"] is True
        assert refreshed["cached"] is False
        assert calls == 2

        nova_pending = client.get(
            "/api/agents/agent-nova/admission"
        ).json()
        assert nova_pending["allowed"] is False
        assert nova_pending["mode"] == "DENIED"
        assert nova_pending["upstreamStatus"] == "LOCAL_MISSING"
        assert nova_pending["externalAgentName"] == "agent2"
        assert calls == 2

        applied = client.post(
            "/api/owners/owner-chen/agent/credential/apply"
        ).json()
        assert applied["credential"]["status"] == "ACTIVE"
        assert applied["agent"]["automationEnabled"] is True
        assert calls == 3

        nova_admission = client.get(
            "/api/agents/agent-nova/admission"
        ).json()
        assert nova_admission["allowed"] is True
        assert nova_admission["mode"] == "DEMO_CONNECTED"
        assert nova_admission["externalAgentName"] == "agent2"

        nova_execution = client.post("/api/agents/agent-nova/run").json()
        assert nova_execution["actionCount"] > 0
        assert all(
            action["admissionMode"] == "DEMO_CONNECTED"
            for action in nova_execution["dashboard"]["actions"]
            if action["agentId"] == "agent-nova"
            and action["status"] == "SUCCESS"
        )
        assert calls == 3

        orbit_pending = client.get(
            "/api/agents/agent-orbit/admission"
        ).json()
        assert orbit_pending["allowed"] is False
        assert orbit_pending["mode"] == "DENIED"
        assert orbit_pending["upstreamStatus"] == "NOT_CHECKED"
        assert orbit_pending["externalAgentName"] == "agent3"

        orbit_admission = client.post(
            "/api/agents/agent-orbit/admission/verify"
        ).json()
        assert orbit_admission["allowed"] is True
        assert orbit_admission["mode"] == "DEMO_CONNECTED"
        assert orbit_admission["externalAgentName"] == "agent3"
        assert calls == 4

    main_module.admission_verifier.clear()


def test_cmcc_crypto_round_trip_and_probe_output_is_masked() -> None:
    app_key = "diagnostic-test-key"
    phone = "19812345092"
    agent_did = "did:cmagent:AI202607271643176Z2RRX5C"
    record_id = "987654321012345678"
    encrypted_phone = CmccCrypto.encrypt_phone_ecb(phone, app_key)
    encrypted_name = CmccCrypto.encrypt_phone_ecb("agent1", app_key)
    encrypted_did = CmccCrypto.encrypt_phone_ecb(agent_did, app_key)

    assert CmccCrypto.decrypt_aes_ecb(encrypted_phone, app_key) == phone
    direct_key = "1234567890ABCDEF1234567890ABCDEF"
    direct_encrypted = CmccCrypto.encrypt_aes_ecb(phone, direct_key, "direct")
    assert CmccCrypto.decrypt_aes_ecb(
        direct_encrypted,
        direct_key,
        "direct",
    ) == phone

    sanitized = sanitize_records(
        [
            {
                "vcRecordId": record_id,
                "vcStatus": 2,
                "issueTime": "2026-07-27 16:43:00",
                "fieldList": [
                    {"fieldName": "phoneNo", "fieldValue": encrypted_phone},
                    {"fieldName": "agentName", "fieldValue": encrypted_name},
                    {"fieldName": "agentDid", "fieldValue": encrypted_did},
                ],
            }
        ],
        app_key,
        "agent1",
    )

    rendered = str(sanitized)
    assert sanitized[0]["matchesAgentName"] is True
    assert sanitized[0]["status"] == "ACTIVE/生效中"
    assert phone not in rendered
    assert agent_did not in rendered
    assert record_id not in rendered
    assert "198****5092" in rendered


def test_cmcc_agent_create_and_issue_writes_recoverable_receipt() -> None:
    config = IssueConfig(
        base_url="https://vctest.cmccsign.com",
        app_id="test-app-id",
        app_key="0123456789abcdef0123456789abcdef",
        client_secret="0123456789abcdef0123456789abcdef",
        phone="19812345092",
        template_id="test-template",
        claw_id="agent1",
        agent_name="agent1",
        agent_description="test",
        platform_name="farm",
        timeout_seconds=2,
    )
    requested_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        body = json.loads(request.content)
        assert request.headers["appId"] == config.app_id
        assert request.headers["signValue"] == CmccCrypto.sign(
            body,
            config.client_secret,
        )
        assert config.phone not in request.content.decode("utf-8")
        if request.url.path.endswith("/create"):
            assert body["agentType"] == "1"
            assert (
                CmccCrypto.decrypt_aes_ecb(
                    body["phoneNo"],
                    config.app_key,
                    key_mode="direct",
                )
                == config.phone
            )
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "aic": "did:cmagent:test-agent1",
                        "vcRecordId": "create-record-1",
                    },
                },
            )
        assert body["agentType"] == 0
        assert body["issueMode"] == 1
        assert (
            CmccCrypto.decrypt_aes_ecb(
                body["phoneNo"],
                config.app_key,
                key_mode="md5",
            )
            == config.phone
        )
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "vcRecordId": 123456789,
                    "publicKey": "04ABCDEF",
                },
            },
        )

    receipt_path = test_data_dir / "cmcc-agent1-success.json"
    with httpx.Client(
        base_url=config.base_url,
        transport=httpx.MockTransport(handler),
    ) as client:
        receipt = create_and_issue_agent(config, receipt_path, client)

    assert requested_paths == [
        "/api/cmvc-tocp-server/open-api/vc/agent/create",
        "/api/cmvc-tocp-server/open-api/vc/agent/issue",
    ]
    assert receipt["status"] == "ISSUED"
    assert receipt["vcRecordId"] == "123456789"
    persisted = receipt_path.read_text(encoding="utf-8")
    assert config.phone not in persisted
    assert config.app_key not in persisted


def test_cmcc_agent_issue_failure_keeps_created_identity() -> None:
    config = IssueConfig(
        base_url="https://vctest.cmccsign.com",
        app_id="test-app-id",
        app_key="0123456789abcdef0123456789abcdef",
        client_secret="0123456789abcdef0123456789abcdef",
        phone="19812345092",
        template_id="test-template",
        claw_id="agent1-recovery",
        agent_name="agent1",
        agent_description="test",
        platform_name="farm",
        timeout_seconds=2,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/create"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "aic": "did:cmagent:recoverable",
                        "vcRecordId": "create-record-recovery",
                    },
                },
            )
        return httpx.Response(503)

    receipt_path = test_data_dir / "cmcc-agent1-recovery.json"
    with httpx.Client(
        base_url=config.base_url,
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(httpx.HTTPStatusError):
            create_and_issue_agent(config, receipt_path, client)

    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["status"] == "ISSUE_UNCERTAIN"
    assert receipt["aic"] == "did:cmagent:recoverable"
    assert receipt["createVcRecordId"] == "create-record-recovery"


def test_cmcc_agent_create_business_failure_is_rejected() -> None:
    config = IssueConfig(
        base_url="https://vctest.cmccsign.com",
        app_id="test-app-id",
        app_key="0123456789abcdef0123456789abcdef",
        client_secret="0123456789abcdef0123456789abcdef",
        phone="19812345092",
        template_id="test-template",
        claw_id="agent1-rejected",
        agent_name="agent1",
        agent_description="test",
        platform_name="farm",
        timeout_seconds=2,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"code": 99999, "msg": "创建智能体失败"},
        )

    receipt_path = test_data_dir / "cmcc-agent1-rejected.json"
    with httpx.Client(
        base_url=config.base_url,
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(CmccBusinessError):
            create_and_issue_agent(config, receipt_path, client)

    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["status"] == "CREATE_REJECTED"
    assert receipt["aic"] is None
    assert receipt["vcRecordId"] is None
