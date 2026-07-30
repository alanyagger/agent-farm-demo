import json

import httpx
import pytest

from backend.app.admission import CmccAdmissionVerifier
from backend.app.config import Settings
from backend.app.credentials import CmccCrypto


APP_KEY = "0123456789abcdef0123456789abcdef"


def admission_settings(mode: str = "demo") -> Settings:
    return Settings(
        _env_file=None,
        credential_provider="mock",
        cmcc_base_url="https://vctest.cmccsign.com",
        cmcc_app_id="test-app-id",
        cmcc_app_key=APP_KEY,
        cmcc_client_secret=APP_KEY,
        cmcc_agent_template_id="test-template",
        cmcc_demo_phone="19812345092",
        cmcc_admission_mode=mode,
        cmcc_admission_agent_mappings=(
            "agent-sprout:agent1,agent-nova:agent2,agent-orbit:agent3"
        ),
        cmcc_admission_agent_id="agent-sprout",
        cmcc_admission_agent_name="agent1",
        cmcc_admission_timeout_seconds=1,
        cmcc_admission_cache_seconds=10,
    )


def test_demo_admission_allows_empty_result_and_uses_cache() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        body = json.loads(request.content)
        assert request.url.path.endswith("/vc/listByTemplateId")
        assert request.headers["appId"] == "test-app-id"
        assert request.headers["signValue"] == CmccCrypto.sign(body, APP_KEY)
        assert "19812345092" not in request.content.decode("utf-8")
        return httpx.Response(200, json={"code": 0, "data": []})

    verifier = CmccAdmissionVerifier(
        admission_settings(),
        transport=httpx.MockTransport(handler),
    )
    first = verifier.verify("agent-sprout")
    second = verifier.verify("agent-sprout")
    forced = verifier.verify("agent-sprout", force=True)

    assert first.allowed is True
    assert first.mode == "DEMO_CONNECTED"
    assert first.upstream_status == "NOT_FOUND"
    assert first.cached is False
    assert second.cached is True
    assert forced.cached is False
    assert calls == 2


def test_real_active_record_has_priority_over_demo_fallback() -> None:
    encrypted_name = CmccCrypto.encrypt_aes_ecb(
        "agent1",
        APP_KEY,
        key_mode="md5",
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": [
                    {
                        "vcRecordId": "record-1",
                        "vcStatus": 2,
                        "fieldList": [
                            {
                                "fieldName": "agentName",
                                "fieldValue": encrypted_name,
                            }
                        ],
                    }
                ],
            },
        )

    verifier = CmccAdmissionVerifier(
        admission_settings(),
        transport=httpx.MockTransport(handler),
    )
    decision = verifier.verify("agent-sprout")

    assert decision.allowed is True
    assert decision.mode == "REAL_ACTIVE"
    assert decision.upstream_status == "ACTIVE"
    assert decision.record_count == 1


def test_strict_mode_denies_empty_result() -> None:
    verifier = CmccAdmissionVerifier(
        admission_settings("strict"),
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"code": 0, "data": []})
        ),
    )

    decision = verifier.verify("agent-sprout")

    assert decision.allowed is False
    assert decision.mode == "DENIED"
    assert decision.upstream_status == "NOT_FOUND"


@pytest.mark.parametrize(
    ("response", "expected_status"),
    [
        (httpx.Response(503), "HTTP_ERROR"),
        (
            httpx.Response(200, json={"code": 99999, "msg": "验签失败"}),
            "API_ERROR",
        ),
        (httpx.Response(200, json=[]), "INVALID_RESPONSE"),
    ],
)
def test_upstream_failures_always_deny(
    response: httpx.Response,
    expected_status: str,
) -> None:
    verifier = CmccAdmissionVerifier(
        admission_settings(),
        transport=httpx.MockTransport(lambda _: response),
    )

    decision = verifier.verify("agent-sprout")

    assert decision.allowed is False
    assert decision.mode == "DENIED"
    assert decision.upstream_status == expected_status


def test_network_failure_denies_without_exposing_secrets() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"failed {APP_KEY}", request=request)

    verifier = CmccAdmissionVerifier(
        admission_settings(),
        transport=httpx.MockTransport(handler),
    )
    decision = verifier.verify("agent-sprout")

    assert decision.allowed is False
    assert decision.upstream_status == "NETWORK_ERROR"
    assert APP_KEY not in decision.message


def test_second_mapped_agent_uses_its_own_identity_and_cache() -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": [
                    {
                        "vcRecordId": "record-agent2",
                        "vcStatus": 2,
                        "agentName": "agent2",
                    }
                ],
            },
        )

    verifier = CmccAdmissionVerifier(
        admission_settings(),
        transport=httpx.MockTransport(handler),
    )
    first = verifier.verify("agent-nova")
    second = verifier.verify("agent-nova")

    assert first.allowed is True
    assert first.mode == "REAL_ACTIVE"
    assert first.external_agent_name == "agent2"
    assert second.cached is True
    assert calls == 1


def test_third_mapped_agent_uses_agent3_identity() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": [
                    {
                        "vcRecordId": "record-agent3",
                        "vcStatus": 2,
                        "agentName": "agent3",
                    }
                ],
            },
        )

    verifier = CmccAdmissionVerifier(
        admission_settings(),
        transport=httpx.MockTransport(handler),
    )
    decision = verifier.verify("agent-orbit")

    assert decision.allowed is True
    assert decision.mode == "REAL_ACTIVE"
    assert decision.external_agent_name == "agent3"


def test_legacy_single_mapping_enables_all_demo_agents() -> None:
    config = admission_settings()
    config.cmcc_admission_agent_mappings = ""
    verifier = CmccAdmissionVerifier(config)

    assert verifier.agent_mappings == {
        "agent-sprout": "agent1",
        "agent-nova": "agent2",
        "agent-orbit": "agent3",
    }


def test_unmapped_agent_uses_local_gate_without_external_call() -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    verifier = CmccAdmissionVerifier(
        admission_settings(),
        transport=httpx.MockTransport(handler),
    )
    decision = verifier.verify("agent-unmapped")

    assert decision.allowed is True
    assert decision.mode == "LOCAL"
    assert decision.external_agent_name == ""
    assert calls == 0
