import hashlib
from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .models import (
    Agent,
    AgentAction,
    AgentCredential,
    AgentRun,
    CredentialEvent,
    Farm,
    Inventory,
    Owner,
    Plot,
    utcnow,
)


OWNERS = [
    {
        "id": "owner-lin",
        "nickname": "林晓",
        "phone_masked": "138****1024",
        "did": "did:cm:person:linxiao-1024",
        "accent": "green",
        "agent_id": "agent-sprout",
        "agent_name": "青芽",
        "claw_id": "claw-farm-sprout",
        "description": "偏好稳健种植，也会观察邻居成熟作物。",
    },
    {
        "id": "owner-zhou",
        "nickname": "周晴",
        "phone_masked": "139****2088",
        "did": "did:cm:person:zhouqing-2088",
        "accent": "yellow",
        "agent_id": "agent-orbit",
        "agent_name": "禾光",
        "claw_id": "claw-farm-orbit",
        "description": "重视高价值作物和及时收获。",
    },
    {
        "id": "owner-chen",
        "nickname": "陈屿",
        "phone_masked": "137****3306",
        "did": "did:cm:person:chenyu-3306",
        "accent": "blue",
        "agent_id": "agent-nova",
        "agent_name": "田小诺",
        "claw_id": "claw-farm-nova",
        "description": "完成身份校验后才能再次参与农场协作。",
    },
]


def _phone_hash(masked_phone: str) -> str:
    return hashlib.sha256(f"demo:{masked_phone}".encode("utf-8")).hexdigest()


def reset_demo(db: Session) -> None:
    for model in (
        AgentRun,
        AgentAction,
        CredentialEvent,
        Inventory,
        Plot,
        AgentCredential,
        Agent,
        Farm,
        Owner,
    ):
        db.execute(delete(model))

    now = utcnow()
    for index, record in enumerate(OWNERS):
        owner = Owner(
            id=record["id"],
            nickname=record["nickname"],
            phone_masked=record["phone_masked"],
            phone_hash=_phone_hash(record["phone_masked"]),
            did=record["did"],
            accent=record["accent"],
        )
        agent = Agent(
            id=record["agent_id"],
            owner_id=owner.id,
            name=record["agent_name"],
            claw_id=record["claw_id"],
            platform_name="Agent Farm Web",
            description=record["description"],
            automation_enabled=False,
        )
        farm = Farm(
            id=f"farm-{index + 1}",
            owner_id=owner.id,
            name=f"{owner.nickname}的可信农场",
            coins=120,
        )
        db.add_all([owner, agent, farm])
        db.flush()

        for crop_type in ("CARROT", "TOMATO", "CORN"):
            db.add(Inventory(farm_id=farm.id, crop_type=crop_type, quantity=0))
        for position in range(6):
            db.add(
                Plot(
                    id=f"{farm.id}-plot-{position + 1}",
                    farm_id=farm.id,
                    position=position,
                )
            )

        if index < 2:
            credential = AgentCredential(
                agent_id=agent.id,
                provider="mock",
                template_id="TPL-AGENT-DEMO-001",
                aic=f"AIC-CM-2026-000{index + 1}",
                vc_record_id=f"VC-DEMO-2026-000{index + 1}",
                public_key=f"04{'A1' if index == 0 else 'B2'}" * 64,
                issue_mode="PLATFORM",
                status="ACTIVE",
                issued_at=now - timedelta(days=1),
                expires_at=now + timedelta(days=30),
                last_verified_at=now,
            )
            db.add(credential)
            db.add_all(
                [
                    CredentialEvent(
                        agent_id=agent.id,
                        step="OWNER_DID_VERIFIED",
                        detail="主人实名 DID 已核验",
                        created_at=now - timedelta(minutes=4),
                    ),
                    CredentialEvent(
                        agent_id=agent.id,
                        step="AGENT_CREATED",
                        detail=f"智能体已获得 AIC：{credential.aic}",
                        created_at=now - timedelta(minutes=3),
                    ),
                    CredentialEvent(
                        agent_id=agent.id,
                        step="APPLICATION_ACCEPTED",
                        detail=f"凭证申领记录：{credential.vc_record_id}",
                        created_at=now - timedelta(minutes=2),
                    ),
                    CredentialEvent(
                        agent_id=agent.id,
                        step="CREDENTIAL_ACTIVE",
                        detail="智能体身份凭证签发完成",
                        created_at=now - timedelta(minutes=1),
                    ),
                ]
            )

    db.flush()

    # Seed different growth stages so the first screen is useful immediately.
    seeded_plots = {
        "farm-1-plot-1": ("TOMATO", now - timedelta(seconds=16), now + timedelta(seconds=2), 5),
        "farm-1-plot-2": ("CARROT", now - timedelta(seconds=5), now + timedelta(seconds=7), 4),
        "farm-2-plot-1": ("CORN", now - timedelta(seconds=30), now - timedelta(seconds=1), 6),
        "farm-2-plot-2": ("TOMATO", now - timedelta(seconds=8), now + timedelta(seconds=10), 5),
    }
    for plot_id, (crop, planted_at, matures_at, crop_yield) in seeded_plots.items():
        plot = db.get(Plot, plot_id)
        if plot:
            plot.crop_type = crop
            plot.planted_at = planted_at
            plot.matures_at = matures_at
            plot.yield_total = crop_yield
            plot.yield_remaining = crop_yield
    db.commit()


def ensure_seeded(db: Session) -> None:
    if db.scalar(select(Owner.id).limit(1)) is None:
        reset_demo(db)
        return

    # Backend restarts always return the two pre-issued demo agents to a quiet
    # state. They can still be run manually or re-enabled from the dashboard.
    for agent in db.scalars(
        select(Agent).where(Agent.id.in_(("agent-sprout", "agent-orbit")))
    ):
        agent.automation_enabled = False
    db.commit()
