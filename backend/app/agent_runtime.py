from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from threading import Lock
from typing import Any, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool, tool
from langgraph.graph import END, START, StateGraph
from sqlalchemy.orm import Session

from .config import settings
from .game import GameEngine, SkillOutcome
from .model_provider import ModelProvider, ModelProviderError, build_model_provider
from .models import Agent, AgentAction, AgentRun, utcnow


SYSTEM_PROMPT = """你是一个受中移互联网智能体身份凭证保护的农场智能体。
你的任务是只通过授权的农场 Skills 管理主人农场：观察、收获、种植，必要时对邻居成熟作物做一次社交采摘。
先观察必要状态，再调用 Skill；不要编造地块 ID、作物状态或执行结果。
每轮最多收获一次、种植两次、采摘一次。服务端会再次校验凭证、额度、库存、冷却时间和地块归属。
不要尝试访问网络、文件、Shell、数据库或未提供的工具。完成后用一句中文说明本轮结果，不要输出内部推理过程。
"""


class AgentGraphState(TypedDict, total=False):
    messages: list[BaseMessage]
    credential_status: str
    blocked: bool
    tool_rounds: int
    tool_call_count: int
    decision_summary: str
    error_message: str


@dataclass
class AgentTurnContext:
    db: Session
    engine: GameEngine
    agent_id: str
    run_id: str
    action_source: str
    credential_status: str = "UNKNOWN"
    actions: list[AgentAction] = field(default_factory=list)
    quotas: dict[str, int] = field(
        default_factory=lambda: {"harvest": 0, "plant": 0, "steal": 0}
    )

    def add_action(self, action: AgentAction) -> AgentAction:
        self.actions.append(action)
        return action

    def quota_available(self, action: str) -> bool:
        limits = {"harvest": 1, "plant": 2, "steal": 1}
        return self.quotas[action] < limits[action]

    def capture_outcome(self, outcome: SkillOutcome, action: str) -> dict[str, Any]:
        self.add_action(outcome.action)
        if outcome.success:
            self.quotas[action] += 1
        return outcome.as_tool_result()

    def reject_quota(self, action: str, action_type: str) -> dict[str, Any]:
        event = self.engine.record_event(
            self.db,
            agent_id=self.agent_id,
            action_type=action_type,
            status="REJECTED",
            reason="当前 Agent 轮次已达到该 Skill 的安全执行额度",
            source=self.action_source,
            credential_status=self.credential_status,
            run_id=self.run_id,
        )
        self.add_action(event)
        return {
            "ok": False,
            "action": action_type,
            "status": "REJECTED",
            "reason": event.reason,
            "traceId": event.trace_id,
        }


@dataclass(frozen=True)
class AgentExecutionResult:
    run: AgentRun
    actions: list[AgentAction]


class AgentRunInProgressError(RuntimeError):
    pass


def _safe_error(error: Exception | str) -> str:
    text = str(error)
    if settings.deepseek_api_key:
        text = text.replace(settings.deepseek_api_key, "[redacted]")
    return text.replace("\n", " ")[:300]


def _summary_from_message(message: BaseMessage | None) -> str:
    if message is None:
        return "本轮没有产生可展示的模型总结。"
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content.strip()[:500] or "本轮已完成授权的农场操作。"
    return "本轮已完成授权的农场操作。"


def _safe_tool_arguments(arguments: Any) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        return {}
    return {
        str(key)[:48]: str(value)[:96]
        for key, value in arguments.items()
        if isinstance(value, (str, int, float, bool))
    }


