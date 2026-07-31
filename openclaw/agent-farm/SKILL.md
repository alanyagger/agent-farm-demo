---
name: agent-farm
description: Use the local credential-gated farm MCP tools to inspect, plant, harvest, and take mature crops for Qingya, Tianxiaonuo, or Heguang.
---

# Agent Farm

Use the `agent-farm` MCP tools whenever the user asks to inspect or operate the
local farm game. Do not use Shell, curl, filesystem access, or direct HTTP for
farm actions.

## Agent Mapping

- 青芽: `agent-sprout`
- 田小诺: `agent-nova`
- 禾光: `agent-orbit`
- If the user does not name an agent, use 青芽.
- If the user names multiple agents, complete a separate turn for each agent in
  the order they were named.

## Required Workflow

1. Call `list_farm_agents` when identity or credential availability is unclear.
2. Call `begin_farm_turn` once for the selected agent and preserve its `runId`.
3. If the returned `ok` or `allowed` value is false, stop that agent's turn and
   explain the credential or busy-state message exactly.
4. Call `inspect_my_farm` before any write.
5. Call `inspect_neighbors` only when the request includes neighbor activity,
   stealing, social harvesting, or a general "play one round" instruction.
6. Call only the write tools needed to satisfy the user's request.
7. Call `finish_farm_turn` for every active run, using a short factual summary
   based only on successful tool results.

## Game Policy

- A general "玩一轮" request means: harvest one mature owned plot, plant up to
  two empty plots, then take from at most one eligible mature neighbor plot.
- One turn allows at most one successful harvest, two successful plants, and
  one successful steal. Do not retry after the service reports a quota limit.
- Use only plot IDs returned by inspection tools.
- Allowed crop values are `CARROT`, `TOMATO`, and `CORN`.
- Never claim an action succeeded unless its tool result says `ok: true`.
- A rejected plot or cooldown is a normal business result. Choose another valid
  plot only when the user's request and the remaining quota still allow it.
- A blocked credential ends the turn immediately. Do not switch to another
  agent unless the user explicitly asked for multiple agents.
