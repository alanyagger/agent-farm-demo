from __future__ import annotations

from datetime import UTC, datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class Owner(Base):
    __tablename__ = "owners"

    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    nickname: Mapped[str] = mapped_column(String(80))
    phone_masked: Mapped[str] = mapped_column(String(32))
    phone_hash: Mapped[str] = mapped_column(String(64), unique=True)
    did: Mapped[str] = mapped_column(String(180), unique=True)
    accent: Mapped[str] = mapped_column(String(24), default="green")

    agent: Mapped["Agent"] = relationship(
        back_populates="owner", cascade="all, delete-orphan", uselist=False
    )
    farm: Mapped["Farm"] = relationship(
        back_populates="owner", cascade="all, delete-orphan", uselist=False
    )


class Agent(Base):
    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id"), unique=True)
    name: Mapped[str] = mapped_column(String(80))
    claw_id: Mapped[str] = mapped_column(String(80), unique=True)
    platform_name: Mapped[str] = mapped_column(String(80), default="Agent Farm")
    description: Mapped[str] = mapped_column(String(240), default="")
    agent_type: Mapped[str] = mapped_column(String(24), default="PERSONAL")
    automation_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    last_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    owner: Mapped[Owner] = relationship(back_populates="agent")
    credential: Mapped[Optional["AgentCredential"]] = relationship(
        back_populates="agent", cascade="all, delete-orphan", uselist=False
    )
    credential_events: Mapped[list["CredentialEvent"]] = relationship(
        back_populates="agent", cascade="all, delete-orphan"
    )


class AgentCredential(Base):
    __tablename__ = "agent_credentials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"), unique=True)
    provider: Mapped[str] = mapped_column(String(32), default="mock")
    template_id: Mapped[str] = mapped_column(String(96))
    aic: Mapped[str] = mapped_column(String(160), unique=True)
    vc_record_id: Mapped[str] = mapped_column(String(160), unique=True)
    public_key: Mapped[str] = mapped_column(Text, default="")
    issue_mode: Mapped[str] = mapped_column(String(24), default="PLATFORM")
    status: Mapped[str] = mapped_column(String(24), default="PENDING")
    issued_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    agent: Mapped[Agent] = relationship(back_populates="credential")


class CredentialEvent(Base):
    __tablename__ = "credential_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"))
    step: Mapped[str] = mapped_column(String(48))
    status: Mapped[str] = mapped_column(String(24), default="SUCCESS")
    detail: Mapped[str] = mapped_column(String(320))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    agent: Mapped[Agent] = relationship(back_populates="credential_events")


class Farm(Base):
    __tablename__ = "farms"

    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id"), unique=True)
    name: Mapped[str] = mapped_column(String(100))
    coins: Mapped[int] = mapped_column(Integer, default=120)

    owner: Mapped[Owner] = relationship(back_populates="farm")
    plots: Mapped[list["Plot"]] = relationship(
        back_populates="farm",
        cascade="all, delete-orphan",
        order_by="Plot.position",
    )
    inventory: Mapped[list["Inventory"]] = relationship(
        back_populates="farm", cascade="all, delete-orphan"
    )


class Plot(Base):
    __tablename__ = "plots"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    farm_id: Mapped[str] = mapped_column(ForeignKey("farms.id"))
    position: Mapped[int] = mapped_column(Integer)
    crop_type: Mapped[Optional[str]] = mapped_column(String(24), nullable=True)
    planted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    matures_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    yield_total: Mapped[int] = mapped_column(Integer, default=0)
    yield_remaining: Mapped[int] = mapped_column(Integer, default=0)

    farm: Mapped[Farm] = relationship(back_populates="plots")


class Inventory(Base):
    __tablename__ = "inventory"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    farm_id: Mapped[str] = mapped_column(ForeignKey("farms.id"))
    crop_type: Mapped[str] = mapped_column(String(24))
    quantity: Mapped[int] = mapped_column(Integer, default=0)

    farm: Mapped[Farm] = relationship(back_populates="inventory")


class AgentAction(Base):
    __tablename__ = "agent_actions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trace_id: Mapped[str] = mapped_column(String(48), unique=True)
    actor_agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"))
    actor_owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id"))
    target_owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id"))
    plot_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    action_type: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(24))
    reason: Mapped[str] = mapped_column(String(320))
    credential_status: Mapped[str] = mapped_column(String(24))
    crop_type: Mapped[Optional[str]] = mapped_column(String(24), nullable=True)
    quantity: Mapped[int] = mapped_column(Integer, default=0)
    source: Mapped[str] = mapped_column(String(24), default="SCHEDULER")
    before_state: Mapped[str] = mapped_column(Text, default="")
    after_state: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
