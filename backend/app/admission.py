from __future__ import annotations

import secrets
import threading
import time
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import httpx

from .config import Settings, settings
from .credentials import CmccCrypto


QUERY_PATH = "/api/cmvc-tocp-server/open-api/vc/listByTemplateId"
STATUS_NAMES = {
    1: "PENDING",
    2: "ACTIVE",
    3: "REVOKED",
    5: "EXPIRED",
    6: "REJECTED",
    7: "REVOKED",
}


@dataclass(frozen=True)
class AdmissionDecision:
    agent_id: str
    external_agent_name: str
    allowed: bool
    mode: str
    upstream_status: str
    message: str
    checked_at: str | None
    cached: bool
    environment: str
    template_id: str
    record_count: int

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        return {
            "agentId": payload["agent_id"],
            "externalAgentName": payload["external_agent_name"],
            "allowed": payload["allowed"],
            "mode": payload["mode"],
            "upstreamStatus": payload["upstream_status"],
            "message": payload["message"],
            "checkedAt": payload["checked_at"],
            "cached": payload["cached"],
            "environment": payload["environment"],
            "templateId": payload["template_id"],
            "recordCount": payload["record_count"],
        }


class CmccAdmissionVerifier:
    """Read-only CMCC admission check with a short in-memory cache."""

    def __init__(
        self,
        config: Settings = settings,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.config = config
        self.transport = transport
        self._cache: dict[str, tuple[float, AdmissionDecision]] = {}
        self._cache_lock = threading.RLock()

    @property
    def mode(self) -> str:
        value = self.config.cmcc_admission_mode.lower().strip()
        return value if value in {"off", "demo", "strict"} else "off"

    @property
    def agent_mappings(self) -> dict[str, str]:
        mappings: dict[str, str] = {}
        for item in self.config.cmcc_admission_agent_mappings.split(","):
            agent_id, separator, external_name = item.strip().partition(":")
            if separator and agent_id.strip() and external_name.strip():
                mappings[agent_id.strip()] = external_name.strip()
        if mappings:
            return mappings

        agent_id = self.config.cmcc_admission_agent_id.strip()
        external_name = self.config.cmcc_admission_agent_name.strip()
        if agent_id and external_name:
            mappings[agent_id] = external_name
        mappings.setdefault("agent-nova", "agent2")
        mappings.setdefault("agent-orbit", "agent3")
        return mappings

    def external_agent_name_for(self, agent_id: str) -> str:
        return self.agent_mappings.get(agent_id, "")

    def applies_to(self, agent_id: str) -> bool:
        return self.mode != "off" and agent_id in self.agent_mappings

    def descriptor(self) -> dict[str, Any]:
        mapped_agents = [
            {
                "agentId": agent_id,
                "externalAgentName": external_name,
            }
            for agent_id, external_name in self.agent_mappings.items()
        ]
        return {
            "mode": self.mode,
            "mappedAgents": mapped_agents,
            "cacheSeconds": self.config.cmcc_admission_cache_seconds,
            "timeoutSeconds": self.config.cmcc_admission_timeout_seconds,
        }

    def clear(self, agent_id: str | None = None) -> None:
        with self._cache_lock:
            if agent_id is None:
                self._cache.clear()
            else:
                self._cache.pop(agent_id, None)

    def snapshot(self, agent_id: str) -> AdmissionDecision:
        if not self.applies_to(agent_id):
            return self._local_decision(agent_id)
        with self._cache_lock:
            cached = self._cache.get(agent_id)
        if cached is None:
            return self._decision(
                agent_id=agent_id,
                allowed=False,
                mode="DENIED",
                upstream_status="NOT_CHECKED",
                message="尚未执行中移接口校验",
                checked_at=None,
            )
        return replace(cached[1], cached=True)

    def verify(self, agent_id: str, *, force: bool = False) -> AdmissionDecision:
        if not self.applies_to(agent_id):
            return self._local_decision(agent_id)

        now = time.monotonic()
        with self._cache_lock:
            cached = self._cache.get(agent_id)
            if (
                not force
                and cached is not None
                and now - cached[0] < self.config.cmcc_admission_cache_seconds
            ):
                return replace(cached[1], cached=True)

        decision = self._query(agent_id)
        with self._cache_lock:
            self._cache[agent_id] = (time.monotonic(), decision)
        return decision

    def _local_decision(self, agent_id: str) -> AdmissionDecision:
        return self._decision(
            agent_id=agent_id,
            allowed=True,
            mode="LOCAL",
            upstream_status="LOCAL",
            message="当前智能体使用本地 Demo 凭证门禁",
            checked_at=None,
        )

    def _decision(
        self,
        *,
        agent_id: str,
        allowed: bool,
        mode: str,
        upstream_status: str,
        message: str,
        checked_at: str | None,
        record_count: int = 0,
    ) -> AdmissionDecision:
        return AdmissionDecision(
            agent_id=agent_id,
            external_agent_name=self.external_agent_name_for(agent_id),
            allowed=allowed,
            mode=mode,
            upstream_status=upstream_status,
            message=message[:240],
            checked_at=checked_at,
            cached=False,
            environment=urlparse(self.config.cmcc_base_url).hostname or "",
            template_id=self.config.cmcc_agent_template_id,
            record_count=record_count,
        )

    def _query(self, agent_id: str) -> AdmissionDecision:
        checked_at = datetime.now(UTC).isoformat()
        external_agent_name = self.external_agent_name_for(agent_id)
        missing = [
            name
            for name, value in {
                "CMCC_BASE_URL": self.config.cmcc_base_url,
                "CMCC_APP_ID": self.config.cmcc_app_id,
                "CMCC_APP_KEY": self.config.cmcc_app_key,
                "CMCC_DEMO_PHONE": self.config.cmcc_demo_phone,
                "CMCC_AGENT_TEMPLATE_ID": self.config.cmcc_agent_template_id,
                "CMCC_ADMISSION_AGENT_MAPPINGS": external_agent_name,
            }.items()
            if not value
        ]
        if missing:
            return self._decision(
                agent_id=agent_id,
                allowed=False,
                mode="DENIED",
                upstream_status="CONFIG_ERROR",
                message=f"中移准入配置缺失：{', '.join(missing)}",
                checked_at=checked_at,
            )

        body = {
            "nonce": secrets.token_hex(16),
            "timestamp": int(datetime.now(UTC).timestamp() * 1000),
            "phoneNo": CmccCrypto.encrypt_phone_ecb(
                self.config.cmcc_demo_phone,
                self.config.cmcc_app_key,
            ),
            "templateId": self.config.cmcc_agent_template_id,
        }
        headers = {
            "appId": self.config.cmcc_app_id,
            "signValue": CmccCrypto.sign(body, self.config.cmcc_app_key),
        }

        try:
            with httpx.Client(
                base_url=self.config.cmcc_base_url.rstrip("/"),
                timeout=self.config.cmcc_admission_timeout_seconds,
                follow_redirects=False,
                transport=self.transport,
            ) as client:
                response = client.post(QUERY_PATH, json=body, headers=headers)
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPStatusError as exc:
            return self._decision(
                agent_id=agent_id,
                allowed=False,
                mode="DENIED",
                upstream_status="HTTP_ERROR",
                message=f"中移接口返回 HTTP {exc.response.status_code}，游戏准入被拒绝",
                checked_at=checked_at,
            )
        except httpx.RequestError as exc:
            return self._decision(
                agent_id=agent_id,
                allowed=False,
                mode="DENIED",
                upstream_status="NETWORK_ERROR",
                message=f"中移接口连接失败（{type(exc).__name__}），游戏准入被拒绝",
                checked_at=checked_at,
            )
        except (ValueError, TypeError):
            return self._decision(
                agent_id=agent_id,
                allowed=False,
                mode="DENIED",
                upstream_status="INVALID_RESPONSE",
                message="中移接口响应格式无效，游戏准入被拒绝",
                checked_at=checked_at,
            )

        if not isinstance(payload, dict):
            return self._decision(
                agent_id=agent_id,
                allowed=False,
                mode="DENIED",
                upstream_status="INVALID_RESPONSE",
                message="中移接口响应格式无效，游戏准入被拒绝",
                checked_at=checked_at,
            )

        if str(payload.get("code")) != "0":
            message = self._safe_message(
                str(payload.get("msg") or "中移接口返回业务错误")
            )
            return self._decision(
                agent_id=agent_id,
                allowed=False,
                mode="DENIED",
                upstream_status="API_ERROR",
                message=f"{message}，游戏准入被拒绝",
                checked_at=checked_at,
            )

        records = self._extract_records(payload.get("data"))
        matching = [
            record
            for record in records
            if self._matches_agent_name(record, external_agent_name)
        ]
        active = next(
            (
                record
                for record in matching
                if self._status_code(record.get("vcStatus")) == 2
            ),
            None,
        )
        if active is not None:
            return self._decision(
                agent_id=agent_id,
                allowed=True,
                mode="REAL_ACTIVE",
                upstream_status="ACTIVE",
                message=(
                    f"中移接口已确认 {external_agent_name} "
                    "持有生效中的智能体身份凭证"
                ),
                checked_at=checked_at,
                record_count=len(records),
            )

        if matching:
            status_code = self._status_code(matching[0].get("vcStatus"))
            status = STATUS_NAMES.get(status_code, "INACTIVE")
            return self._decision(
                agent_id=agent_id,
                allowed=False,
                mode="DENIED",
                upstream_status=status,
                message=(
                    f"中移接口返回的 {external_agent_name} "
                    f"凭证状态为 {status}，"
                    "游戏准入被拒绝"
                ),
                checked_at=checked_at,
                record_count=len(records),
            )

        if self.mode == "demo":
            return self._decision(
                agent_id=agent_id,
                allowed=True,
                mode="DEMO_CONNECTED",
                upstream_status="NOT_FOUND",
                message="中移接口已连通，未发现测试凭证，当前使用演示准入",
                checked_at=checked_at,
                record_count=len(records),
            )
        return self._decision(
            agent_id=agent_id,
            allowed=False,
            mode="DENIED",
            upstream_status="NOT_FOUND",
            message=(
                "中移接口已连通，但未发现 "
                f"{external_agent_name} 的有效凭证"
            ),
            checked_at=checked_at,
            record_count=len(records),
        )

    def _matches_agent_name(
        self,
        record: dict[str, Any],
        expected: str,
    ) -> bool:
        candidates: list[Any] = [record.get("agentName")]
        fields = record.get("fieldList")
        if isinstance(fields, list):
            for field in fields:
                if not isinstance(field, dict):
                    continue
                field_name = str(field.get("fieldName") or "").lower()
                normalized = field_name.replace("_", "").replace("-", "")
                if "agentname" in normalized or "智能体名称" in field_name:
                    candidates.append(field.get("fieldValue"))

        for value in candidates:
            text = str(value or "")
            if text == expected:
                return True
            if not text:
                continue
            try:
                plain = CmccCrypto.decrypt_aes_ecb(
                    text,
                    self.config.cmcc_app_key,
                    key_mode="md5",
                )
            except Exception:
                continue
            if plain == expected:
                return True
        return False

    @staticmethod
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

    @staticmethod
    def _status_code(value: Any) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return -1

    def _safe_message(self, value: str) -> str:
        message = value.replace("\n", " ")
        for sensitive in (
            self.config.cmcc_app_id,
            self.config.cmcc_app_key,
            self.config.cmcc_client_secret,
            self.config.cmcc_demo_phone,
        ):
            if sensitive:
                message = message.replace(sensitive, "[redacted]")
        return message[:160]
