# Planner Agent — Implementation Roadmap

Status: **living roadmap**, 2026-07-19. Reflects what is built vs what remains, consistent with
the as-built [technical_architecture.md](technical_architecture.md). (The earlier version of this
file described a 6-stage design that was never implemented — superseded.)

Non-negotiables (hold across all work): every task `traces_to` a `req_id`; judges degrade, never
raise; genuine unknowns become questions (never fabricated tasks); **local models + deterministic
code only, no Claude at runtime, offline-capable**.

---

## Part A — Built (done)

- **A1 · Analyst input** — `analyst.py`: `GET :7803/projects/{pid}/package` → requirements keyed on
  `req_id` verbatim, + coverage gaps, problem statement, readiness manifest. Replaced the deleted
  reqoach `store/`. *Verified live.*
- **A2 · Decompose** — `stages.decompose` → `planner_decompose` preset (temp 0). Decomposes a
  requirement into candidate tasks; injects Architect context when a handover exists.
- **A3 · Bounded gate→refine loop** — `loop.plan_tasks`: feasibility gate
  (`planner_feasibility_reason`, tuned, execution-validated) → refine
  (`planner_refine`, honesty-tuned: split/prerequisite/resolve/question) → `refine_budget`
  termination. Prerequisite reuse + prerequisite-aware re-judge. *Gate + refiner tuned this project.*
- **A4 · Architect handover integration** — `architecture.py`: decompose-against-architecture,
  component-name precedence (collision-safe), constraint-based acceptance, open-issue surfacing.
  Defensive (missing file → runs as before). *Verified against a real 386-req handover.*
- **A5 · Dedup** — `loop.dedup_plan`: union-find on title|deliverable, rewire deps, union traces.
  Runs after naming. *Verified: zero dup deliverables, DAG intact.*
- **A6 · plan.json emission** — `pipeline.assemble_plan`: feasible tasks (contract + acceptance +
  deps + traces + feasibility), questions, flagged, coverage_gaps, architecture provenance +
  open issues, prerequisite DAG. `scripts/produce_plan.py <PROJECT_ID>`.
- **A7 · Scale plumbing** — per-requirement restructure → `--workers N` (thread pool; backend has
  **2 llama.cpp slots**, so ~2× real) + `--checkpoint PATH` (JSON-lines WAL, resume by `req_id`
  delta; `advance_ids` avoids id collisions). *Verified E2E: serial+ckpt then resume+threaded.*
- **A8 · Downstream** — `builder_agent` MVP exists (separate repo): consumes `plan.json`, builds via
  opencode, verifies deterministically.

---

## Part B — Next (prioritised)

### B1 · Full-scale run (386 requirements) — *highest value, low effort now*
The only real test that cross-requirement dedup and the DAG hold on genuine overlap. Feasible now
(resumable + ~2×). Run, inspect the plan, fix whatever the scale surfaces.
**Done when:** a 386-req plan is produced and its DAG/dedup/questions are sane.

### B2 · Packaging + service + minimal UI
No Dockerfile/compose/requirements today (unlike Analyst/Architect); CLI + JSON only. Add packaging;
wrap `produce_plan` in a small service that streams the loop's existing `log=` events (socket.io or
SSE); a read-only page: tasks appearing as they pass the gate, the DAG, a traceability drawer.
**Done when:** pick a project in a browser, run, watch it stream, inspect the plan + traces.

### B3 · Semantic sequencing (enrich the DAG)
Today the DAG has only prerequisite edges. Add cross-task dependency detection via the **reranker**
(`:8601` `/v1/rerank`, sigmoid-scaled — *not* raw cosine, per house rule): candidate pairs by
shared entity / producer→consumer, LLM-confirm above threshold. Cycle-check.
**Done when:** the DAG carries real cross-task edges beyond prerequisites, cycle-free.

### B4 · Memoization / per-project cache — **DONE + verified (2026-07-19)**
Per-requirement loop results are memoized by input-hash in a per-project file, so a re-plan after a
small change reprocesses only the changed requirements. As built:
- **`src/planner/cache.py` · `Cache`** — a per-project JSON-lines store (`data/cache/<project_id>.jsonl`).
  Key = `sha1(CACHE_VERSION + req.text + arch_context)`. `cache_version()` folds in the *contents* of
  the decompose/gate/refine prompt files (`prompts/*.txt`) → retuning any of those prompts changes the
  version and every entry it could have shaped stops matching (auto-invalidation). Reuses checkpoint's
  `_res_to_dict`/`_res_from_dict`; append-only, last-write-wins, thread-safe (lock).
- **Wired into `pipeline.plan_project` / `_plan_one_requirement`**: compute `ctx` once, cache-`get`
  on (text, ctx) → hit short-circuits the LLM work; miss plans then `put`s. Naming + dedup + assemble
  ALWAYS re-run fresh over the aggregated (cached + new) set (cross-requirement dedup stays correct).
- **Id-collision handling**: `plan_project` calls `advance_ids(max(checkpoint_max, cache_max))` before
  planning, so reused entries keep their (lower) ids and new tasks get ids strictly above them. Because
  every run advances past the current max before generating, cache ids stay globally distinct/monotonic.
- **CLI**: `--cache` (bare → `data/cache/<project_id>.jsonl`, or `--cache PATH`).
- **Caveat (holds):** helps *re-runs* only; a cold first run computes everything once (does nothing for
  the initial 386 cold run — B1 is still separate).
- **Verified**: offline mechanics test (roundtrip, key sensitivity, version invalidation, id-monotonicity)
  + live two-pass run (`--limit 4 --workers 2`): run 1 = 4 miss / ~50 s, run 2 = 4 hit / **0.11 s**,
  plan.json **byte-identical**, cache file did not grow on the hit run.

### B5 · Coverage-gap planning
Today gaps are pass-through escalations. Optionally turn a gap into a plannable item (a task to add
the missing requirement) vs. keep escalating genuine unknowns.
**Done when:** coverage gaps yield tasks or questions, not just pass-through.

### B6 · Acceptance depth
Use Architect constraints when emitted (upstream emits none yet); make `kind:test` acceptance
actually executable (a runnable check), beyond the kind-based default.

### B7 · Builder feedback loop (cross-agent calibration)
Feed builder's real `built`/`failed` outcomes back to tune the gate — a feasible task that
repeatedly fails to build is a gate false-positive. Designed, unwired.

### B8 · Optional / lower priority
Confidence score on the plan; a frame/archetype stage for task-kind policy; use of the Analyst
`classes[]` routing (also empty upstream until `classify:run`).

---

## Cross-cutting
- **Verification:** every change checked against the live Analyst (`:7803`) + a real Architect
  handover; deterministic pieces unit-tested; the loop's outputs checked for dup deliverables /
  dangling edges.
- **Reuse:** the existing checkpoint serialization and `advance_ids` for B4; the `log=` seam for B2;
  the reranker service for B3.
- **Infra note:** raising throughput past ~2× needs the model server's `--parallel` raised — shared
  VRAM across all agents, a platform decision, not the planner's to flip.
