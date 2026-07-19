# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Planner Agent is the **plan-only** stage of the SDLC chain **analyst_agent → architect_agent →
planner_agent → builder_agent**. It consumes validated requirements (+ optional architecture) and
emits a `plan.json` task graph for builder_agent. It never executes anything.

Authoritative design: `documents/technical_architecture.md` (as-built) and
`documents/implementation_plan.md` (done/next roadmap). Both are kept accurate — read them first.

## The load-bearing constraint

**Operational runtime = local Gemma presets + deterministic Python only. No Claude at runtime;
offline-capable.** Claude (you) is a *build-time* tool for authoring/tuning prompts — never in the
plan-production path. This is why every judge is a local-model preset and all routing/dedup/DAG/
verification is plain code. Do not introduce a runtime dependency on Claude or any cloud API.

## Commands

No build system, no test framework, no packaging (stdlib only). "Tests" are dev harnesses (below).

- **Register the Gemma presets** (one-time, idempotent; needed before planning):
  `python scripts/register_planner_agents.py`  — prompts live in `prompts/*.txt`; to change a
  judge's behavior, edit the prompt file and re-register.
- **Produce a plan** (the product entry point):
  `python scripts/produce_plan.py <PROJECT_ID> [--limit N] [--workers N] [--checkpoint PATH] [--modelled-only]`
  → `data/plans/<PROJECT_ID>.plan.json`. `<PROJECT_ID>` is an Analyst project id (`curl :7803/projects`).
- **Verify a change** (there is no unit-test runner): `python3 -m py_compile src/planner/*.py scripts/*.py`,
  then run `produce_plan.py --limit 4` and read the output. Prefer verifying against live services.
- **Dev-only harnesses** (`tune_*.py`, `measure_*.py`, `validate_*.py`, `run_opencode_outcomes.py`):
  they drive Gemma and/or opencode to tune prompts and measure plan quality. They are **not** part of
  the operational path — `run_opencode_outcomes.py` / `measure_plan_quality.py` in particular are
  builder-territory probes used here only for validation.

## Required live services

- **Analyst** `:7803` — `GET /projects/{pid}/package` is the requirements input (`ANALYST_URL`).
- **agent_server** `:7701` — hosts/routes the Gemma presets (`AGENT_SERVER_URL`); admin API at
  `/admin/api/agents/{name}` for preset config.
- **llama.cpp** `:8500` — the actual Gemma model server (currently `--parallel 2` = 2 slots; verify
  before assuming — it has changed). Shared across all agents; raising slots is a platform decision.
- **Architect handover** (optional) — `{ARCHITECT_ARCH_DIR}/<project_id>/planner_handover.json`;
  absent → planner runs with planner-chosen names (loaded defensively).

## Architecture (the cross-file picture)

`produce_plan.py` → `analyst.get_package` → `pipeline.plan_project` → `pipeline.assemble_plan` → JSON.

`pipeline.plan_project` processes **one requirement at a time** (this is what makes it concurrent and
resumable), each independently: `stages.decompose` (against Architect context if present) →
`loop.plan_tasks`. Then it aggregates all requirements' results and applies two **global** steps in
this order: **architect naming, then dedup** (order matters — canonical names must be what dedup
merges on).

`loop.plan_tasks` is the core: a work queue where each task hits the **feasibility gate**
(`planner_feasibility_reason` preset) → if not feasible, **refine** (`planner_refine`: one of
`split | prerequisite | resolve | question`), re-queued with `refines+1`; bounded by `refine_budget`
(a per-lineage reprocess counter — the single termination guard). Only `feasible` tasks reach the
plan; genuine unknowns become `questions` (never fabricated), unconvergeable tasks become `flagged`.

`client.py` has two call paths: `preset_json(agent, user)` — the "A1" convention where the preset on
agent_server supplies the system prompt + sampling (gate/refine/decompose use this); and inline
`complete_json/complete_text` (system prompt in code, legacy validation stages). The client is
thread-safe (locked counters) — that's why `--workers` works.

## Invariants that will bite if violated

- **`req_id` is THE trace key** across the whole Analyst→…→Builder chain — use verbatim, never re-key.
  Note it is one namespace mixing author ids (`NFR-03`) and generated ids (`REQ-0005`).
- **Every task `traces_to` a `req_id`** (anti-hallucination) — nothing invented from nowhere. Dedup
  unions the traces of merged tasks.
- **Dedup is union-find on normalized title OR deliverable-basename**; the survivor unions
  `traces_to`, keeps fuller instructions, and dependents are rewired onto it. Runs *after* naming.
- **On resume/cache, call `loop.advance_ids(max_existing)`** before generating new tasks, or task ids
  collide with committed ones.
- **The gate deliberately treats a conventional tech default** (which framework/DB/library — the
  planner's call) as *feasible*, but **unspecified product content** (a formula, business rule, data
  source, auth mechanism) as *not feasible* → a question. Preserve this distinction when tuning.
- **Coverage gaps are pass-through escalations**, not tasks. **`depends_on` from the Architect is
  interface direction, not build order** — sequencing stays the planner's.

## Project-specific lessons (measured, non-obvious)

- **Heuristic/structural granularity proxies do NOT predict feasibility** (measured) — do not add a
  regex/word-count pre-gate that skips the LLM judge; it risks false-feasible verdicts the gate is
  tuned to avoid (kept at ~0).
- **For similarity/duplicate judgments use the reranker** (`:8601` `/v1/rerank`, sigmoid-scaled), not
  raw cosine — house rule; relevant to the future semantic-sequencing work.
- **A local LLM judging its own output saturates** (~92% rubber-stamp) — plan quality is validated by
  *deterministic* checks (compile/parse + stub-detector), never LLM self-report.
