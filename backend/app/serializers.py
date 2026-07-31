import json
from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from .game import CROPS, serialize_crop_stage
from .model_provider import runtime_descriptor
from .models import Agent, AgentAction, AgentRun, Farm, Owner, utcnow


def iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") + "Z" if value else None


def serialize_owner(owner: Owner) -> dict:
    credential = owner.agent.credential if owner.agent else None
    return {
        "id": owner.id,
        "nickname": owner.nickname,
        "phoneMasked": owner.phone_masked,
        "did": owner.did,
        "accent": owner.accent,
        "agent": {
            "id": owner.agent.id,
            "name": owner.agent.name,
            "automationEnabled": owner.agent.automation_enabled,
            "credentialStatus": credential.status if credential else "MISSING",
        },
    }


def serialize_plot(plot) -> dict:
    stage, progress = serialize_crop_stage(plot)
    crop_config = CROPS.get(plot.crop_type or "", {})
    return {
        "id": plot.id,
        "position": plot.position,
        "cropType": plot.crop_type,
        "cropName": crop_config.get("name"),
        "stage": stage,
        "progress": progress,
        "yieldRemaining": plot.yield_remaining,
        "yieldTotal": plot.yield_total,
        "plantedAt": iso(plot.planted_at),
        "maturesAt": iso(plot.matures_at),
    }


def serialize_farm(farm: Farm) -> dict:
    return {
        "id": farm.id,
        "name": farm.name,
        "coins": farm.coins,
        "plots": [serialize_plot(plot) for plot in farm.plots],
        "inventory": [
            {
                "cropType": item.crop_type,
                "cropName": CROPS[item.crop_type]["name"],
                "quantity": item.quantity,
            }
            for item in sorted(farm.inventory, key=lambda item: item.crop_type)
        ],
    }


def serialize_action(action: AgentAction, owner_id: str, db: Session) -> dict:
    actor = db.get(Agent, action.actor_agent_id)
    target = db.get(Owner, action.target_owner_id)
    is_incoming = (
        action.target_owner_id == owner_id and action.actor_owner_id != owner_id
    )
    after_state = json.loads(action.after_state or "{}")
    return {
        "id": action.id,
        "traceId": action.trace_id,
        "agentId": action.actor_agent_id,
        "agentName": actor.name if actor else "未知智能体",
        "targetOwnerId": action.target_owner_id,
        "targetOwnerName": target.nickname if target else "未知主人",
        "actionType": action.action_type,
        "status": action.status,
        "reason": action.reason,
        "credentialStatus": action.credential_status,
        "cropType": action.crop_type,
        "cropName": CROPS.get(action.crop_type or "", {}).get("name"),
        "quantity": action.quantity,
        "source": action.source,
        "executionMode": (
            "OPENCLAW"
            if action.source == "OPENCLAW"
            else "LLM"
            if action.source.startswith("LLM_")
            else "RULES"
        ),
        "isIncoming": is_incoming,
        "beforeState": json.loads(action.before_state or "{}"),
        "afterState": after_state,
        "agentRunId": after_state.get("agentRunId"),
        "admissionMode": after_state.get("admissionMode", "LOCAL"),
        "admissionUpstreamStatus": after_state.get(
            "admissionUpstreamStatus", "LOCAL"
        ),
        "createdAt": iso(action.created_at),
    }


def dashboard(db: Session, owner_id: str) -> dict | None:
    owner = db.scalar(
        select(Owner)
        .where(Owner.id == owner_id)
        .options(
            selectinload(Owner.agent).selectinload(Agent.credential),
            selectinload(Owner.agent).selectinload(Agent.credential_events),
            selectinload(Owner.farm).selectinload(Farm.plots),
            selectinload(Owner.farm).selectinload(Farm.inventory),
        )
    )
    if owner is None:
        return None
    agent = owner.agent
    credential = agent.credential
    neighbors = db.scalars(
        select(Owner)
        .where(Owner.id != owner_id)
        .options(
            selectinload(Owner.agent).selectinload(Agent.credential),
            selectinload(Owner.farm).selectinload(Farm.plots),
            selectinload(Owner.farm).selectinload(Farm.inventory),
        )
        .order_by(Owner.nickname)
    ).all()
    actions = db.scalars(
        select(AgentAction)
        .where(
            or_(
                AgentAction.actor_owner_id == owner_id,
                AgentAction.target_owner_id == owner_id,
            )
        )
        .order_by(AgentAction.created_at.desc())
        .limit(120)
    ).all()
    events = sorted(agent.credential_events, key=lambda event: event.created_at)
    runs = db.scalars(
        select(AgentRun)
        .where(AgentRun.agent_id == agent.id)
        .order_by(AgentRun.started_at.desc())
        .limit(12)
    ).all()

    return {
        "owner": serialize_owner(owner),
        "agent": {
            "id": agent.id,
            "name": agent.name,
            "clawId": agent.claw_id,
            "platformName": agent.platform_name,
            "description": agent.description,
            "automationEnabled": agent.automation_enabled,
            "lastRunAt": iso(agent.last_run_at),
        },
        "runtime": runtime_descriptor(),
        "recentRuns": [
            {
                "id": run.id,
                "triggerSource": run.trigger_source,
                "runtimeMode": run.runtime_mode,
                "provider": run.provider,
                "model": run.model_name,
                "status": run.status,
                "credentialStatus": run.credential_status,
                "toolCallCount": run.tool_call_count,
                "decisionSummary": run.decision_summary,
                "errorMessage": run.error_message,
                "startedAt": iso(run.started_at),
                "completedAt": iso(run.completed_at),
            }
            for run in runs
        ],
        "credential": (
            {
                "provider": credential.provider,
                "templateId": credential.template_id,
                "aic": credential.aic,
                "vcRecordId": credential.vc_record_id,
                "issueMode": credential.issue_mode,
                "status": credential.status,
                "issuedAt": iso(credential.issued_at),
                "expiresAt": iso(credential.expires_at),
                "lastVerifiedAt": iso(credential.last_verified_at),
            }
            if credential
            else None
        ),
        "credentialEvents": [
            {
                "id": event.id,
                "step": event.step,
                "status": event.status,
                "detail": event.detail,
                "createdAt": iso(event.created_at),
            }
            for event in events
        ],
        "farm": serialize_farm(owner.farm),
        "neighbors": [
            {
                "owner": serialize_owner(neighbor),
                "farm": serialize_farm(neighbor.farm),
            }
            for neighbor in neighbors
        ],
        "actions": [serialize_action(action, owner_id, db) for action in actions],
        "cropCatalog": [
            {
                "type": crop_type,
                "name": config["name"],
                "growthSeconds": config["growth_seconds"],
                "yield": config["yield"],
                "value": config["value"],
            }
            for crop_type, config in CROPS.items()
        ],
        "serverTime": iso(utcnow()),
    }
