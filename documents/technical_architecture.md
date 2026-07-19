# Planner Agent — Technical Architecture (as-built)

Status: **AS-BUILT**, 2026-07-19. This document describes the system that exists in the code,
not an intended design. Where a capability is designed-but-not-built it says so explicitly.
It supersedes the earlier aspirational version (the 6-stage pipeline described previously was
never implemented as such — see §10).

Scope: a **plan-only** service. It consumes validated requirements + an optional architecture
model and emits a `plan.json` task graph for `builder_agent`. It does not execute anything.

Runtime constraint (honored everywhere): **local models + deterministic code only. No Claude
at runtime; works offline.** Claude is used at build-time to author/tune prompts, never in the
operational path.

---

## 1. Position in the chain

```
analyst_agent (:7803)  ──►  architect_agent (files)  ──►  Planner Agent  ──►  builder_agent
validated requirements     SysML model + handover        this repo:          builds each
+ INCOSE scores + gaps     (component names, IFCs,        decompose→gate→     feasible task
                            constraints, open issues)      refine→plan.json    via opencode
```

`req_id` is the single trace key across the whole chain (e.g. `NFR-03`, `REQ-0005` — one
namespace: author-assigned ids from the source document plus generated ids for unlabelled
requirements). Every emitted task carries `traces_to: [req_id, …]`.

---

## 2. Inputs