def build_farm_tools(context: AgentTurnContext) -> list[BaseTool]:
    @tool
    def inspect_my_farm() -> dict[str, Any]:
        """读取主人农场、地块成熟情况、库存和可种植作物目录。"""
        return context.engine.skill_snapshot(context.db, context.agent_id)

    @tool
    def inspect_neighbors() -> list[dict[str, Any]]:
        """读取邻居农场中可供社交采摘的地块，不含任何身份敏感信息。"""
        return context.engine.skill_neighbors(context.db, context.agent_id)

    @tool
    def read_recent_actions() -> list[dict[str, Any]]:
        """读取当前智能体最近五条行为，用作短期工作记忆。"""
        return context.engine.skill_recent_actions(context.db, context.agent_id)

    @tool
    def harvest_crop(plot_id: str) -> dict[str, Any]:
        """收获主人农场指定的成熟地块。每轮最多一次，服务端会复核凭证和状态。"""
        if not context.quota_available("harvest"):
            return context.reject_quota("harvest", "HARVEST")
        outcome = context.engine.harvest_crop(
            context.db,
            agent_id=context.agent_id,
            plot_id=plot_id,
            source=context.action_source,
            run_id=context.run_id,
        )
        return context.capture_outcome(outcome, "harvest")

    @tool
    def plant_crop(plot_id: str, crop_type: str) -> dict[str, Any]:
        """在主人农场指定空地种植 CARROT、TOMATO 或 CORN。每轮最多两次。"""
        if not context.quota_available("plant"):
            return context.reject_quota("plant", "PLANT")
        outcome = context.engine.plant_crop(
            context.db,
            agent_id=context.agent_id,
            plot_id=plot_id,
            crop_type=crop_type,
            source=context.action_source,
            run_id=context.run_id,
        )
        return context.capture_outcome(outcome, "plant")

    @tool
    def steal_crop(plot_id: str) -> dict[str, Any]:
        """从邻居指定成熟地块采摘一份。每轮最多一次，服务端保留安全产量并核验冷却时间。"""
        if not context.quota_available("steal"):
            return context.reject_quota("steal", "STEAL")
        outcome = context.engine.steal_crop(
            context.db,
            agent_id=context.agent_id,
            plot_id=plot_id,
            source=context.action_source,
            run_id=context.run_id,
        )
        return context.capture_outcome(outcome, "steal")

    return [
        inspect_my_farm,
        inspect_neighbors,
        read_recent_actions,
        harvest_crop,
        plant_crop,
        steal_crop,
    ]


