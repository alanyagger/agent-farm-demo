from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

import httpx
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from sqlalchemy.orm import Session

from .config import settings
from .models import (
    Agent,
    AgentCredential,
    CredentialEvent,
    Owner,
    utcnow,
)


@dataclass(frozen=True)
class CreatedAgentIdentity:
    aic: str
    vc_record_id: str


@dataclass(frozen=True)
class IssuedCredential:
    vc_record_id: str
    public_key: str
    issued_at: datetime
    expires_at: datetime


class CredentialProvider(Protocol):
    name: str

    def create_agent(self, owner: Owner, agent: Agent) -> CreatedAgentIdentity:
        ...

    def issue_credential(
        self,
        owner: Owner,
        agent: Agent,
        created: CreatedAgentIdentity,
    ) -> IssuedCredential:
        ...

    def query_status(
        self,
        owner: Owner,
        credential: AgentCredential,
    ) -> str:
        ...


class MockCredentialProvider:
    name = "mock"

    def create_agent(self, owner: Owner, agent: Agent) -> CreatedAgentIdentity:
        suffix = uuid.uuid4().hex[:12].upper()
        return CreatedAgentIdentity(
            aic=f"AIC-CM-{suffix}",
            vc_record_id=f"VC-{uuid.uuid4().hex[:16].upper()}",
        )

    def issue_credential(
        self,
        owner: Owner,
        agent: Agent,
        created: CreatedAgentIdentity,
    ) -> IssuedCredential:
        key_material = hashlib.sha256(
            f"{owner.did}:{agent.claw_id}:{created.aic}".encode("utf-8")
        ).hexdigest()
        issued_at = utcnow()
        return IssuedCredential(
            vc_record_id=created.vc_record_id,
            public_key=f"04{key_material}{key_material}",
            issued_at=issued_at,
            expires_at=issued_at + timedelta(days=30),
        )

    def query_status(
        self,
        owner: Owner,
        credential: AgentCredential,
    ) -> str:
        return credential.status


