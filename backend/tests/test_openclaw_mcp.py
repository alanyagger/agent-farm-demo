from __future__ import annotations

import pytest
from mcp import Client

from backend.openclaw_mcp.server import mcp


@pytest.mark.asyncio
async def test_openclaw_mcp_exposes_only_farm_tools() -> None:
    async with Client(mcp) as client:
        result = await client.list_tools()

    tools = {tool.name: tool for tool in result.tools}
    assert set(tools) == {
        "list_farm_agents",
        "begin_farm_turn",
        "inspect_my_farm",
        "inspect_neighbors",
        "read_recent_actions",
        "harvest_crop",
        "plant_crop",
        "steal_crop",
        "finish_farm_turn",
    }
    assert tools["begin_farm_turn"].input_schema["properties"]["agent_id"][
        "default"
    ] == "agent-sprout"
    crop_schema = tools["plant_crop"].input_schema["properties"]["crop_type"]
    assert crop_schema["enum"] == ["CARROT", "TOMATO", "CORN"]
