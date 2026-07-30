from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from backend.app.config import settings
from backend.app.credentials import CmccCrypto
from backend.tools.cmcc_credential_probe import _mask_identifier


CREATE_PATH = "/api/cmvc-tocp-server/open-api/vc/agent/create"
ISSUE_PATH = "/api/cmvc-tocp-server/open-api/vc/agent/issue"
TEST_HOST = "vctest.cmccsign.com"


class CmccBusinessError(RuntimeError):
    def __init__(self, code: Any, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"CMCC code={code}: {message}")


@dataclass(frozen=True)
class IssueConfig:
    base_url: str
    app_id: str
    app_key: str
    client_secret: str
    phone: str
    template_id: str
    claw_id: str
    agent_name: str
    agent_description: str
    platform_name: str
    timeout_seconds: float


def _base_body() -> dict[str, Any]:
    return {
        "nonce": secrets.token_hex(16),
        "timestamp": int(datetime.now(UTC).timestamp() * 1000),
    }


def _post(
    client: httpx.Client,
    path: str,
    body: dict[str, Any],
    app_id: str,
    secret: str,
) -> dict[str, Any]:
    response = client.post(
        path,
        json=body,
        headers={
            "appId": app_id,
            "signValue": CmccCrypto.sign(body, secret),
        },
    )
    response.raise_for_status()
    payload = response.json()
    if str(payload.get("code")) != "0":
        message = str(payload.get("msg") or "CMCC API returned a failure")[:240]
        raise CmccBusinessError(payload.get("code"), message)
    data = payload.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("CMCC response did not contain an object data field")
    return data


