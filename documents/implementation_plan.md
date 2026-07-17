# Planner Agent — Implementation Plan

Status: Plan (pre-implementation)
Scope: phased, shippable build order for the architecture in
[technical_architecture.md](technical_architecture.md). Each phase leaves the app working
and is independently demoable. Mirrors reqoach's build discipline: reuse its LLM client,
streaming job core, and single-container deploy; verify every stage against real reqoach
`store/` data.

Guiding order: **prove the input plumbing and a naive decomposition end-to-end first, then
add the granularity gate (the novel part), then sequencing, then the frame/quality-signal
sophistication.** The hard, calibratable part — "small enough for Gemma" — comes only after
tasks flow through the pipeline at all.

---

## Phase 0 — Skeleton + reqoach input adapter  *(no planning yet)*
The load-bearing plumbing.
- Package `planner` under `src/`; copy reqoach's `AgentServerClient` (`llm/client.py`).
- **Input adapter**: given a reqoach `store/projects/<pid>/`, load the latest
  `scorecard.json`, `coverage.json`, `problem_statement.json`, `coverage_profile.json`.
  Validate shapes against the real files in `~/env/labs/requirements/store`.
- Normalize to the **PlanItem** stream (§3): requirements (drop `duplicate_of`) + gaps,
  each with domain + quality_signals attached.
- CLI: `python scripts/load_project.py <reqoach_pid>` → prints N requirements, N gaps.

**Done when:** the adapter reads a real reqoach project and emits a correct PlanItem list.

---

## Phase 1 — Naive decompose → flat task list  *(no gate, no graph)*
Get tasks flowing end-to-end, even if coarse.
- `planner_decompose` preset (agent_server); `register_planner_agents.py` (POST, 409→PUT).
- Stage 2 only: each PlanItem → candidate tasks (single pass, no recursion).
- Stage 5 minimal: attach a naive acceptance check per task.
- Emit a flat `plan.json` (tasks[], each `traces_to` its source). No `graph`, no `frame`.
- CLI: `python scripts/produce_plan.py <reqoach_pid> plan.json`.

**Done when:** a real project produces a flat, fully-traced task list. Spot-check that every
task traces to a requirement or gap (anti-hallucination contract).

---

## Phase 2 — Granularity gate (the linchpin, recursive)
The core bet: reduce every task to Gemma-size.
- `planner_granularity` preset scoring the 5 atomicity criteria (§5) → `atomic|split`.
- Stage 3 recursion: `split` → re-decompose, bounded depth (3); still-not-atomic at max
  depth → emit flagged + push to `unresolved[]`.
- Tasks gain `granularity{ verdict, confidence, rationale, depth }`.

**Done when:** compound requirements visibly fan out into multiple atomic tasks; oversized
tasks are flagged, not hidden. Verified on a known-compound requirement (e.g. a C5=1 item).

---

## Phase 3 — Sequencing (dependency DAG)
Turn the task list into an ordered graph.
- Structural edges from a task-kind ordering (`schema → code → test`, data-before-endpoints).
- `planner_sequence` preset for semantic edges (shared entity / producer→consumer); reuse
  reqoach's reranker-overlap machinery if it fits.
- Build `graph{ nodes, edges }`; cycle-check → cycles go to `unresolved[]`.
- Emit topological order + the DAG.

**Done when:** `plan.json` carries a valid DAG; a topological task list is renderable and
dependency-sane on a real project.

---

## Phase 4 — Frame + quality-signal-driven decomposition
Make the plan domain-aware and quality-aware (§6).
- Stage 0 Frame: resolve archetype(s)/`class` from the Coverage Profile → per-domain
  **task-kind policy** (code vs. process vs. docs). Read reqoach `catalog/` read-only.
- Feed C1–C9 signals into Stage 2: split along low-C5 seams; low C3/C4 → low-confidence +
  clarifying question; low C7 → `acceptance.kind = "review"`; `gap.severity` → `priority`.

**Done when:** a `functional` requirement yields code/test tasks while a `legal-privacy`
requirement yields process/docs tasks; poorly-scored requirements produce flagged tasks +
questions instead of confident guesses.

---

## Phase 5 — Service + streaming + minimal UI
Make it a running app, not a script.
- FastAPI + socket.io, single container (`network_mode: host`), reqoach `store/` bind-mounted
  read-only. Reuse reqoach's `jobs.py` streaming pattern: `stage`, `plan_item`, `task`,
  `sequence`, `plan`, `cancelled`, `error`; cooperative cancel.
- Endpoints: `POST /plans {reqoach_project_id}` → streamed run; `GET /plans/{id}`;
  `GET /plans` (history). Persist `store/plans/<id>/plan.json` + `meta.json` (atomic writes).
- Minimal read-only UI: pick a reqoach project → Run → tasks stream in live → dependency
  view + traceability drawer (why does this task exist?). Priority-colored (`--s1..--s5`).

**Done when:** pick a project in the browser, run, watch tasks appear as each passes the gate,
inspect the DAG and each task's trace. Verified headless (0 console errors), per reqoach norm.

---

## Later (post-MVP)
- **Executor** service: drive Gemma per task → verify against `acceptance` → loop. Closes the
  calibration loop for the granularity gate (§10.3). The Task shape already targets this.
- **Granularity calibration** from real Gemma success/failure telemetry.
- **Re-plan diff** after requirements change; **plan editing** UI (split/reorder/accept).
- **API-based input** (import from a running reqoach instead of shared `store/`).

---

## Cross-cutting
- **Verification:** validate against real reqoach `store/` data every phase; headless
  Chromium (0 console errors) once the UI exists; presets curl-checked.
- **Reuse:** reqoach `AgentServerClient`, `jobs.py` streaming, catalog, dashboard/chart/
  criticality-color + nav components, single-container deploy.
- **Non-negotiables (inherited from reqoach):** every task traces to a requirement or gap;
  every judge degrades, never raises; the plan reports confidence + flags what it could not
  reduce — it never claims "fully plannable"; local model only, no data leaves the host.
