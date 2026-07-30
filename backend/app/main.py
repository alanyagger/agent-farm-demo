from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, suppress

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .admission import CmccAdmissionVerifier
from .agent_runtime import AgentRunInProgressError, AgentRuntime
from .config import settings
from .credentials import CredentialService
from .database import Base, SessionLocal, engine, get_db
from .game import GameEngine
from .model_provider import runtime_descriptor
from .models import Agent, Owner
from .schemas import AutomationRequest, SimulateCredentialStatusRequest
from .seed import ensure_seeded, reset_demo
from .serializers import dashboard, serialize_owner


credential_service = CredentialService()
admission_verifier = CmccAdmissionVerifier()
game_engine = GameEngine(credential_service, admission_verifier)
agent_runtime = AgentRuntime(game_engine)
logger = logging.getLogger(__name__)
scheduled_tasks: dict[str, asyncio.Task[None]] = {}


def _enabled_agent_ids() -> list[str]:
    with SessionLocal() as db:
        return list(
            db.scalars(
                select(Agent.id)
                .where(Agent.automation_enabled.is_(True))
                .order_by(Agent.id)
            ).all()
        )


def _run_scheduled_agent(agent_id: str) -> None:
    with SessionLocal() as db:
        try:
            agent_runtime.run(db, agent_id, source="SCHEDULER")
        except AgentRunInProgressError:
            return
        except Exception:
            db.rollback()
            logger.exception("Scheduled Agent run failed: %s", agent_id)


def _forget_scheduled_task(agent_id: str, task: asyncio.Task[None]) -> None:
    if scheduled_tasks.get(agent_id) is task:
        scheduled_tasks.pop(agent_id, None)


async def scheduler_loop() -> None:
    while True:
        await asyncio.sleep(settings.scheduler_interval_seconds)
        agent_ids = await asyncio.to_thread(_enabled_agent_ids)
        for agent_id in agent_ids:
            current = scheduled_tasks.get(agent_id)
            if current is not None and not current.done():
                continue
            task = asyncio.create_task(
                asyncio.to_thread(_run_scheduled_agent, agent_id),
                name=f"agent-scheduler-{agent_id}",
            )
            scheduled_tasks[agent_id] = task
            task.add_done_callback(
                lambda completed, scheduled_agent_id=agent_id: (
                    _forget_scheduled_task(scheduled_agent_id, completed)
                )
            )


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
        running_tasks = list(scheduled_tasks.values())
        for task in running_tasks:
            task.cancel()
        if running_tasks:
            await asyncio.gather(*running_tasks, return_exceptions=True)
        scheduled_tasks.clear()


app = FastAPI(
    title=settings.app_name,
    version="1.1.0",
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
        "admission": admission_verifier.descriptor(),
        "schedulerIntervalSeconds": settings.scheduler_interval_seconds,
        "agentRuntime": runtime_descriptor(),
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
    if admission_verifier.applies_to(owner.agent.id):
        admission_verifier.clear(owner.agent.id)
        admission = admission_verifier.verify(owner.agent.id, force=True)
        owner.agent.automation_enabled = admission.allowed
        db.commit()
    db.expire_all()
    return dashboard(db, owner_id)


def _load_admission_agent(agent_id: str, db: Session) -> Agent:
    agent = db.scalar(
        select(Agent)
        .where(Agent.id == agent_id)
        .options(selectinload(Agent.credential))
    )
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    return agent


def _admission_payload(agent: Agent, *, force: bool) -> dict:
    credential_status = (
        agent.credential.status if agent.credential is not None else "MISSING"
    )
    if credential_status != "ACTIVE":
        payload = admission_verifier.snapshot(agent.id).as_dict()
        payload.update(
            {
                "allowed": False,
                "mode": "DENIED",
                "upstreamStatus": f"LOCAL_{credential_status}",
                "message": f"本地凭证状态为 {credential_status}，未调用中移准入接口",
                "cached": True,
            }
        )
        return payload
    decision = (
        admission_verifier.verify(agent.id, force=True)
        if force
        else admission_verifier.snapshot(agent.id)
    )
    return decision.as_dict()


@app.get("/api/agents/{agent_id}/admission")
def get_agent_admission(agent_id: str, db: Session = Depends(get_db)) -> dict:
    agent = _load_admission_agent(agent_id, db)
    return _admission_payload(agent, force=False)


@app.post("/api/agents/{agent_id}/admission/verify")
def verify_agent_admission(agent_id: str, db: Session = Depends(get_db)) -> dict:
    agent = _load_admission_agent(agent_id, db)
    return _admission_payload(agent, force=True)


@app.post("/api/agents/{agent_id}/run")
def run_agent(agent_id: str, db: Session = Depends(get_db)) -> dict:
    agent = db.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    try:
        execution = agent_runtime.run(db, agent_id, source="MANUAL")
    except AgentRunInProgressError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "actionCount": len(execution.actions),
        "traceIds": [action.trace_id for action in execution.actions],
        "run": {
            "id": execution.run.id,
            "status": execution.run.status,
            "runtimeMode": execution.run.runtime_mode,
            "provider": execution.run.provider,
            "model": execution.run.model_name,
        },
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
        admission_verifier.clear()
    return {"status": "reset", "defaultOwnerId": "owner-lin"}
