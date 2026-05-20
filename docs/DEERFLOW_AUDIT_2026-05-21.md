# DeerFlow Audit - 2026-05-21

Repository reviewed: <https://github.com/bytedance/deer-flow>

## Executive Summary

DeerFlow is useful to us as an architecture reference, not as a dependency to
embed inside the trading system.  It is a broad super-agent harness with
sub-agents, skills, sandboxed execution, memory, message channels, tracing, and
Gateway APIs.  That is powerful, but too large and too privileged to run inside
the live trading stack.

The right move is to copy the discipline:

- progressive skills,
- explicit run modes,
- deterministic guardrails before tool/action execution,
- per-task state and structured outputs,
- context management through summaries/files,
- strict separation between research and live execution.

## What Was Relevant

### 1. Progressive Skills

DeerFlow keeps skills as structured workflow files and loads only what the task
needs.  For poly1 this maps cleanly to strategy research:

- backtest skill,
- market microstructure skill,
- cross-venue arbitrage skill,
- paper/repo review skill,
- research-to-shadow-spec skill.

### 2. Sub-Agent Discipline

DeerFlow explicitly caps parallel subagents and batches work.  The useful rule
for us is not "spawn many agents"; it is "decompose only when there are truly
independent subtasks, cap concurrency, and synthesize structured outputs."

### 3. Guardrails

DeerFlow's guardrail design evaluates tool/action calls before execution.  For
poly1 the equivalent is:

- no live trading from research tasks,
- shadow-only until backtest/markout evidence exists,
- no secret export,
- rule mapping required for arbitrage,
- human approval before promotion.

### 4. Context Management

DeerFlow summarizes long work and preserves important skill context.  For poly1
we should keep long research output in files and feed MetaBrain only concise,
structured evidence: sample size, winrate, markout, PnL proxy, blockers.

### 5. Security Warning

DeerFlow explicitly warns that public deployment with command execution can be
risky.  This matters for us because poly1 touches money.  We should not expose
a DeerFlow-like agent runtime on the trading server unless it is isolated,
authenticated, network-restricted, and incapable of placing trades.

## What We Added To poly1

### Research Harness

Files:

- `agents/application/research_harness.py`
- `config/research_harness.json`
- `scripts/research_harness.py`
- `tests/test_research_harness.py`

The harness defines a deterministic, local version of DeerFlow's useful pieces:

- run modes: `flash`, `standard`, `pro`, `ultra`,
- max parallel task limit: `3`,
- named research skills,
- allowed guardrails,
- run plans generated from `config/research_queue.json`.

It does not call LLMs, does not open live trading, and does not execute shell
actions.  It is a planning/validation layer that makes tomorrow's 30-day
backtest and strategy QA repeatable.

### Registered Agent

`research_harness` was added to `config/agent_registry.json` as a shadow-only
`research_orchestrator`.

## How To Use

```bash
python scripts/research_harness.py --json --plans
python scripts/research_queue.py --json
python scripts/validate_agent_registry.py --json
```

Tomorrow morning, the 30-day QA should use the harness plan as the checklist:

1. Run `shadow_markout_backtest`.
2. Review `market_microstructure_review`.
3. Review `cross_venue_arb_review`.
4. Convert any promising paper/repo into a `research_to_shadow_spec`.
5. Block anything deferred or lacking evidence.

## Decision

Do not install DeerFlow into the live trading server now.

Do keep borrowing its architecture patterns in small, testable slices.