class AgentRuntime:
    def __init__(self, game_engine: GameEngine) -> None:
        self.game_engine = game_engine
        self._active_agents: set[str] = set()
        self._active_agents_lock = Lock()

    def is_running(self, agent_id: str) -> bool:
        with self._active_agents_lock:
            return agent_id in self._active_agents

    def claim_agent(self, agent_id: str) -> None:
        with self._active_agents_lock:
            if agent_id in self._active_agents:
                raise AgentRunInProgressError("该智能体已有一轮任务正在执行")
            self._active_agents.add(agent_id)

    def release_agent(self, agent_id: str) -> None:
        with self._active_agents_lock:
            self._active_agents.discard(agent_id)

    def _new_run(self, db: Session, agent_id: str, source: str) -> AgentRun:
        llm_mode = settings.llm_runtime_enabled
        run = AgentRun(
            id=f"RUN-{uuid.uuid4().hex[:16].upper()}",
            agent_id=agent_id,
            trigger_source=source,
            runtime_mode="LLM" if llm_mode else "RULES",
            provider=settings.model_provider if llm_mode else "rules",
            model_name=settings.deepseek_model if llm_mode else "deterministic-rules",
        )
        db.add(run)
        db.commit()
        return run

    @staticmethod
    def _finalize_run(
        db: Session,
        run: AgentRun,
        *,
        status: str,
        credential_status: str,
        actions: list[AgentAction],
        tool_call_count: int = 0,
        decision_summary: str = "",
        error_message: str = "",
    ) -> None:
        run.status = status
        run.credential_status = credential_status
        run.tool_call_count = tool_call_count
        run.action_trace_ids = json.dumps([action.trace_id for action in actions])
        run.decision_summary = decision_summary[:2000]
        run.error_message = error_message[:320]
        run.completed_at = utcnow()
        db.commit()

    def run(self, db: Session, agent_id: str, source: str = "MANUAL") -> AgentExecutionResult:
        if db.get(Agent, agent_id) is None:
            raise ValueError("Agent not found")
        self.claim_agent(agent_id)
        try:
            run = self._new_run(db, agent_id, source)
            if not settings.llm_runtime_enabled:
                actions = self.game_engine.run_agent(
                    db, agent_id, source=source, run_id=run.id
                )
                status = "BLOCKED" if actions and actions[0].status == "BLOCKED" else "SUCCESS"
                credential_status = actions[0].credential_status if actions else "ACTIVE"
                self._finalize_run(
                    db,
                    run,
                    status=status,
                    credential_status=credential_status,
                    actions=actions,
                    decision_summary="确定性规则引擎已完成本轮农场巡检。",
                )
                return AgentExecutionResult(run, actions)
            return self._run_llm(db, agent_id, source, run)
        finally:
            self.release_agent(agent_id)

    def _run_llm(
        self,
        db: Session,
        agent_id: str,
        source: str,
        run: AgentRun,
    ) -> AgentExecutionResult:
        action_source = f"LLM_{source}"
        context = AgentTurnContext(
            db=db,
            engine=self.game_engine,
            agent_id=agent_id,
            run_id=run.id,
            action_source=action_source,
        )
        provider: ModelProvider | None = None

        tools = build_farm_tools(context)
        tools_by_name = {item.name: item for item in tools}

        def credential_gate(_: AgentGraphState) -> AgentGraphState:
            with self.game_engine.lock:
                _, credential_status, blocked = self.game_engine.verify_agent_access(
                    db,
                    agent_id,
                    action_source,
                    run.id,
                )
                db.commit()
            context.credential_status = credential_status
            if blocked:
                context.add_action(blocked)
            return {
                "credential_status": credential_status,
                "blocked": blocked is not None,
                "messages": [],
                "tool_rounds": 0,
                "tool_call_count": 0,
            }

        def route_after_gate(state: AgentGraphState) -> str:
            return "audit" if state.get("blocked") else "context"

        def prepare_context(state: AgentGraphState) -> AgentGraphState:
            snapshot = self.game_engine.skill_snapshot(db, agent_id)
            return {
                "messages": [
                    SystemMessage(content=SYSTEM_PROMPT),
                    HumanMessage(
                        content=(
                            "请使用允许的 Skills 完成这一轮农场管理。当前脱敏状态：\n"
                            + json.dumps(snapshot, ensure_ascii=False)
                        )
                    ),
                ],
                "tool_rounds": state.get("tool_rounds", 0),
                "tool_call_count": state.get("tool_call_count", 0),
            }

        def call_model(state: AgentGraphState) -> AgentGraphState:
            nonlocal provider
            # Never keep a SQLite transaction open while waiting for an
            # external model response.
            db.commit()
            if provider is None:
                # This node is unreachable for a credential-blocked Agent. Keeping
                # provider construction here prevents even model initialization
                # before the graph's deterministic credential gate succeeds.
                provider = build_model_provider()
            model = provider.bind_tools(tools)
            response = model.invoke(state["messages"])
            if not isinstance(response, AIMessage):
                raise ModelProviderError("Model did not return a valid assistant message")
            return {"messages": [*state["messages"], response]}

        def route_after_model(state: AgentGraphState) -> str:
            response = state["messages"][-1]
            tool_calls = getattr(response, "tool_calls", [])
            if not tool_calls:
                return "audit"
            if state.get("tool_rounds", 0) >= settings.agent_max_tool_rounds:
                return "limit"
            return "tools"

        def execute_tools(state: AgentGraphState) -> AgentGraphState:
            response = state["messages"][-1]
            tool_messages: list[ToolMessage] = []
            call_count = state.get("tool_call_count", 0)
            for tool_call in getattr(response, "tool_calls", []):
                call_count += 1
                tool_name = str(tool_call.get("name", ""))
                tool_args = tool_call.get("args", {})
                with self.game_engine.lock:
                    decision = self.game_engine.record_event(
                        db,
                        agent_id=agent_id,
                        action_type="MODEL_DECISION",
                        status="SUCCESS",
                        reason=f"DeepSeek 模型请求调用 Skill：{tool_name or 'unknown'}",
                        source=action_source,
                        credential_status=context.credential_status,
                        after={
                            "skill": tool_name,
                            "arguments": _safe_tool_arguments(tool_args),
                        },
                        run_id=run.id,
                    )
                    context.add_action(decision)
                    selected_tool = tools_by_name.get(tool_name)
                    try:
                        if selected_tool is None:
                            raise ValueError("Skill 不在允许列表中")
                        result = selected_tool.invoke(tool_args)
                    except Exception as exc:
                        message = _safe_error(exc)
                        error_action = self.game_engine.record_event(
                            db,
                            agent_id=agent_id,
                            action_type="AGENT_ERROR",
                            status="REJECTED",
                            reason=f"Skill 参数或执行被拒绝：{message}",
                            source=action_source,
                            credential_status=context.credential_status,
                            after={"skill": tool_name},
                            run_id=run.id,
                        )
                        context.add_action(error_action)
                        result = {
                            "ok": False,
                            "error": message,
                            "traceId": error_action.trace_id,
                        }
                    # Commit each Skill independently so polling clients can see
                    # progress before the complete LLM turn finishes.
                    db.commit()
                tool_messages.append(
                    ToolMessage(
                        content=json.dumps(result, ensure_ascii=False),
                        tool_call_id=str(tool_call.get("id", tool_name)),
                    )
                )
            return {
                "messages": [*state["messages"], *tool_messages],
                "tool_rounds": state.get("tool_rounds", 0) + 1,
                "tool_call_count": call_count,
            }

        def max_rounds(_: AgentGraphState) -> AgentGraphState:
            with self.game_engine.lock:
                error_action = self.game_engine.record_event(
                    db,
                    agent_id=agent_id,
                    action_type="AGENT_ERROR",
                    status="FAILED",
                    reason="模型超过本轮最大 Skill 调用轮次，已安全停止",
                    source=action_source,
                    credential_status=context.credential_status,
                    run_id=run.id,
                )
                db.commit()
            context.add_action(error_action)
            return {"error_message": error_action.reason}

        def audit(state: AgentGraphState) -> AgentGraphState:
            last_message = state.get("messages", [])[-1] if state.get("messages") else None
            return {
                "decision_summary": _summary_from_message(last_message),
                "tool_call_count": state.get("tool_call_count", 0),
            }

        workflow = StateGraph(AgentGraphState)
        workflow.add_node("credential_gate", credential_gate)
        workflow.add_node("context", prepare_context)
        workflow.add_node("model", call_model)
        workflow.add_node("tools", execute_tools)
        workflow.add_node("limit", max_rounds)
        workflow.add_node("audit", audit)
        workflow.add_edge(START, "credential_gate")
        workflow.add_conditional_edges(
            "credential_gate", route_after_gate, {"context": "context", "audit": "audit"}
        )
        workflow.add_edge("context", "model")
        workflow.add_conditional_edges(
            "model",
            route_after_model,
            {"tools": "tools", "limit": "limit", "audit": "audit"},
        )
        workflow.add_edge("tools", "model")
        workflow.add_edge("limit", "audit")
        workflow.add_edge("audit", END)

        try:
            output = workflow.compile().invoke({})
            agent = db.get(Agent, agent_id)
            if agent is not None:
                agent.last_run_at = utcnow()
            status = "BLOCKED" if output.get("blocked") else "SUCCESS"
            if output.get("error_message"):
                status = "FAILED"
            self._finalize_run(
                db,
                run,
                status=status,
                credential_status=output.get("credential_status", context.credential_status),
                actions=context.actions,
                tool_call_count=output.get("tool_call_count", 0),
                decision_summary=output.get("decision_summary", ""),
                error_message=output.get("error_message", ""),
            )
        except Exception as exc:
            db.rollback()
            message = _safe_error(exc)
            error_action = self.game_engine.record_event(
                db,
                agent_id=agent_id,
                action_type="AGENT_ERROR",
                status="FAILED",
                reason=f"模型调用已停止：{message}",
                source=action_source,
                credential_status=context.credential_status,
                run_id=run.id,
            )
            context.add_action(error_action)
            self._finalize_run(
                db,
                run,
                status="FAILED",
                credential_status=context.credential_status,
                actions=context.actions,
                decision_summary="本轮模型调用失败，未启用规则兜底。",
                error_message=message,
            )
        return AgentExecutionResult(run, context.actions)
