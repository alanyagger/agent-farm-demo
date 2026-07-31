from __future__ import annotations

from typing import Literal

from mcp.server import MCPServer

from .client import FarmApiClient


mcp = MCPServer(
    "agent-farm",
    instructions=(
        "Use only these tools to operate the local credential-gated farm. "
        "Begin one run per agent, inspect state before writes, trust tool "
        "results instead of claiming success, and always finish active runs."
    ),
)
api = FarmApiClient()


@mcp.tool()
def list_farm_agents() -> dict:
    """List the three farm agents and their local Mock credential status."""
    return api.request("GET", "/api/openclaw/agents")


@mcp.tool()
def begin_farm_turn(
    instruction: str,
    agent_id: str = "agent-sprout",
) -> dict:
    """Start one audited farm turn. Defaults to Qingya when no agent is named."""
    return api.request(
        "POST",
        "/api/openclaw/runs",
        payload={"agentId": agent_id, "instruction": instruction},
    )


@mcp.tool()
def inspect_my_farm(run_id: str) -> dict:
    """Read the selected agent's farm, plots, inventory, and crop catalog."""
    return api.request("GET", f"/api/openclaw/runs/{run_id}/farm")


@mcp.tool()
def inspect_neighbors(run_id: str) -> dict:
    """Read neighbor plots that may contain mature crops available to take."""
    return api.request("GET", f"/api/openclaw/runs/{run_id}/neighbors")


@mcp.tool()
def read_recent_actions(run_id: str) -> dict:
    """Read recent audited actions for short-term farm context."""
    return api.request("GET", f"/api/openclaw/runs/{run_id}/actions")


@mcp.tool()
def harvest_crop(run_id: str, plot_id: str) -> dict:
    """Harvest one mature plot owned by the selected agent."""
    return api.request(
        "POST",
        f"/api/openclaw/runs/{run_id}/harvest",
        payload={"plotId": plot_id},
    )


@mcp.tool()
def plant_crop(
    run_id: str,
    plot_id: str,
    crop_type: Literal["CARROT", "TOMATO", "CORN"],
) -> dict:
    """Plant CARROT, TOMATO, or CORN in one empty owned plot."""
    return api.request(
        "POST",
        f"/api/openclaw/runs/{run_id}/plant",
        payload={"plotId": plot_id, "cropType": crop_type},
    )


@mcp.tool()
def steal_crop(run_id: str, plot_id: str) -> dict:
    """Take one allowed unit from a mature neighbor plot."""
    return api.request(
        "POST",
        f"/api/openclaw/runs/{run_id}/steal",
        payload={"plotId": plot_id},
    )


@mcp.tool()
def finish_farm_turn(run_id: str, summary: str) -> dict:
    """Finish the run with a concise summary based only on tool results."""
    return api.request(
        "POST",
        f"/api/openclaw/runs/{run_id}/finish",
        payload={"summary": summary},
    )


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
