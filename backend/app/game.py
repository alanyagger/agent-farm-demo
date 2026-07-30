from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .admission import CmccAdmissionVerifier
from .credentials import CredentialService
from .models import Agent, AgentAction, Farm, Inventory, Plot, utcnow


CROPS = {
    "CARROT": {"name": "胡萝卜", "growth_seconds": 12, "yield": 4, "value": 3},
    "TOMATO": {"name": "番茄", "growth_seconds": 18, "yield": 5, "value": 5},
    "CORN": {"name": "玉米", "growth_seconds": 24, "yield": 6, "value": 7},
}
CROP_ORDER = ("CARROT", "TOMATO", "CORN")


@dataclass(frozen=True)
class SkillOutcome:
    action: AgentAction
    success: bool
    detail: dict[str, Any]

    def as_tool_result(self) -> dict[str, Any]:
        return {
            "ok": self.success,
            "action": self.action.action_type,
            "status": self.action.status,
            "reason": self.action.reason,
            "traceId": self.action.trace_id,
            **self.detail,
        }


class GameEngine:
    """Deterministic farm rules and the only write path exposed to agent Skills."""

    def __init__(
        self,
        credential_service: CredentialService,
        admission_verifier: CmccAdmissionVerifier | None = None,
    ) -> None:
        self.credential_service = credential_service
        self.admission_verifier = admission_verifier or CmccAdmissionVerifier()
        self.lock = threading.RLock()

    @staticmethod
    def _inventory(db: Session, farm_id: str, crop_type: str) -> Inventory:
        item = db.scalar(
            select(Inventory).where(
                Inventory.farm_id == farm_id,
                Inventory.crop_type == crop_type,
            )
        )
        if item is None:
            item = Inventory(farm_id=farm_id, crop_type=crop_type, quantity=0)
            db.add(item)
            db.flush()
        return item

    def _record(
        self,
        db: Session,
        *,
        agent: Agent,
        target_owner_id: str,
        action_type: str,
        status: str,
        reason: str,
        credential_status: str,
        source: str,
        plot: Plot | None = None,
        crop_type: str | None = None,
        quantity: int = 0,
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
        run_id: str | None = None,
    ) -> AgentAction:
        after_state = dict(after or {})
        admission = self.admission_verifier.snapshot(agent.id)
        after_state.setdefault("admissionMode", admission.mode)
        after_state.setdefault("admissionUpstreamStatus", admission.upstream_status)
        if run_id:
            after_state["agentRunId"] = run_id
        action = AgentAction(
            trace_id=f"TR-{uuid.uuid4().hex[:16].upper()}",
            actor_agent_id=agent.id,
            actor_owner_id=agent.owner_id,
            target_owner_id=target_owner_id,
            plot_id=plot.id if plot else None,
            action_type=action_type,
            status=status,
            reason=reason[:320],
            credential_status=credential_status,
            crop_type=crop_type,
            quantity=quantity,
            source=source,
            before_state=json.dumps(before or {}, ensure_ascii=False),
            after_state=json.dumps(after_state, ensure_ascii=False),
        )
        db.add(action)
        db.flush()
        return action

    @staticmethod
    def _credential_reason(status: str) -> str:
        reason_map = {
            "MISSING": "未发现中移互联网智能体身份凭证，游戏准入被拒绝",
            "PENDING": "智能体身份凭证仍在签发中，暂不能执行游戏行为",
            "REVOKED": "智能体身份凭证已吊销，游戏准入被拒绝",
            "EXPIRED": "智能体身份凭证已过期，游戏准入被拒绝",
            "REJECTED": "智能体身份凭证签发被拒绝，游戏准入被拒绝",
        }
        return reason_map.get(status, "凭证状态无效")

    @staticmethod
    def _load_agent(db: Session, agent_id: str) -> Agent:
        agent = db.scalar(
            select(Agent)
            .where(Agent.id == agent_id)
            .options(selectinload(Agent.owner), selectinload(Agent.credential))
        )
        if agent is None:
            raise ValueError("Agent not found")
        return agent

    @staticmethod
    def _load_farm(db: Session, owner_id: str) -> Farm:
        farm = db.scalar(
            select(Farm)
            .where(Farm.owner_id == owner_id)
            .options(selectinload(Farm.plots), selectinload(Farm.inventory))
        )
        if farm is None:
            raise ValueError("Farm not found")
        return farm

    def _verify_active(
        self,
        db: Session,
        *,
        agent: Agent,
        source: str,
        run_id: str | None = None,
    ) -> tuple[str, AgentAction | None]:
        credential_status, _ = self.credential_service.verify(db, agent)
        if credential_status == "ACTIVE":
            admission = self.admission_verifier.verify(agent.id)
            if admission.allowed:
                return credential_status, None
            blocked = self._record(
                db,
                agent=agent,
                target_owner_id=agent.owner_id,
                action_type="ACCESS",
                status="BLOCKED",
                reason=admission.message,
                credential_status="REJECTED",
                source=source,
                after={
                    "admissionMode": admission.mode,
                    "admissionUpstreamStatus": admission.upstream_status,
                },
                run_id=run_id,
            )
            return "REJECTED", blocked
        uses_cmcc_admission = self.admission_verifier.applies_to(agent.id)
        admission_mode = "DENIED" if uses_cmcc_admission else "LOCAL"
        admission_status = (
            f"LOCAL_{credential_status}"
            if uses_cmcc_admission
            else credential_status
        )
        reason = self._credential_reason(credential_status)
        if uses_cmcc_admission and credential_status == "MISSING":
            reason = "尚未申领中移互联网智能体身份凭证，不能参与农场协作"
        blocked = self._record(
            db,
            agent=agent,
            target_owner_id=agent.owner_id,
            action_type="ACCESS",
            status="BLOCKED",
            reason=reason,
            credential_status=credential_status,
            source=source,
            after={
                "admissionMode": admission_mode,
                "admissionUpstreamStatus": admission_status,
            },
            run_id=run_id,
        )
        return credential_status, blocked

    def verify_agent_access(
        self,
        db: Session,
        agent_id: str,
        source: str,
        run_id: str | None = None,
    ) -> tuple[Agent, str, AgentAction | None]:
        """Graph entry-point gate. Skill writes repeat this check defensively."""
        with self.lock:
            agent = self._load_agent(db, agent_id)
            status, blocked = self._verify_active(
                db, agent=agent, source=source, run_id=run_id
            )
            return agent, status, blocked

    def record_event(
        self,
        db: Session,
        *,
        agent_id: str,
        action_type: str,
        status: str,
        reason: str,
        source: str,
        credential_status: str = "ACTIVE",
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
        run_id: str | None = None,
    ) -> AgentAction:
        with self.lock:
            agent = self._load_agent(db, agent_id)
            return self._record(
                db,
                agent=agent,
                target_owner_id=agent.owner_id,
                action_type=action_type,
                status=status,
                reason=reason,
                credential_status=credential_status,
                source=source,
                before=before,
                after=after,
                run_id=run_id,
            )

    def skill_snapshot(self, db: Session, agent_id: str) -> dict[str, Any]:
        """Return the model-safe state: no DID, phone number, token, or raw DB data."""
        with self.lock:
            agent = self._load_agent(db, agent_id)
            farm = self._load_farm(db, agent.owner_id)
            now = utcnow()
            return {
                "farm": {
                    "id": farm.id,
                    "name": farm.name,
                    "coins": farm.coins,
                    "plots": [
                        {
                            "id": plot.id,
                            "position": plot.position + 1,
                            "cropType": plot.crop_type,
                            "cropName": CROPS.get(plot.crop_type or "", {}).get("name"),
                            "mature": bool(
                                plot.crop_type
                                and plot.matures_at
                                and plot.matures_at <= now
                                and plot.yield_remaining > 0
                            ),
                            "yieldRemaining": plot.yield_remaining,
                        }
                        for plot in farm.plots
                    ],
                    "inventory": {
                        item.crop_type: item.quantity for item in farm.inventory
                    },
                },
                "cropCatalog": [
                    {
                        "type": crop_type,
                        "name": config["name"],
                        "growthSeconds": config["growth_seconds"],
                    }
                    for crop_type, config in CROPS.items()
                ],
            }

    def skill_neighbors(self, db: Session, agent_id: str) -> list[dict[str, Any]]:
        with self.lock:
            agent = self._load_agent(db, agent_id)
            now = utcnow()
            farms = db.scalars(
                select(Farm)
                .where(Farm.owner_id != agent.owner_id)
                .options(selectinload(Farm.owner), selectinload(Farm.plots))
                .order_by(Farm.name)
            ).all()
            return [
                {
                    "farmId": farm.id,
                    "farmName": farm.name,
                    "ownerNickname": farm.owner.nickname,
                    "plots": [
                        {
                            "id": plot.id,
                            "position": plot.position + 1,
                            "cropType": plot.crop_type,
                            "cropName": CROPS.get(plot.crop_type or "", {}).get("name"),
                            "canSteal": bool(
                                plot.crop_type
                                and plot.matures_at
                                and plot.matures_at <= now
                                and plot.yield_remaining > 1
                            ),
                            "yieldRemaining": plot.yield_remaining,
                        }
                        for plot in farm.plots
                    ],
                }
                for farm in farms
            ]

    def skill_recent_actions(
        self, db: Session, agent_id: str, limit: int = 5
    ) -> list[dict[str, Any]]:
        with self.lock:
            actions = db.scalars(
                select(AgentAction)
                .where(AgentAction.actor_agent_id == agent_id)
                .order_by(AgentAction.created_at.desc())
                .limit(max(1, min(limit, 10)))
            ).all()
            return [
                {
                    "action": action.action_type,
                    "status": action.status,
                    "reason": action.reason,
                    "cropType": action.crop_type,
                    "quantity": action.quantity,
                    "createdAt": action.created_at.isoformat(timespec="seconds") + "Z",
                }
                for action in actions
            ]

    def _write_access(
        self,
        db: Session,
        *,
        agent_id: str,
        source: str,
        run_id: str | None,
    ) -> tuple[Agent, str, AgentAction | None]:
        agent = self._load_agent(db, agent_id)
        status, blocked = self._verify_active(
            db, agent=agent, source=source, run_id=run_id
        )
        return agent, status, blocked

    def _rejected_outcome(
        self,
        db: Session,
        *,
        agent: Agent,
        action_type: str,
        reason: str,
        credential_status: str,
        source: str,
        run_id: str | None,
        plot: Plot | None = None,
        crop_type: str | None = None,
    ) -> SkillOutcome:
        action = self._record(
            db,
            agent=agent,
            target_owner_id=agent.owner_id,
            action_type=action_type,
            status="REJECTED",
            reason=reason,
            credential_status=credential_status,
            source=source,
            plot=plot,
            crop_type=crop_type,
            run_id=run_id,
        )
        return SkillOutcome(action, False, {"message": reason})

    def harvest_crop(
        self,
        db: Session,
        *,
        agent_id: str,
        plot_id: str,
        source: str,
        run_id: str | None = None,
    ) -> SkillOutcome:
        with self.lock:
            agent, status, blocked = self._write_access(
                db, agent_id=agent_id, source=source, run_id=run_id
            )
            if blocked:
                return SkillOutcome(blocked, False, {"message": blocked.reason})
            farm = self._load_farm(db, agent.owner_id)
            plot = next((item for item in farm.plots if item.id == plot_id), None)
            if plot is None:
                return self._rejected_outcome(
                    db,
                    agent=agent,
                    action_type="HARVEST",
                    reason="该地块不属于当前智能体的农场",
                    credential_status=status,
                    source=source,
                    run_id=run_id,
                )
            now = utcnow()
            if not (
                plot.crop_type
                and plot.matures_at
                and plot.matures_at <= now
                and plot.yield_remaining > 0
            ):
                return self._rejected_outcome(
                    db,
                    agent=agent,
                    action_type="HARVEST",
                    reason="地块没有可收获的成熟作物",
                    credential_status=status,
                    source=source,
                    run_id=run_id,
                    plot=plot,
                )
            crop_type = plot.crop_type
            quantity = plot.yield_remaining
            inventory = self._inventory(db, farm.id, crop_type)
            before = {"crop": crop_type, "yieldRemaining": quantity}
            inventory.quantity += quantity
            farm.coins += quantity * CROPS[crop_type]["value"]
            plot.crop_type = None
            plot.planted_at = None
            plot.matures_at = None
            plot.yield_total = 0
            plot.yield_remaining = 0
            action = self._record(
                db,
                agent=agent,
                target_owner_id=agent.owner_id,
                action_type="HARVEST",
                status="SUCCESS",
                reason="Skill 已核验成熟状态并完成收获",
                credential_status=status,
                source=source,
                plot=plot,
                crop_type=crop_type,
                quantity=quantity,
                before=before,
                after={"inventory": inventory.quantity, "coins": farm.coins},
                run_id=run_id,
            )
            return SkillOutcome(
                action,
                True,
                {"cropType": crop_type, "quantity": quantity, "coins": farm.coins},
            )

    def plant_crop(
        self,
        db: Session,
        *,
        agent_id: str,
        plot_id: str,
        crop_type: str,
        source: str,
        run_id: str | None = None,
    ) -> SkillOutcome:
        with self.lock:
            agent, status, blocked = self._write_access(
                db, agent_id=agent_id, source=source, run_id=run_id
            )
            if blocked:
                return SkillOutcome(blocked, False, {"message": blocked.reason})
            farm = self._load_farm(db, agent.owner_id)
            plot = next((item for item in farm.plots if item.id == plot_id), None)
            normalized_crop = crop_type.upper()
            if normalized_crop not in CROPS:
                return self._rejected_outcome(
                    db,
                    agent=agent,
                    action_type="PLANT",
                    reason="作物类型不在允许的农场作物目录中",
                    credential_status=status,
                    source=source,
                    run_id=run_id,
                )
            if plot is None:
                return self._rejected_outcome(
                    db,
                    agent=agent,
                    action_type="PLANT",
                    reason="该地块不属于当前智能体的农场",
                    credential_status=status,
                    source=source,
                    run_id=run_id,
                    crop_type=normalized_crop,
                )
            if plot.crop_type is not None:
                return self._rejected_outcome(
                    db,
                    agent=agent,
                    action_type="PLANT",
                    reason="地块当前不是空地，不能覆盖种植",
                    credential_status=status,
                    source=source,
                    run_id=run_id,
                    plot=plot,
                    crop_type=normalized_crop,
                )
            now = utcnow()
            config = CROPS[normalized_crop]
            plot.crop_type = normalized_crop
            plot.planted_at = now
            plot.matures_at = now + timedelta(seconds=config["growth_seconds"])
            plot.yield_total = config["yield"]
            plot.yield_remaining = config["yield"]
            action = self._record(
                db,
                agent=agent,
                target_owner_id=agent.owner_id,
                action_type="PLANT",
                status="SUCCESS",
                reason="Skill 已确认空地并完成种植",
                credential_status=status,
                source=source,
                plot=plot,
                crop_type=normalized_crop,
                quantity=1,
                before={"crop": None},
                after={"crop": normalized_crop, "maturesAt": plot.matures_at.isoformat()},
                run_id=run_id,
            )
            return SkillOutcome(
                action,
                True,
                {"cropType": normalized_crop, "maturesAt": plot.matures_at.isoformat()},
            )

    def steal_crop(
        self,
        db: Session,
        *,
        agent_id: str,
        plot_id: str,
        source: str,
        run_id: str | None = None,
    ) -> SkillOutcome:
        with self.lock:
            agent, status, blocked = self._write_access(
                db, agent_id=agent_id, source=source, run_id=run_id
            )
            if blocked:
                return SkillOutcome(blocked, False, {"message": blocked.reason})
            farm = self._load_farm(db, agent.owner_id)
            target_plot = db.scalar(
                select(Plot)
                .where(Plot.id == plot_id)
                .options(selectinload(Plot.farm))
            )
            if target_plot is None or target_plot.farm.owner_id == agent.owner_id:
                return self._rejected_outcome(
                    db,
                    agent=agent,
                    action_type="STEAL",
                    reason="只能采摘邻居农场中符合条件的地块",
                    credential_status=status,
                    source=source,
                    run_id=run_id,
                )
            cooldown_cutoff = utcnow() - timedelta(seconds=18)
            recently_stolen = db.scalar(
                select(AgentAction.id).where(
                    AgentAction.actor_agent_id == agent.id,
                    AgentAction.action_type == "STEAL",
                    AgentAction.status == "SUCCESS",
                    AgentAction.plot_id == target_plot.id,
                    AgentAction.created_at >= cooldown_cutoff,
                )
            )
            if recently_stolen is not None:
                return self._rejected_outcome(
                    db,
                    agent=agent,
                    action_type="STEAL",
                    reason="该邻居地块仍在社交采摘冷却时间内",
                    credential_status=status,
                    source=source,
                    run_id=run_id,
                    plot=target_plot,
                )
            now = utcnow()
            if not (
                target_plot.crop_type
                and target_plot.matures_at
                and target_plot.matures_at <= now
                and target_plot.yield_remaining > 1
            ):
                return self._rejected_outcome(
                    db,
                    agent=agent,
                    action_type="STEAL",
                    reason="邻居作物未成熟或剩余产量不足，不能采摘",
                    credential_status=status,
                    source=source,
                    run_id=run_id,
                    plot=target_plot,
                )
            crop_type = target_plot.crop_type
            before_amount = target_plot.yield_remaining
            target_plot.yield_remaining -= 1
            inventory = self._inventory(db, farm.id, crop_type)
            inventory.quantity += 1
            action = self._record(
                db,
                agent=agent,
                target_owner_id=target_plot.farm.owner_id,
                action_type="STEAL",
                status="SUCCESS",
                reason="Skill 已核验成熟状态、保留产量与冷却时间",
                credential_status=status,
                source=source,
                plot=target_plot,
                crop_type=crop_type,
                quantity=1,
                before={"yieldRemaining": before_amount},
                after={"yieldRemaining": target_plot.yield_remaining},
                run_id=run_id,
            )
            return SkillOutcome(
                action,
                True,
                {"cropType": crop_type, "quantity": 1, "yieldRemaining": target_plot.yield_remaining},
            )

    def run_agent(
        self,
        db: Session,
        agent_id: str,
        source: str = "MANUAL",
        run_id: str | None = None,
    ) -> list[AgentAction]:
        """The original deterministic mode, retained for offline demonstration."""
        with self.lock:
            agent = self._load_agent(db, agent_id)
            credential_status, blocked = self._verify_active(
                db, agent=agent, source=source, run_id=run_id
            )
            if blocked:
                agent.last_run_at = utcnow()
                db.commit()
                return [blocked]

            now = utcnow()
            farm = self._load_farm(db, agent.owner_id)
            actions: list[AgentAction] = []

            mature_plots = [
                plot
                for plot in farm.plots
                if plot.crop_type
                and plot.matures_at
                and plot.matures_at <= now
                and plot.yield_remaining > 0
            ]
            for plot in mature_plots[:1]:
                crop_type = plot.crop_type
                quantity = plot.yield_remaining
                inventory = self._inventory(db, farm.id, crop_type)
                inventory.quantity += quantity
                farm.coins += quantity * CROPS[crop_type]["value"]
                before = {"crop": crop_type, "yieldRemaining": quantity}
                plot.crop_type = None
                plot.planted_at = None
                plot.matures_at = None
                plot.yield_total = 0
                plot.yield_remaining = 0
                actions.append(
                    self._record(
                        db,
                        agent=agent,
                        target_owner_id=agent.owner_id,
                        action_type="HARVEST",
                        status="SUCCESS",
                        reason="检测到自家成熟作物，优先完成收获",
                        credential_status=credential_status,
                        source=source,
                        plot=plot,
                        crop_type=crop_type,
                        quantity=quantity,
                        before=before,
                        after={"inventory": inventory.quantity, "coins": farm.coins},
                        run_id=run_id,
                    )
                )

            empty_plots = [plot for plot in farm.plots if plot.crop_type is None]
            for plot in empty_plots[:2]:
                crop_type = CROP_ORDER[
                    (plot.position + sum(ord(char) for char in agent.id)) % len(CROP_ORDER)
                ]
                config = CROPS[crop_type]
                plot.crop_type = crop_type
                plot.planted_at = now
                plot.matures_at = now + timedelta(seconds=config["growth_seconds"])
                plot.yield_total = config["yield"]
                plot.yield_remaining = config["yield"]
                actions.append(
                    self._record(
                        db,
                        agent=agent,
                        target_owner_id=agent.owner_id,
                        action_type="PLANT",
                        status="SUCCESS",
                        reason=f"发现第 {plot.position + 1} 块空地，按轮作策略补种",
                        credential_status=credential_status,
                        source=source,
                        plot=plot,
                        crop_type=crop_type,
                        quantity=1,
                        before={"crop": None},
                        after={"crop": crop_type, "maturesAt": plot.matures_at.isoformat()},
                        run_id=run_id,
                    )
                )

            cooldown_cutoff = now - timedelta(seconds=18)
            recent_stolen_plot_ids = set(
                db.scalars(
                    select(AgentAction.plot_id).where(
                        AgentAction.actor_agent_id == agent.id,
                        AgentAction.action_type == "STEAL",
                        AgentAction.status == "SUCCESS",
                        AgentAction.created_at >= cooldown_cutoff,
                    )
                ).all()
            )
            steal_conditions = [
                Farm.owner_id != agent.owner_id,
                Plot.crop_type.is_not(None),
                Plot.matures_at <= now,
                Plot.yield_remaining > 1,
            ]
            if recent_stolen_plot_ids:
                steal_conditions.append(Plot.id.not_in(recent_stolen_plot_ids))
            steal_target = db.scalar(
                select(Plot)
                .join(Farm, Plot.farm_id == Farm.id)
                .where(*steal_conditions)
                .order_by(Plot.matures_at.asc(), Plot.position.asc())
                .limit(1)
            )
            if steal_target:
                target_farm = db.get(Farm, steal_target.farm_id)
                crop_type = steal_target.crop_type
                before_amount = steal_target.yield_remaining
                steal_target.yield_remaining -= 1
                inventory = self._inventory(db, farm.id, crop_type)
                inventory.quantity += 1
                actions.append(
                    self._record(
                        db,
                        agent=agent,
                        target_owner_id=target_farm.owner_id,
                        action_type="STEAL",
                        status="SUCCESS",
                        reason="发现邻居成熟且仍保留安全产量的作物，执行一次社交采摘",
                        credential_status=credential_status,
                        source=source,
                        plot=steal_target,
                        crop_type=crop_type,
                        quantity=1,
                        before={"yieldRemaining": before_amount},
                        after={"yieldRemaining": steal_target.yield_remaining},
                        run_id=run_id,
                    )
                )

            if not actions and source == "MANUAL":
                actions.append(
                    self._record(
                        db,
                        agent=agent,
                        target_owner_id=agent.owner_id,
                        action_type="OBSERVE",
                        status="SUCCESS",
                        reason="本轮没有可收获、可补种或符合冷却规则的邻居作物",
                        credential_status=credential_status,
                        source=source,
                        run_id=run_id,
                    )
                )
            agent.last_run_at = now
            db.commit()
            return actions


def serialize_crop_stage(plot: Plot) -> tuple[str, int]:
    if not plot.crop_type or not plot.planted_at or not plot.matures_at:
        return "EMPTY", 0
    now = utcnow()
    total = max((plot.matures_at - plot.planted_at).total_seconds(), 1)
    elapsed = (now - plot.planted_at).total_seconds()
    progress = max(0, min(100, round(elapsed / total * 100)))
    if progress >= 100:
        return "MATURE", 100
    if progress >= 62:
        return "LEAF", progress
    return "SPROUT", progress
