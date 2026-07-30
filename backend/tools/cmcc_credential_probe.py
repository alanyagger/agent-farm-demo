from __future__ import annotations

import argparse
import json
import secrets
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import httpx

from backend.app.config import settings
from backend.app.credentials import CmccCrypto


QUERY_PATH = "/api/cmvc-tocp-server/open-api/vc/listByTemplateId"
PRODUCTION_HOST = "vc.cmccsign.com"
STATUS_LABELS = {
    1: "PENDING/待签发",
    2: "ACTIVE/生效中",
    3: "REVOKED/已吊销",
    5: "EXPIRED/超时未签发",
    6: "REJECTED/拒绝签发",
    7: "REVOKED/用户已注销",
}


@dataclass(frozen=True)
class ProbeConfig:
    base_url: str
    app_id: str
    app_key: str
    phone: str
    template_id: str
    agent_name: str
    aes_key_mode: str
    timeout_seconds: float


def _mask_identifier(value: Any, head: int = 8, tail: int = 6) -> str:
    text = str(value or "")
    if not text:
        return "<empty>"
    if len(text) <= head + tail + 3:
        return f"{text[:2]}***{text[-2:]}" if len(text) > 4 else "***"
    return f"{text[:head]}...{text[-tail:]}"


def _mask_phone(value: str) -> str:
    digits = str(value or "")
    if len(digits) < 7:
        return "***"
    return f"{digits[:3]}****{digits[-4:]}"


def _field_kind(field_name: str) -> str:
    normalized = field_name.lower().replace("_", "").replace("-", "")
    if any(token in normalized for token in ("phoneno", "mobile", "手机号", "电话")):
        return "phone"
    if any(token in normalized for token in ("agentname", "智能体名称")):
        return "agent_name"
    if any(token in normalized for token in ("aic", "did", "智能体标识")):
        return "identifier"
    if any(token in normalized for token in ("description", "描述")):
        return "description"
    return "other"


def _decrypt_field(
    value: Any,
    app_key: str,
    aes_key_mode: str,
) -> tuple[str, bool]:
    text = str(value or "")
    if not text:
        return "", False
    try:
        return CmccCrypto.decrypt_aes_ecb(text, app_key, aes_key_mode), True
    except Exception:
        return text, False


def _safe_field(
    field_name: str,
    value: Any,
    app_key: str,
    aes_key_mode: str,
) -> tuple[dict[str, Any], str]:
    plain, decrypted = _decrypt_field(value, app_key, aes_key_mode)
    kind = _field_kind(field_name)
    if kind == "phone":
        display = _mask_phone(plain)
    elif kind == "agent_name":
        display = plain[:64]
    elif kind == "identifier":
        display = _mask_identifier(plain, head=16, tail=6)
    elif kind == "description":
        display = f"<present:{len(plain)} chars>" if plain else "<empty>"
    else:
        display = "<decrypted>" if decrypted else "<opaque>"
    return {
        "name": field_name[:80],
        "value": display,
        "decrypted": decrypted,
    }, plain


def sanitize_records(
    records: list[dict[str, Any]],
    app_key: str,
    agent_name: str,
    aes_key_mode: str = "md5",
) -> list[dict[str, Any]]:
    sanitized: list[dict[str, Any]] = []
    for index, record in enumerate(records, start=1):
        safe_fields: list[dict[str, Any]] = []
        matched_agent_name = False
        field_list = record.get("fieldList")
        if isinstance(field_list, list):
            for field in field_list:
                if not isinstance(field, dict):
                    continue
                field_name = str(field.get("fieldName") or "")
                safe_field, plain = _safe_field(
                    field_name,
                    field.get("fieldValue"),
                    app_key,
                    aes_key_mode,
                )
                safe_fields.append(safe_field)
                if (
                    agent_name
                    and _field_kind(field_name) == "agent_name"
                    and plain == agent_name
                ):
                    matched_agent_name = True

        raw_status = record.get("vcStatus")
        try:
            status_code = int(raw_status)
        except (TypeError, ValueError):
            status_code = -1
        sanitized.append(
            {
                "index": index,
                "vcRecordId": _mask_identifier(record.get("vcRecordId")),
                "vcStatus": status_code,
                "status": STATUS_LABELS.get(status_code, "UNKNOWN"),
                "issueTime": str(record.get("issueTime") or ""),
                "matchesAgentName": matched_agent_name,
                "fields": safe_fields,
            }
        )
    return sanitized