def _write_receipt(path: Path, receipt: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _safe_error(exc: Exception, config: IssueConfig) -> str:
    message = f"{type(exc).__name__}: {exc}".replace("\n", " ")
    for sensitive in (
        config.app_id,
        config.app_key,
        config.client_secret,
        config.phone,
    ):
        if sensitive:
            message = message.replace(sensitive, "[redacted]")
    return message[:300]


def create_and_issue_agent(
    config: IssueConfig,
    receipt_path: Path,
    client: httpx.Client,
) -> dict[str, Any]:
    if receipt_path.exists():
        raise FileExistsError(
            f"Receipt already exists: {receipt_path}. Refusing duplicate creation."
        )

    now = datetime.now(UTC).isoformat()
    receipt: dict[str, Any] = {
        "schemaVersion": 1,
        "environment": urlparse(config.base_url).hostname,
        "templateId": config.template_id,
        "agentName": config.agent_name,
        "clawId": config.claw_id,
        "status": "CREATE_REQUESTING",
        "startedAt": now,
        "updatedAt": now,
        "aic": None,
        "createVcRecordId": None,
        "vcRecordId": None,
        "publicKey": None,
        "error": None,
    }
    _write_receipt(receipt_path, receipt)

    create_phone_no = CmccCrypto.encrypt_aes_ecb(
        config.phone,
        config.app_key,
        key_mode="direct",
    )
    create_body = {
        **_base_body(),
        "phoneNo": create_phone_no,
        "templateId": config.template_id,
        "clawId": config.claw_id,
        "registerTime": datetime.now(timezone(timedelta(hours=8))).strftime(
            "%Y-%m-%d %H:%M:%S"
        ),
        "agentName": config.agent_name,
        "agentDescription": config.agent_description,
        "platformName": config.platform_name,
        "agentType": "1",
    }

    try:
        created = _post(
            client,
            CREATE_PATH,
            create_body,
            config.app_id,
            config.client_secret,
        )
        aic = str(created["aic"])
        create_record_id = str(created["vcRecordId"])
    except CmccBusinessError as exc:
        receipt.update(
            {
                "status": "CREATE_REJECTED",
                "updatedAt": datetime.now(UTC).isoformat(),
                "error": _safe_error(exc, config),
            }
        )
        _write_receipt(receipt_path, receipt)
        raise
    except Exception as exc:
        receipt.update(
            {
                "status": "CREATE_UNCERTAIN",
                "updatedAt": datetime.now(UTC).isoformat(),
                "error": _safe_error(exc, config),
            }
        )
        _write_receipt(receipt_path, receipt)
        raise

    receipt.update(
        {
            "status": "CREATED",
            "updatedAt": datetime.now(UTC).isoformat(),
            "aic": aic,
            "createVcRecordId": create_record_id,
        }
    )
    _write_receipt(receipt_path, receipt)

    issue_phone_no = CmccCrypto.encrypt_phone_ecb(config.phone, config.app_key)
    issue_body = {
        **_base_body(),
        "phoneNo": issue_phone_no,
        "templateId": config.template_id,
        "aic": aic,
        "issueMode": 1,
        "agentName": config.agent_name,
        "agentDescription": config.agent_description,
        "agentType": 0,
    }
    receipt.update(
        {
            "status": "ISSUE_REQUESTING",
            "updatedAt": datetime.now(UTC).isoformat(),
        }
    )
    _write_receipt(receipt_path, receipt)

    try:
        issued = _post(
            client,
            ISSUE_PATH,
            issue_body,
            config.app_id,
            config.client_secret,
        )
        issue_record_id = str(issued.get("vcRecordId") or create_record_id)
        public_key = str(issued.get("publicKey") or "")
    except CmccBusinessError as exc:
        receipt.update(
            {
                "status": "ISSUE_REJECTED",
                "updatedAt": datetime.now(UTC).isoformat(),
                "error": _safe_error(exc, config),
            }
        )
        _write_receipt(receipt_path, receipt)
        raise
    except Exception as exc:
        receipt.update(
            {
                "status": "ISSUE_UNCERTAIN",
                "updatedAt": datetime.now(UTC).isoformat(),
                "error": _safe_error(exc, config),
            }
        )
        _write_receipt(receipt_path, receipt)
        raise

    receipt.update(
        {
            "status": "ISSUED",
            "updatedAt": datetime.now(UTC).isoformat(),
            "vcRecordId": issue_record_id,
            "publicKey": public_key,
            "error": None,
        }
    )
    _write_receipt(receipt_path, receipt)
    return receipt


def _build_config(args: argparse.Namespace) -> IssueConfig:
    base_url = (args.base_url or settings.cmcc_base_url).rstrip("/")
    template_id = args.template_id or settings.cmcc_agent_template_id
    required = {
        "CMCC_BASE_URL/--base-url": base_url,
        "CMCC_APP_ID": settings.cmcc_app_id,
        "CMCC_APP_KEY": settings.cmcc_app_key,
        "CMCC_CLIENT_SECRET": settings.cmcc_client_secret,
        "CMCC_DEMO_PHONE": settings.cmcc_demo_phone,
        "CMCC_AGENT_TEMPLATE_ID/--template-id": template_id,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise ValueError(f"Missing configuration: {', '.join(missing)}")
    hostname = (urlparse(base_url).hostname or "").lower()
    if urlparse(base_url).scheme != "https" or hostname != TEST_HOST:
        raise ValueError(f"Only the CMCC test host {TEST_HOST} is allowed")
    if not args.confirm_create_and_issue:
        raise ValueError("Pass --confirm-create-and-issue to authorize this operation")
    return IssueConfig(
        base_url=base_url,
        app_id=settings.cmcc_app_id,
        app_key=settings.cmcc_app_key,
        client_secret=settings.cmcc_client_secret,
        phone=settings.cmcc_demo_phone,
        template_id=template_id,
        claw_id=args.claw_id,
        agent_name=args.agent_name,
        agent_description=args.description,
        platform_name=args.platform_name,
        timeout_seconds=args.timeout,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Create and issue one platform-hosted CMCC test Agent credential. "
            "A durable local receipt prevents accidental duplicate creation."
        )
    )
    parser.add_argument("--base-url", default="")
    parser.add_argument("--template-id", default="")
    parser.add_argument("--agent-name", default="agent1")
    parser.add_argument("--claw-id", default="agent1")
    parser.add_argument("--description", default="用于偷菜 Demo 鉴权")
    parser.add_argument("--platform-name", default="智能体凭证农场")
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument(
        "--receipt",
        type=Path,
        default=Path(".logs/cmcc-agent1-issuance.json"),
    )
    parser.add_argument("--confirm-create-and-issue", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config: IssueConfig | None = None
    try:
        config = _build_config(args)
        with httpx.Client(
            base_url=config.base_url,
            timeout=config.timeout_seconds,
            follow_redirects=False,
        ) as client:
            receipt = create_and_issue_agent(config, args.receipt, client)
    except Exception as exc:
        safe_message = (
            _safe_error(exc, config)
            if config is not None
            else f"{type(exc).__name__}: {exc}"[:300]
        )
        print(
            json.dumps(
                {"ok": False, "error": safe_message},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2

    print(
        json.dumps(
            {
                "ok": True,
                "status": receipt["status"],
                "environment": receipt["environment"],
                "agentName": receipt["agentName"],
                "clawId": receipt["clawId"],
                "aic": _mask_identifier(receipt["aic"], head=16, tail=6),
                "vcRecordId": _mask_identifier(receipt["vcRecordId"]),
                "publicKeyReturned": bool(receipt["publicKey"]),
                "receipt": str(args.receipt),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
