import atexit
import os
import shutil
import tempfile
from pathlib import Path

test_data_dir = Path(tempfile.mkdtemp(prefix="agent-farm-tests-"))
atexit.register(shutil.rmtree, test_data_dir, ignore_errors=True)

os.environ["DATABASE_URL"] = f"sqlite:///{(test_data_dir / 'agent_farm.db').as_posix()}"
os.environ["CREDENTIAL_PROVIDER"] = "mock"
os.environ["SCHEDULER_INTERVAL_SECONDS"] = "3600"

from fastapi.testclient import TestClient

from backend.app.main import app


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
