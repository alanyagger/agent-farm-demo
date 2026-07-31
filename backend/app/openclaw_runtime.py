from __future__ import annotations

import json
import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .agent_runtime import (
    AgentRunInProgressError,
    AgentRuntime,
    AgentTurnContext,
    build_farm_tools,
)
from .config import settings
from .game import GameEngine
from .models import Agent, AgentAction, AgentRun, utcnow


OPENCLAW_AGENT_IDS = {
    "青芽": "agent-sprout",
    "田小诺": "agent-nova",
    "禾光": "agent-orbit",
    "agent-sprout": "agent-sprout",
    "agent-nova": "agent-nova",
    "agent-orbit": "agent-orbit",
}
WRITE_TOOLS = {"harvest_crop", "plant_crop", "steal_crop"}
QUOTA_ACTIONS = {
    "HARVEST": "harvest",
    "PLANT": "plant",
    "STEAL": "steal",
}


class OpenClawRunError(RuntimeError):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


class OpenClawRunService:
    def __init__(self, game_engine: GameEngine, agent_runtime: AgentRuntime) -> None:
        self.game_engine = game_engine
        self.agent_runtime = agent_runtime

    @staticmethod
    def _trace_ids(run: AgentRun) -> list[str]:
        try:
            values = json.loads(run.action_trace_ids or "[]")
        except json.JSONDecodeError:
            return []
        return [str(value) for value in values if value]

    @staticmethod
    def _expires_at(run: AgentRun):
        return run.started_at + timedelta(
            seconds=max(30, settings.openclaw_farm_run_ttl_seconds)
        )

    @classmethod
    def _run_payload(
        cls,
        run: AgentRun,
        *,
        allowed: bool | None = None,
        message: str = "",
    ) -> dict[str, Any]:
        payload = {
            "runId": run.id,
            "agentId": run.agent_id,
            "status": run.status,
            "credentialStatus": run.credential_status,
            "toolCallCount": run.tool_call_count,
            "traceIds": cls._trace_ids(run),
            "instruction": cls._instruction_from_summary(run.decision_summary),
            "summary": cls._final_summary(run.decision_summary),
            "startedAt": run.started_at.isoformat(timespec="seconds") + "Z",
            "expiresAt": cls._expires_at(run).isoformat(timespec="seconds") + "Z",
            "completedAt": (
                run.completed_at.isoformat(timespec="seconds") + "Z"
                if run.completed_at
                else None
            ),
        }
        if allowed is not None:
            payload["allowed"] = allowed
        if message:
            payload["message"] = message
        return payload

    @staticmethod
    def _instruction_from_summary(value: str) -> str:
        first_line = (value or "").splitlines()[0]
        return first_line.removeprefix("用户指令：").strip()

    @staticmethod
    def _final_summary(value: str) -> str:
        marker = "\n执行总结："
        return value.split(marker, 1)[1].strip() if marker in value else ""

    def list_agents(self, db: Session) -> dict[str, Any]:
        agents = db.scalars(
            select(Agent)
            .where(Agent.id.in_(OPENCLAW_AGENT_IDS.values()))
            .options(selectinload(Agent.credential))
            .order_by(Agent.id)
        ).all()
        order = {"agent-sprout": 0, "agent-nova": 1, "agent-orbit": 2}
        items = []
        for agent in sorted(agents, key=lambda item: order.get(item.id, 99)):
            credential_status = (
                agent.credential.status if agent.credential is not None else "MISSING"
            )
            items.append(
                {
                    "agentId": agent.id,
                    "agentName": agent.name,
                    "credentialStatus": credential_status,
                    "available": credential_status == "ACTIVE",
                    "busy": self.agent_runtime.is_running(agent.id),
                    "default": agent.id == "agent-sprout",
                }
            )
        return {"ok": True, "agents": items, "defaultAgentId": "agent-sprout"}

    def begin(
        self,
        db: Session,
        *,
        agent_id: str,
        instruction: str,
    ) -> dict[str, Any]:
        normalized_agent_id = OPENCLAW_AGENT_IDS.get(agent_id.strip(), agent_id.strip())
        agent = db.get(Agent, normalized_agent_id)
        if agent is None or normalized_agent_id not in set(OPENCLAW_AGENT_IDS.values()):
            raise OpenClawRunError("未知农场智能体", 404)

        self.expire_runs(db)
        try:
            self.agent_runtime.claim_agent(normalized_agent_id)
        except AgentRunInProgressError as exc:
            raise OpenClawRunError(str(exc), 409) from exc

        run = AgentRun(
            id=f"OCRUN-{uuid.uuid4().hex[:16].upper()}",
            agent_id=normalized_agent_id,
            trigger_source="OPENCLAW",
            runtime_mode="OPENCLAW",
            provider="openclaw",
            model_name=settings.openclaw_model_label,
            decision_summary=f"用户指令：{instruction.strip()[:800]}",
        )
        try:
            db.add(run)
            db.commit()
            _, credential_status, blocked = self.game_engine.verify_agent_access(
                db,
                normalized_agent_id,
                source="OPENCLAW",
                run_id=run.id,
            )
            run.credential_status = credential_status
            if blocked is not None:
                run.status = "BLOCKED"
                run.action_trace_ids = json.dumps([blocked.trace_id])
                run.completed_at = utcnow()
                db.commit()
                self.agent_runtime.release_agent(normalized_agent_id)
                return {
                    "ok": False,
                    **self._run_payload(
                        run,
                        allowed=False,
                        message=blocked.reason,
                    ),
                }
            db.commit()
            return {
                "ok": True,
                **self._run_payload(
                    run,
                    allowed=True,
                    message=f"{agent.name} 已通过本地 Mock 凭证校验",
                ),
            }
        except Exception as exc:
            db.rollback()
            persisted_run = db.get(AgentRun, run.id)
            if persisted_run is not None and persisted_run.status == "RUNNING":
                persisted_run.status = "FAILED"
                persisted_run.error_message = str(exc).replace("\n", " ")[:320]
                persisted_run.completed_at = utcnow()
                db.commit()
            self.agent_runtime.release_agent(normalized_agent_id)
            raise

    def _load_run(self, db: Session, run_id: str) -> AgentRun:
        run = db.get(AgentRun, run_id)
        if run is None or run.runtime_mode != "OPENCLAW":
            raise OpenClawRunError("OpenClaw 运行不存在", 404)
        if run.status != "RUNNING":
            raise OpenClawRunError(
                f"OpenClaw 运行已结束，当前状态为 {run.status}",
                409,
            )
        if self._expires_at(run) <= utcnow():
            self._expire_run(db, run)
            raise OpenClawRunError("OpenClaw 运行已超时", 409)
        return run

    def _actions_for_run(self, db: Session, run: AgentRun) -> list[AgentAction]:
        trace_ids = self._trace_ids(run)
        if not trace_ids:
            return []
        actions = db.scalars(
            select(AgentAction).where(AgentAction.trace_id.in_(trace_ids))
        ).all()
        by_trace = {action.trace_id: action for action in actions}
        return [by_trace[trace_id] for trace_id in trace_ids if trace_id in by_trace]

    def _context(self, db: Session, run: AgentRun) -> AgentTurnContext:
        quotas = {"harvest": 0, "plant": 0, "steal": 0}
        for action in self._actions_for_run(db, run):
            quota = QUOTA_ACTIONS.get(action.action_type)
            if quota and action.status == "SUCCESS":
                quotas[quota] += 1
        return AgentTurnContext(
            db=db,
            engine=self.game_engine,
            agent_id=run.agent_id,
            run_id=run.id,
            action_source="OPENCLAW",
            credential_status=run.credential_status,
            quotas=quotas,
        )

    def call_tool(
        self,
        db: Session,
        *,
        run_id: str,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        run = self._load_run(db, run_id)
        context = self._context(db, run)
        tools = {tool.name: tool for tool in build_farm_tools(context)}
        selected = tools.get(tool_name)
        if selected is None:
            raise OpenClawRunError("农场 Skill 不在允许列表中", 404)

        try:
            result = selected.invoke(arguments or {})
        except Exception as exc:
            db.rollback()
            raise OpenClawRunError(f"农场 Skill 参数无效：{exc}", 422) from exc

        run.tool_call_count += 1
        trace_ids = self._trace_ids(run)
        trace_ids.extend(action.trace_id for action in context.actions)
        run.action_trace_ids = json.dumps(trace_ids)
        if context.actions:
            run.credential_status = context.actions[-1].credential_status
        agent = db.get(Agent, run.agent_id)
        if agent is not None:
            agent.last_run_at = utcnow()

        blocked = next(
            (action for action in context.actions if action.status == "BLOCKED"),
            None,
        )
        if blocked is not None:
            run.status = "BLOCKED"
            run.completed_at = utcnow()
            self.agent_runtime.release_agent(run.agent_id)
        db.commit()
        return {
            "ok": not bool(blocked),
            "tool": tool_name,
            "result": result,
            "run": self._run_payload(run),
        }

    def finish(self, db: Session, *, run_id: str, summary: str) -> dict[str, Any]:
        run = self._load_run(db, run_id)
        instruction = self._instruction_from_summary(run.decision_summary)
        run.status = "SUCCESS"
        run.decision_summary = (
            f"用户指令：{instruction}\n执行总结：{summary.strip()[:1000]}"
        )
        run.completed_at = utcnow()
        agent = db.get(Agent, run.agent_id)
        if agent is not None:
            agent.last_run_at = run.completed_at
        db.commit()
        self.agent_runtime.release_agent(run.agent_id)
        return {
            "ok": True,
            "message": "OpenClaw 农场任务已完成",
            "run": self._run_payload(run),
        }

    def _expire_run(self, db: Session, run: AgentRun) -> None:
        run.status = "EXPIRED"
        run.error_message = "OpenClaw 运行超过允许时长，已自动结束"
        run.completed_at = utcnow()
        db.commit()
        self.agent_runtime.release_agent(run.agent_id)

    def expire_runs(self, db: Session) -> int:
        cutoff = utcnow() - timedelta(
            seconds=max(30, settings.openclaw_farm_run_ttl_seconds)
        )
        runs = db.scalars(
            select(AgentRun).where(
                AgentRun.runtime_mode == "OPENCLAW",
                AgentRun.status == "RUNNING",
                AgentRun.started_at <= cutoff,
            )
        ).all()
        for run in runs:
            self._expire_run(db, run)
        return len(runs)

    def recover_interrupted_runs(self, db: Session) -> int:
        runs = db.scalars(
            select(AgentRun).where(
                AgentRun.runtime_mode == "OPENCLAW",
                AgentRun.status == "RUNNING",
            )
        ).all()
        for run in runs:
            run.status = "FAILED"
            run.error_message = "后端服务已重启，本轮 OpenClaw 任务已安全终止"
            run.completed_at = utcnow()
        if runs:
            db.commit()
        return len(runs)

    def release_running_agents(self, db: Session) -> int:
        runs = db.scalars(
            select(AgentRun).where(
                AgentRun.runtime_mode == "OPENCLAW",
                AgentRun.status == "RUNNING",
            )
        ).all()
        for run in runs:
            self.agent_runtime.release_agent(run.agent_id)
        return len(runs)