### 2.1 Analyst package (required) — `src/planner/analyst.py`
One HTTP call returns everything:
```
GET {ANALYST_URL:-http://localhost:7803}/projects/{pid}/package
```
`requirements_from_package()` maps each record to a `Requirement` keyed on **`req_id` verbatim**,
carrying: `text`, per-characteristic INCOSE scores (`C1..C9`), `avg_score`, and the routing
`classes[]`/`constraints[]` (empty until the Analyst's `classify:run` has run — currently empty).
`coverage_gaps_from_package()` and `problem_statement_version()` ride in the same payload.
`readiness()` extracts the manifest: **branch on `architect_ready`, not on data being present**
(every package is `draft`/`architect_ready:false` today — the Analyst release gate isn't built).

> Historical note: the planner previously read a reqoach filesystem `store/`. That store was
> deleted when reqoach was refactored; the Analyst service replaced it. No file path remains.

### 2.2 Architect handover (optional) — `src/planner/architecture.py`
```
{ARCHITECT_ARCH_DIR}/{project_id}/planner_handover.json
```
Loaded defensively: **absent → `None` → the planner runs exactly as before** (no architecture
context, planner-chosen names). When present it provides, keyed by `req_id`:
- `by_requirement[req_id]`: components, functions, interfaces, constraints, state_machines, `classes`.
- `components[]`: every component once, with a `suggested_module` naming hint.
- `open_issues[]`: things the Architect could not settle (kinds observed: `unquantified_constraint`,
  `semantic_defect`, `unresolved_interface`; a `detail` may be a string OR an object).
- `depends_on[]`: **interface direction, not build order** — used as a hint only.

---

## 3. The pipeline (what actually runs)

Entry: `pipeline.plan_project(client, requirements, refine_budget=3, refine_k=1, handover=None)`
→ `pipeline.assemble_plan(...)`. Driven by `scripts/produce_plan.py <PROJECT_ID>`.

```
requirements ─► DECOMPOSE (per requirement, against architecture if present)
                    │  seed tasks, each traces_to=[req_id]
                    ▼
             ┌─ BOUNDED GATE→REFINE LOOP (loop.plan_tasks) ────────────────────────┐
             │  pop task → FEASIBILITY GATE                                          │
             │     feasible ───────────────────────────────► collect (feasible)     │
             │     else, if refines < refine_budget → REFINE:                        │
             │        split | prerequisite | resolve | question                      │
             │        re-queue children/self (refines+1)                             │
             │     else → FLAGGED (did not converge)                                 │
             └──────────────────────────────────────────────────────────────────────┘
                    │  {feasible[], questions[], flagged[]}
                    ▼
             ARCHITECT NAMING (precedence)  →  DEDUP (union-find)  →  ASSEMBLE plan.json
```

### 3.1 Decompose — `stages.decompose`
One LLM call per requirement proposes candidate tasks `{title, kind, deliverable, instructions}`
(`kind ∈ code|test|schema|config|docs`). If a handover exists, the requirement's
`architecture_context` (component/function/interface names + constraints) is injected so tasks
adopt the Architect's structure instead of inventing one.

All three LLM stages now use registered presets (temp 0). `stages.decompose` calls the
`planner_decompose` preset (2048 / temp 0); its prompt is identical to the former inline one, so
this reconciled config without changing behavior.

### 3.2 Feasibility gate — `loop._gate` → preset `planner_feasibility_reason`
A reasoning-scripted judge (temp 0, max_tokens 1024) returns
`{verdict: feasible|borderline|infeasible, reasoning, missing, blocking_criterion}`. It scores
five atomicity criteria (single deliverable; no open design decision; self-contained; concrete
done-condition; no hidden fan-out) and, crucially, distinguishes a **conventional tech default**
(which framework/DB — feasible; the Planner's call, not a gap) from **unspecified product content**
(a formula, business rule, data source — not feasible). **Prerequisite-aware:** the gate is told
which prerequisite deliverables to assume exist, so a dependent task isn't judged infeasible
forever. Only `feasible` is terminal-good; `borderline` and `infeasible` both route to refine.

### 3.3 Refine — `loop._refine` → preset `planner_refine`
Given the task + the gate's stated gap, chooses one action (temp 0, max_tokens 2048):
- **split** — too large/fan-out → smaller single-deliverable children.
- **prerequisite** — needs a missing artifact → emit it as a task + a dependency edge. **Reused,
  not duplicated:** if an equivalent prerequisite already exists, the edge is wired to it.
- **resolve** — an open decision with a truly conventional default → restate it, proceed.
- **question** — a genuine unknown (formula, vendor, business/legal rule) → **escalate, never
  fabricate** (the honesty principle; tuned so tech-stack defaults resolve while product
  decisions ask).

`refine_k` (default 1) enables self-consistency: sample K times, majority-vote the *action*, pick
a representative. Built and unit-tested; not on by default.

### 3.4 Termination — `refine_budget` (default 3)
A single guard. `refines` counts how many times a task **lineage** has been reprocessed by *any*
mechanism (split, prerequisite re-judge, resolve). At the budget it is flagged, never re-queued.
Finite reprocesses × finite branching ⇒ the loop always halts. (This is a *reprocess* budget, not
a nesting depth — a resolve that doesn't nest still counts.)

### 3.5 Architect naming precedence — `pipeline._apply_architect_naming`
Runs **before** dedup (so canonical names are what get merged on). If a feasible task maps to
exactly one Architect component for its requirement, the deliverable is renamed to that
component's module (`MatchingService` → `matching_service.py`); the planner's original name is
kept in `planner_proposed_deliverable`, `named_by="architect"`. A requirement with several
components only renames when the task clearly matches one — otherwise it keeps the planner's more
specific name (prevents distinct tasks collapsing onto one filename).

### 3.6 Dedup — `loop.dedup_plan`
The same work produced from different requirements/branches is merged so the builder doesn't build
(or overwrite) one artifact twice. **Union-find** merges tasks that share **either** signal — the
same normalized title **or** the same deliverable basename — so chains collapse. The survivor
**unions `traces_to`** (still serves every requirement), keeps the fuller instructions, and every
dependency pointing at a merged-away task is **rewired onto the survivor** (no dangling/self edges).
Repeated questions/flags collapse too.

### 3.7 Assemble — `pipeline.assemble_plan`
Builds the `plan.json` contract (§4): feasible tasks with acceptance/dependencies/traceability,
questions, flagged, coverage gaps, architecture provenance + open issues, and the DAG.

---

## 4. Output — `plan.json` (the builder handoff contract)

```jsonc
{
  "contract_version": "1.0",
  "source": {                    // Analyst provenance — branch on architect_ready, don't assume
    "producer": "analyst-agent", "project_id", "project_name", "run_id",
    "release_status", "architect_ready", "threshold", "blockers",
    "problem_statement_version", "requirements_considered", "trace_key": "req_id"
  },
  "summary": { "feasible", "questions", "flagged", "coverage_gaps" },

  "tasks": [ {                   // FEASIBLE only — every one passed the gate
    "task_id", "title", "kind", "deliverable",
    "deliverable_named_by": "planner|architect", "planner_proposed_deliverable",
    "instructions",
    "acceptance": { "kind", "check", "source?" },   // §5
    "depends_on": ["task_id", …],
    "traces_to": ["req_id", …],                     // never empty
    "origin": "decomposed|split|prerequisite|resolved",
    "feasibility": { "verdict", "reasoning" }
  } ],

  "questions":   [ { "task_title", "question", "gap", "traces_to" } ],   // → requirements author
  "flagged":     [ { "task_title", "reason", "traces_to" } ],            // did not converge
  "coverage_gaps": [ … ],        // Analyst coverage gaps, PASS-THROUGH escalations (not tasks)
  "architecture_open_issues": [ … ],  // Architect open issues touching planned reqs — do not build silently
  "architecture": { "architect_ready", "release_status", "components_named", "note" } | null,
  "graph": { "nodes": ["task_id"], "edges": [["a","b"]] }   // b is a prerequisite of a
}
```

---

## 5. Acceptance criteria

Deterministic, kind-based defaults (`pipeline._acceptance`), designed for builder_agent's
verifier: `test → run and pass`; `schema/config → parses, no placeholders`; `docs → human review`;
otherwise `build+verify → compiles/parses, no stub fingerprints`. **When the Architect supplies a
validated constraint expression** for the requirement (e.g. `latencyMs <= 200`), acceptance becomes
that constraint (`kind:"constraint", source:"architect"`). Real per-task tests/assertions are
**not** generated. (Upstream currently emits zero constraints, so that path is unexercised.)

---

## 6. LLM presets & config

| stage | preset / path | max_tokens | temp | notes |
|---|---|---|---|---|
| decompose | `planner_decompose` preset | 2048 | 0.0 | prompt identical to former inline path |
| feasibility gate | `planner_feasibility_reason` | 1024 | 0.0 | reasoning-scripted, prereq-aware |
| refine | `planner_refine` | 2048 | 0.0 | actions: split/prereq/resolve/question |

`src/planner/client.py` is a thin stdlib client over agent_server (`:7701`). `preset_json(agent,
user)` sends only user content (preset supplies system prompt + params — the "A1" convention);
the inline `complete_json/complete_text` path carries an in-code system prompt + the client default
(8192 / temp per call). Registered idempotently by `scripts/register_planner_agents.py`.

---

## 7. Concurrency & performance (the real numbers)

- **Per-requirement, optionally concurrent.** `plan_project` processes each requirement
  independently (decompose → gate/refine loop), then aggregates → names → dedups globally.
  `--workers N` fans requirements over a thread pool (the client is thread-safe); within a
  requirement the gate/refine loop is still serial (`while work:`).
- **Backend: 2 llama.cpp slots** (verified: the model server runs `--parallel 2`, and 2 concurrent
  requests complete in ~1 request's time). So `--workers 2` gives a real ~2x speedup now; going beyond
  2x is what would need `--parallel` raised (a shared-VRAM tradeoff).
- **Resumable.** `--checkpoint PATH` writes a per-requirement JSON-lines WAL; a restart skips
  done `req_id`s and plans only the delta (`advance_ids` avoids task-id collisions). So an
  interruption costs minutes, not the whole run.
- **Volume:** ~20 LLM calls per requirement (measured) → a full 386-run is ~7–8k calls; with
  `--workers 2` on 2 slots that is roughly halved. Largest run to date: **~5 requirements** — the
  full 386 run has not yet been done (see §9).

---

## 8. Design principles realized (verified)

- **Honesty over completeness** — genuine unknowns become questions; coverage gaps go to the
  author, not fabricated tasks. Refiner tuned so it asks about product decisions and resolves
  conventional tech defaults.
- **Traceability / anti-hallucination** — every task `traces_to` a `req_id`; nothing invented from
  nowhere. Merged tasks union their traces.
- **Deterministic where it counts** — routing, dedup (union-find), the DAG, naming precedence, and
  acceptance are code. Judges are local Gemma presets. Nothing calls Claude at runtime.
- **Quality validated by execution, not self-report** — plan quality was checked by actually
  building feasible tasks with opencode and verifying deterministically (compile/parse + a HARD/SOFT
  stub-detector). A local LLM judging its own output was measured to saturate (~92% rubber-stamp)
  and is not trusted. NOTE: that build-probe is `builder_agent` territory; in this repo it lives in
  `scripts/` as a dev-time validation harness only.

---

## 9. Not built / known limitations (honest list)

- **No frame/archetype stage** (Stage 0 of the old design) — task kinds come from decompose, not an
  archetype policy.
- **Coverage gaps are pass-through**, not normalized into the plannable stream — they are surfaced
  as escalations, never turned into tasks.
- **No semantic sequencing** — the DAG carries only prerequisite edges the refine loop creates.
  There is no LLM- or reranker-discovered cross-task dependency detection.
- **No confidence score** is computed.
- **Acceptance is shallow** — kind-based defaults + Architect constraints when present; no generated
  tests/assertions; `kind:test` acceptance is not actually executed by the planner.
- **Cross-requirement dedup is unexercised on real data** — implemented + unit-tested, but the small
  samples only triggered intra-requirement merges.
- **No service / streaming / UI and no packaging** — CLI + JSON files only; no Dockerfile,
  docker-compose, or requirements.txt (unlike analyst_agent/architect_agent). The loop exposes a
  `log=` seam ready for streaming, but nothing consumes it.
- **Not run at scale** — largest run ~5 requirements; the 386-run is now feasible (resumable + 2× parallel) but not yet done.

---

## 10. Divergence from the original design doc

The prior document described a 6-stage pipeline (Frame → Normalize → Decompose → Granularity Gate →
Sequence → Acceptance/Emit) and structural proxies for granularity. Reality:
- The recursive "granularity gate" exists as the **gate→refine loop bounded by `refine_budget`**.
- **Frame, gap-normalization, and semantic Sequence are not built.** Emit computes no confidence.
- Structural/heuristic granularity proxies were **measured this project and did not predict
  feasibility** — the gate is an LLM judgment against explicit criteria, not heuristics. (This is
  why a heuristic pre-gate is *not* used: it would risk the false-feasible verdicts the gate is
  tuned to avoid.)

---

## 11. Repository layout

```
src/planner/
  client.py       agent_server client (preset "A1" call + inline path; stdlib only)
  analyst.py      Analyst package client — requirements/gaps/readiness, keyed on req_id  (§2.1)
  architecture.py Architect handover reader — context, naming, constraints, open issues  (§2.2)
  loader.py       Requirement dataclass + quality-bucket sampling
  stages.py       decompose (planner_decompose preset) + legacy validation-era stages
  loop.py         plan_tasks (gate→refine loop, refine_budget) + dedup_plan (union-find)
                  + advance_ids (resume-safe task ids)                                    (§3)
  checkpoint.py   per-requirement JSON-lines WAL (append/resume) — makes long runs resumable
  pipeline.py     plan_project (per-req, --workers threads, --checkpoint) + assemble_plan  (§3,§4,§7)
prompts/          planner_feasibility_reason (gate) · planner_refine · planner_decompose · …
scripts/          register_planner_agents · produce_plan (entry point)
                  + dev-time validation harnesses (tune_*, measure_*, run_opencode_outcomes)
documents/        technical_architecture.md (this) · implementation_plan.md
```