def _extract_records(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in (
            "records",
            "list",
            "rows",
            "items",
            "vcList",
            "credentialList",
            "data",
        ):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def query_existing_credentials(config: ProbeConfig) -> dict[str, Any]:
    body = {
        "nonce": secrets.token_hex(16),
        "timestamp": int(datetime.now(UTC).timestamp() * 1000),
        "phoneNo": CmccCrypto.encrypt_aes_ecb(
            config.phone,
            config.app_key,
            config.aes_key_mode,
        ),
        "templateId": config.template_id,
    }
    headers = {
        "appId": config.app_id,
        "signValue": CmccCrypto.sign(body, config.app_key),
    }
    with httpx.Client(
        base_url=config.base_url,
        timeout=config.timeout_seconds,
        follow_redirects=False,
    ) as client:
        response = client.post(QUERY_PATH, json=body, headers=headers)
        response.raise_for_status()
        payload = response.json()

    code = payload.get("code")
    if str(code) != "0":
        message = str(payload.get("msg") or "CMCC API returned a failure")[:240]
        raise RuntimeError(f"CMCC code={code}: {message}")
    response_data = payload.get("data")
    records = _extract_records(response_data)
    safe_records = sanitize_records(
        records,
        config.app_key,
        config.agent_name,
        config.aes_key_mode,
    )
    return {
        "environment": urlparse(config.base_url).hostname,
        "endpoint": QUERY_PATH,
        "templateId": config.template_id,
        "phone": _mask_phone(config.phone),
        "aesKeyMode": config.aes_key_mode,
        "responseDataType": type(response_data).__name__,
        "responseDataKeys": (
            sorted(str(key)[:80] for key in response_data.keys())
            if isinstance(response_data, dict)
            else []
        ),
        "recordCount": len(safe_records),
        "matchedAgentCount": sum(
            1 for record in safe_records if record["matchesAgentName"]
        ),
        "records": safe_records,
    }


def _build_config(args: argparse.Namespace) -> ProbeConfig:
    base_url = (args.base_url or settings.cmcc_base_url).rstrip("/")
    template_id = args.template_id or settings.cmcc_agent_template_id
    required = {
        "CMCC_BASE_URL/--base-url": base_url,
        "CMCC_APP_ID": settings.cmcc_app_id,
        "CMCC_APP_KEY": settings.cmcc_app_key,
        "CMCC_DEMO_PHONE": settings.cmcc_demo_phone,
        "CMCC_AGENT_TEMPLATE_ID/--template-id": template_id,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise ValueError(f"Missing configuration: {', '.join(missing)}")

    parsed = urlparse(base_url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("CMCC base URL must be an absolute HTTPS URL")
    if parsed.hostname.lower() == PRODUCTION_HOST and not args.allow_production:
        raise ValueError(
            "Production endpoint refused. Use --allow-production only after test approval."
        )
    return ProbeConfig(
        base_url=base_url,
        app_id=settings.cmcc_app_id,
        app_key=settings.cmcc_app_key,
        phone=settings.cmcc_demo_phone,
        template_id=template_id,
        agent_name=args.agent_name,
        aes_key_mode=args.aes_key_mode,
        timeout_seconds=args.timeout,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only CMCC credential query. It never creates credentials or "
            "writes the local database, and all identifiers are masked."
        )
    )
    parser.add_argument("--base-url", default="")
    parser.add_argument("--template-id", default="")
    parser.add_argument("--agent-name", default="agent1")
    parser.add_argument(
        "--aes-key-mode",
        choices=("md5", "direct"),
        default="md5",
        help="AES key derivation used by this endpoint",
    )
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--allow-production", action="store_true")
    return parser.parse_args()


def main() -> int:
    try:
        config = _build_config(parse_args())
        report = query_existing_credentials(config)
    except httpx.HTTPStatusError as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "HTTP request rejected",
                    "httpStatus": exc.response.status_code,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2
    except httpx.RequestError as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": f"Network request failed: {type(exc).__name__}",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2
    except (ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(
            json.dumps(
                {"ok": False, "error": str(exc)[:300]},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2

    print(json.dumps({"ok": True, **report}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
