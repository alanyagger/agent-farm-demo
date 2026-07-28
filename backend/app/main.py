from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .config import settings
from .credentials import CredentialService
from .database import Base, SessionLocal, engine, get_db
from .game import GameEngine
from .models import Agent, Owner
from .schemas import AutomationRequest, SimulateCredentialStatusRequest
from .seed import ensure_seeded, reset_demo
from .serializers import dashboard, serialize_owner


credential_service = CredentialService()
game_engine = GameEngine(credential_service)


async def scheduler_loop() -> None:
    while True:
        await asyncio.sleep(settings.scheduler_interval_seconds)
        with SessionLocal() as db:
            agent_ids = db.scalars(
                select(Agent.id)
                .where(Agent.automation_enabled.is_(True))
                .order_by(Agent.id)
            ).all()
        for agent_id in agent_ids:
            with SessionLocal() as db:
                try:
                    game_engine.run_agent(db, agent_id, source="SCHEDULER")
                except Exception:
                    db.rollback()


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        ensure_seeded(db)
    scheduler = asyncio.create_task(scheduler_loop())
    try:
        yield
    finally:
        scheduler.cancel()
        with suppress(asyncio.CancelledError):
            await scheduler


app = FastAPI(
    title=settings.app_name,
    version="1.0.0",
    description="Credential-gated autonomous farm demo with traceable agent actions.",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "provider": credential_service.provider.name,
        "schedulerIntervalSeconds": settings.scheduler_interval_seconds,
    }


@app.get("/api/owners")
def list_owners(db: Session = Depends(get_db)) -> list[dict]:
    owners = db.scalars(
        select(Owner)
        .options(
            selectinload(Owner.agent).selectinload(Agent.credential),
            selectinload(Owner.farm),
        )
        .order_by(Owner.id)
    ).all()
    return [serialize_owner(owner) for owner in owners]


@app.get("/api/owners/{owner_id}/dashboard")
def owner_dashboard(owner_id: str, db: Session = Depends(get_db)) -> dict:
    payload = dashboard(db, owner_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="Owner not found")
    return payload


@app.post("/api/owners/{owner_id}/agent/credential/apply")
def apply_credential(owner_id: str, db: Session = Depends(get_db)) -> dict:
    owner = db.scalar(
        select(Owner)
        .where(Owner.id == owner_id)
        .options(
            selectinload(Owner.agent).selectinload(Agent.credential),
            selectinload(Owner.agent).selectinload(Agent.credential_events),
        )
    )
    if owner is None:
        raise HTTPException(status_code=404, detail="Owner not found")
    try:
        credential_service.apply(db, owner, owner.agent)
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    db.expire_all()
    return dashboard(db, owner_id)


@app.post("/api/agents/{agent_id}/run")
def run_agent(agent_id: str, db: Session = Depends(get_db)) -> dict:
    agent = db.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    try:
        actions = game_engine.run_agent(db, agent_id, source="MANUAL")
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "actionCount": len(actions),
        "traceIds": [action.trace_id for action in actions],
        "dashboard": dashboard(db, agent.owner_id),
    }


@app.patch("/api/agents/{agent_id}/automation")
def set_automation(
    agent_id: str,
    request: AutomationRequest,
    db: Session = Depends(get_db),
) -> dict:
    agent = db.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    agent.automation_enabled = request.enabled
    db.commit()
    return {
        "agentId": agent.id,
        "automationEnabled": agent.automation_enabled,
    }


@app.post("/api/agents/{agent_id}/credential/simulate-status")
def simulate_credential_status(
    agent_id: str,
    request: SimulateCredentialStatusRequest,
    db: Session = Depends(get_db),
) -> dict:
    agent = db.scalar(
        select(Agent)
        .where(Agent.id == agent_id)
        .options(
            selectinload(Agent.owner),
            selectinload(Agent.credential),
            selectinload(Agent.credential_events),
        )
    )
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    try:
        credential_service.simulate_status(db, agent, request.status)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return dashboard(db, agent.owner_id)


@app.post("/api/demo/reset")
def reset(db: Session = Depends(get_db)) -> dict:
    with game_engine.lock:
        reset_demo(db)
    return {"status": "reset", "defaultOwnerId": "owner-lin"}