class CmccCrypto:
    """Endpoint-scoped crypto helpers matching the supplied interface workbook."""

    @staticmethod
    def canonical_json(body: dict) -> str:
        return json.dumps(
            body,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def sign(cls, body: dict, secret: str) -> str:
        digest = hmac.new(
            secret.encode("utf-8"),
            cls.canonical_json(body).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return digest.upper()

    @staticmethod
    def encrypt_phone_ecb(phone: str, app_key: str) -> str:
        key = hashlib.md5(app_key.encode("utf-8")).digest()
        padder = padding.PKCS7(algorithms.AES.block_size).padder()
        padded = padder.update(phone.encode("utf-8")) + padder.finalize()
        encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
        encrypted = encryptor.update(padded) + encryptor.finalize()
        return base64.b64encode(encrypted).decode("ascii")


class CmccCredentialProvider:
    """Disabled-by-default adapter skeleton for the documented CMCC APIs."""

    name = "cmcc"

    def __init__(self) -> None:
        required = {
            "CMCC_BASE_URL": settings.cmcc_base_url,
            "CMCC_APP_ID": settings.cmcc_app_id,
            "CMCC_APP_KEY": settings.cmcc_app_key,
            "CMCC_CLIENT_SECRET": settings.cmcc_client_secret,
            "CMCC_AGENT_TEMPLATE_ID": settings.cmcc_agent_template_id,
            "CMCC_DEMO_PHONE": settings.cmcc_demo_phone,
        }
        missing = [key for key, value in required.items() if not value]
        if missing:
            raise RuntimeError(f"CMCC provider missing configuration: {', '.join(missing)}")
        self.client = httpx.Client(base_url=settings.cmcc_base_url, timeout=10)

    @staticmethod
    def _base_body() -> dict:
        return {
            "nonce": secrets.token_hex(16),
            "timestamp": int(datetime.utcnow().timestamp() * 1000),
        }

    def _post(self, path: str, body: dict, secret: str) -> dict:
        headers = {
            "appId": settings.cmcc_app_id,
            "signValue": CmccCrypto.sign(body, secret),
        }
        response = self.client.post(path, json=body, headers=headers)
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") != 0:
            raise RuntimeError(payload.get("msg") or f"CMCC request failed: {path}")
        return payload.get("data") or {}

    def create_agent(self, owner: Owner, agent: Agent) -> CreatedAgentIdentity:
        body = {
            **self._base_body(),
            "phoneNo": CmccCrypto.encrypt_phone_ecb(
                settings.cmcc_demo_phone, settings.cmcc_app_key
            ),
            "templateId": settings.cmcc_agent_template_id,
            "clawId": agent.claw_id,
            "registerTime": agent.created_at.strftime("%Y-%m-%d %H:%M:%S"),
            "agentName": agent.name,
            "agentDescription": agent.description,
            "platformName": agent.platform_name,
            # create endpoint: 1=personal, while issue endpoint uses 0=personal.
            "agentType": "1" if agent.agent_type == "PERSONAL" else "0",
        }
        data = self._post(
            "/api/cmvc-tocp-server/open-api/vc/agent/create",
            body,
            settings.cmcc_client_secret,
        )
        return CreatedAgentIdentity(
            aic=str(data["aic"]),
            vc_record_id=str(data["vcRecordId"]),
        )

    def issue_credential(
        self,
        owner: Owner,
        agent: Agent,
        created: CreatedAgentIdentity,
    ) -> IssuedCredential:
        body = {
            **self._base_body(),
            "phoneNo": CmccCrypto.encrypt_phone_ecb(
                settings.cmcc_demo_phone, settings.cmcc_app_key
            ),
            "templateId": settings.cmcc_agent_template_id,
            "aic": created.aic,
            "issueMode": 1,
            "agentName": agent.name,
            "agentDescription": agent.description,
            "agentType": 0 if agent.agent_type == "PERSONAL" else 1,
        }
        data = self._post(
            "/api/cmvc-tocp-server/open-api/vc/agent/issue",
            body,
            settings.cmcc_client_secret,
        )
        issued_at = utcnow()
        return IssuedCredential(
            vc_record_id=str(data.get("vcRecordId", created.vc_record_id)),
            public_key=str(data.get("publicKey") or ""),
            issued_at=issued_at,
            expires_at=issued_at + timedelta(days=30),
        )

    def query_status(
        self,
        owner: Owner,
        credential: AgentCredential,
    ) -> str:
        body = {
            **self._base_body(),
            "phoneNo": CmccCrypto.encrypt_phone_ecb(
                settings.cmcc_demo_phone, settings.cmcc_app_key
            ),
            "templateId": credential.template_id,
        }
        data = self._post(
            "/api/cmvc-tocp-server/open-api/vc/listByTemplateId",
            body,
            settings.cmcc_app_key,
        )
        records = data if isinstance(data, list) else []
        matching = next(
            (
                record
                for record in records
                if str(record.get("vcRecordId")) == credential.vc_record_id
            ),
            None,
        )
        status_map = {
            1: "PENDING",
            2: "ACTIVE",
            3: "REVOKED",
            5: "EXPIRED",
            6: "REJECTED",
        }
        return status_map.get((matching or {}).get("vcStatus"), "REJECTED")


def build_provider() -> CredentialProvider:
    if settings.credential_provider.lower() == "cmcc":
        return CmccCredentialProvider()
    return MockCredentialProvider()


class CredentialService:
    def __init__(self, provider: CredentialProvider | None = None) -> None:
        self.provider = provider or build_provider()

    @staticmethod
    def _event(db: Session, agent: Agent, step: str, detail: str) -> None:
        db.add(
            CredentialEvent(
                agent_id=agent.id,
                step=step,
                status="SUCCESS",
                detail=detail,
            )
        )
        db.flush()

    def apply(self, db: Session, owner: Owner, agent: Agent) -> AgentCredential:
        self._event(
            db,
            agent,
            "OWNER_DID_VERIFIED",
            f"主人实名 DID 已核验：{owner.did}",
        )
        created = self.provider.create_agent(owner, agent)
        self._event(
            db,
            agent,
            "AGENT_CREATED",
            f"智能体统一标识已创建：{created.aic}",
        )

        credential = agent.credential
        if credential is None:
            credential = AgentCredential(
                agent=agent,
                provider=self.provider.name,
                template_id=settings.cmcc_agent_template_id or "TPL-AGENT-DEMO-001",
                aic=created.aic,
                vc_record_id=created.vc_record_id,
                status="PENDING",
            )
            db.add(credential)
        else:
            credential.provider = self.provider.name
            credential.aic = created.aic
            credential.vc_record_id = created.vc_record_id
            credential.status = "PENDING"
        db.flush()
        self._event(
            db,
            agent,
            "APPLICATION_ACCEPTED",
            f"凭证申领已受理：{created.vc_record_id}",
        )

        issued = self.provider.issue_credential(owner, agent, created)
        credential.vc_record_id = str(issued.vc_record_id)
        credential.public_key = issued.public_key
        credential.issue_mode = "PLATFORM"
        credential.status = "ACTIVE"
        credential.issued_at = issued.issued_at
        credential.expires_at = issued.expires_at
        credential.last_verified_at = issued.issued_at
        self._event(
            db,
            agent,
            "CREDENTIAL_ACTIVE",
            "智能体身份凭证签发完成，当前状态为生效中",
        )
        db.commit()
        db.refresh(credential)
        return credential

    def verify(
        self,
        db: Session,
        agent: Agent,
    ) -> tuple[str, AgentCredential | None]:
        credential = agent.credential
        if credential is None:
            return "MISSING", None
        if credential.expires_at and credential.expires_at <= utcnow():
            credential.status = "EXPIRED"
        if credential.status == "ACTIVE":
            credential.status = self.provider.query_status(agent.owner, credential)
        credential.last_verified_at = utcnow()
        db.flush()
        return credential.status, credential

    def simulate_status(
        self,
        db: Session,
        agent: Agent,
        status: str,
    ) -> AgentCredential:
        if self.provider.name != "mock":
            raise ValueError("Status simulation is available only for the mock provider")
        if agent.credential is None:
            raise ValueError("Agent has no credential")
        agent.credential.status = status
        if status == "EXPIRED":
            agent.credential.expires_at = utcnow() - timedelta(seconds=1)
        elif status == "ACTIVE" and (
            agent.credential.expires_at is None
            or agent.credential.expires_at <= utcnow()
        ):
            agent.credential.expires_at = utcnow() + timedelta(days=30)
        self._event(
            db,
            agent,
            f"STATUS_{status}",
            f"Demo 模拟凭证状态切换为 {status}",
        )
        db.commit()
        return agent.credential
