from __future__ import annotations

import json
import threading
import uuid
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .credentials import CredentialService
from .models import (
    Agent,
    AgentAction,
    Farm,
    Inventory,
    Plot,
    utcnow,
)


CROPS = {
    "CARROT": {"name": "胡萝卜", "growth_seconds": 12, "yield": 4, "value": 3},
    "TOMATO": {"name": "番茄", "growth_seconds": 18, "yield": 5, "value": 5},
    "CORN": {"name": "玉米", "growth_seconds": 24, "yield": 6, "value": 7},
}
CROP_ORDER = ("CARROT", "TOMATO", "CORN")


class GameEngine:
    def __init__(self, credential_service: CredentialService) -> None:
        self.credential_service = credential_service
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

    @staticmethod
    def _record(
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
        before: dict | None = None,
        after: dict | None = None,
    ) -> AgentAction:
        action = AgentAction(
            trace_id=f"TR-{uuid.uuid4().hex[:16].upper()}",
            actor_agent_id=agent.id,
            actor_owner_id=agent.owner_id,
            target_owner_id=target_owner_id,
            plot_id=plot.id if plot else None,
            action_type=action_type,
            status=status,
            reason=reason,
            credential_status=credential_status,
            crop_type=crop_type,
            quantity=quantity,
            source=source,
            before_state=json.dumps(before or {}, ensure_ascii=False),
            after_state=json.dumps(after or {}, ensure_ascii=False),
        )
        db.add(action)
        return action

    def run_agent(
        self,
        db: Session,
        agent_id: str,
        source: str = "MANUAL",
    ) -> list[AgentAction]:
        with self.lock:
            agent = db.scalar(
                select(Agent)
                .where(Agent.id == agent_id)
                .options(
                    selectinload(Agent.owner),
                    selectinload(Agent.credential),
                )
            )
            if agent is None:
                raise ValueError("Agent not found")

            credential_status, _ = self.credential_service.verify(db, agent)
            if credential_status != "ACTIVE":
                reason_map = {
                    "MISSING": "未发现中移互联网智能体身份凭证，游戏准入被拒绝",
                    "PENDING": "智能体身份凭证仍在签发中，暂不能执行游戏行为",
                    "REVOKED": "智能体身份凭证已吊销，游戏准入被拒绝",
                    "EXPIRED": "智能体身份凭证已过期，游戏准入被拒绝",
                    "REJECTED": "智能体身份凭证签发被拒绝，游戏准入被拒绝",
                }
                blocked = self._record(
                    db,
                    agent=agent,
                    target_owner_id=agent.owner_id,
                    action_type="ACCESS",
                    status="BLOCKED",
                    reason=reason_map.get(credential_status, "凭证状态无效"),
                    credential_status=credential_status,
                    source=source,
                )
                agent.last_run_at = utcnow()
                db.commit()
                return [blocked]

            now = utcnow()
            farm = db.scalar(
                select(Farm)
                .where(Farm.owner_id == agent.owner_id)
                .options(selectinload(Farm.plots), selectinload(Farm.inventory))
            )
            if farm is None:
                raise ValueError("Farm not found")
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
                before = {
                    "crop": crop_type,
                    "yieldRemaining": plot.yield_remaining,
                }
                inventory = self._inventory(db, farm.id, crop_type)
                inventory.quantity += quantity
                farm.coins += quantity * CROPS[crop_type]["value"]
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
                        after={
                            "crop": crop_type,
                            "maturesAt": plot.matures_at.isoformat(),
                        },
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
