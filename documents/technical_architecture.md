# Planner Agent — Technical Architecture

Status: Design (pre-implementation, living doc)
Scope: a **plan-only** service that consumes **reqoach** output — the validated
requirements set (`scorecard.json`) *and* the coverage verdict (`coverage.json`),
grounded by the project's Problem Statement — and emits a **granular, dependency-aware
task graph** (`plan.json`). Every task is decomposed to a size a **local model
(Gemma 4 E4B)** can implement in one shot, with an explicit acceptance check. Planner
does **not** execute tasks; it produces an execution-ready plan and stops.

Upstream companion (the analyzer that produces our input):
`~/env/labs/requirements` — "reqoach". Its projects-mode design lives at
`~/env/labs/requirements/specs/projects_mode/technical_architecture.md`.

---

## 1. Why this exists

reqoach answers two questions about a requirements set: **"is each requirement
well-written?"** (INCOSE quality, C1–C9) and **"what's missing?"** (coverage — gaps and
questions with confidence). Neither question moves work forward. The natural next step is
**"what do we actually do about these requirements, in pieces small enough to be built?"**

Planner Agent turns a *judged* requirements set into an **ordered task graph**. The design
bet — the reason it can lean on a small local model — is granularity:

> **A local model like Gemma 4 E4B cannot reliably implement a requirement, but it can
> reliably implement a task that has one deliverable, one artifact, no unresolved design
> decisions, and a checkable done-condition.** Planning is therefore the act of reducing
> requirements to that size.

Two consequences drive the whole design, mirroring reqoach's own honesty principles:

1. **Granularity is judged, not measured.** "Small enough for Gemma" is not a line count or
   a token budget — it is a per-task LLM verdict against explicit atomicity criteria (§5),
   applied recursively until every leaf passes. This is the analog of reqoach's rule that
   *the LLM is the sole requirement identifier — no regex.*
2. **Every task must trace to a requirement or a gap.** A task with no upstream source is a
   hallucinated task. Traceability is the anti-hallucination gate — the analog of reqoach's
   segmentation traceability check (a requirement that can't be traced to its source block
   is dropped).

Non-negotiable, inherited from reqoach: the plan carries **confidence, and it flags what it
could not reduce.** It does not claim "fully plannable." A requirement too ambiguous to
decompose safely yields a *clarifying question*, not a confident guess (§6).

---

## 2. What Planner consumes (reqoach output — real shapes)

Input is a **reqoach project**: one Problem Statement, one (latest) Quality run, one
(latest) Coverage run. Planner reads them either from reqoach's `store/projects/<pid>/…`
on disk, or over its API. The concrete shapes (verified against live store data):

### `scorecard.json` — the stated requirements (the primary atoms)
```
requirements[]:                         # ~hundreds per project
  req_id            "REQ-0001"
  text              the requirement prose
  provenance        { source_document_id, source_document, section_path, page,
                      bbox, char_span }        # full traceability back to the doc
  lineage           { origin, was_compound, derived_from, duplicate_of }
  characteristics   { C1..C9: { score 1..5, rules_triggered[], evidence, justification } }
set_level           # C10..C15 across the whole set (consistency, completeness-as-a-set…)
aggregates          # rollups
```
Only `duplicate_of is None` requirements are planned (reqoach's contract — duplicates are
retained for audit, not deleted).

### `coverage.json` — what's missing (gap-derived atoms)
```
gaps[]:                                  # ~tens per project
  title, severity (critical|high|medium|low), detail
  question          the clarifying question that would close the gap
  grounding[]       which standard/archetype concern justified the flag
  domain, domain_name
domains[]           # per-domain coverage status (16 domains) + addressed[] + gaps[]
enrichments, synthesis
```

### `problem_statement.json` + `coverage_profile.json` — the reference
The structured, provenance-graded Problem Statement (purpose / stakeholders / context /
scope / capabilities / constraints / success_criteria, each field graded
`stated|inferred|assumed` with confidence) and the candidate **archetypes** (from reqoach's
`catalog/project_types/*`). These two set the **planning frame**: the archetype `class`
(`data-dominant | computation-dominant | control-dominant | systems-software |
interaction-client`) and salient domains drive **what kind of task** a requirement becomes
(§4, §5).

---

## 3. Two kinds of plannable item

Planner normalizes both inputs into one **PlanItem** stream so the decomposer treats them
uniformly:

| Source | PlanItem kind | Becomes tasks that… |
|---|---|---|
| `scorecard.requirements[]` (stated) | `requirement` | **implement** what the requirement asks for. |
| `coverage.gaps[]` (missing) | `gap` | **add the missing requirement, then implement it** — or surface its clarifying `question` when the gap is a genuine unknown. |

A `gap` PlanItem is a first-class citizen, not an afterthought: the whole point of ingesting
coverage is that the plan addresses *what should exist*, not only *what was written down*.
Gaps carry their `severity` straight through to task priority, and their `question` is
preserved verbatim when the gap can't be turned into a confident task.

---

## 4. Domain model

```
Plan                       (one planning run over one reqoach project snapshot)
  id, project_ref{ reqoach_project_id, quality_run_id, coverage_run_id, problem_statement_version }
  created_at, frame              # §5 stage 0 — resolved archetypes/class + task-kind policy
  tasks[]                        # the flattened leaf tasks
  graph{ nodes[], edges[] }      # dependency DAG over task ids
  confidence, unresolved[]       # honesty: questions + items that couldn't be reduced

Task                       (a leaf — the unit sized for Gemma)
  task_id            "T-0007"
  title              imperative, one deliverable
  kind               code | test | config | schema | docs | infra | process | spec
  deliverable        the single artifact this task produces (path / entity / doc section)
  instructions       what to do, self-contained (the executor prompt body)
  context_refs[]     pointers the executor needs (source req text, doc section, sibling tasks)
  acceptance         { check, kind: test|command|assertion|review }   # how "done" is proven
  depends_on[]       task_ids that must complete first
  traces_to[]        { source: requirement|gap, id: "REQ-0001"|gap-title, why }
  granularity        { verdict: atomic|split, confidence, rationale, depth }
  priority           from gap severity / requirement criticality
  status             planned            # Planner only ever emits "planned"

PlanItem                   (transient — a requirement or gap before decomposition)
  item_id, kind (requirement|gap), text, domain, archetype_slice, quality_signals{}, source_ref
```

**Key relationships**
- A **Task** `traces_to` one or more PlanItems (requirements and/or gaps). No task without a
  trace. A PlanItem may fan out to many tasks; a task may satisfy part of several PlanItems.
- The **graph** is a DAG over tasks. Edges are `depends_on`. Cross-requirement dependencies
  (shared entities/data model) are discovered in §5 stage 4, not assumed per-requirement.
- The **frame** (archetype `class` + salient domains) is resolved once per plan and picks
  the **task-kind policy**: a `functional` requirement under an `interaction-client` /
  `data-dominant` archetype yields `schema → code → test` tasks; a `legal-privacy` or
  `constraints` requirement yields `process`/`docs`/`spec` tasks. This is the
  "both / configurable" behavior — task type follows the requirement's domain, not a global
  switch.

---

## 5. The planning pipeline

An explicit run over a reqoach project. Stages, streamed as events (§8):

### Stage 0 — Frame  *(runs first, once)*
Load the Problem Statement + Coverage Profile. Resolve the active **archetype(s)** and their
`class`, and derive the **task-kind policy** per domain (§4). Cheap, deterministic-ish: reads
reqoach's catalog and profile, one optional LLM call to reconcile hybrids. Output: `frame`.

### Stage 1 — Normalize
Turn `scorecard.requirements[]` (primary) and `coverage.gaps[]` (missing) into one
**PlanItem** stream (§3). Attach each requirement's **quality_signals** — the C1–C9 scores —
because they drive decomposition (§6). Drop `duplicate_of` requirements.

### Stage 2 — Decompose  (LLM, domain-configured)
For each PlanItem, an LLM (`planner_decompose` preset) proposes **candidate tasks** in the
task-kind vocabulary the frame selected. Compound requirements (low C5) are *expected* to
split; the decomposer is told the requirement's quality signals so it splits along the seams
reqoach already found. A `gap` item first proposes the *missing requirement*, then tasks.

### Stage 3 — Granularity gate  *(the linchpin — recursive)*
A judge (`planner_granularity` preset) scores each candidate task against explicit
**atomicity criteria**:

1. **Single deliverable** — one artifact (one file, one function, one schema, one doc section).
2. **No open design decision** — nothing a builder would have to *decide*; choices are made
   or deferred to a named dependency.
3. **Bounded context** — the instructions + `context_refs` fit comfortably in Gemma's window
   with room to generate the output.
4. **Checkable done-condition** — a concrete acceptance check can be stated (§ stage 5).
5. **No hidden fan-out** — "implement X for all entities" is not atomic; "implement X for
   entity E" is.

Verdict `atomic` → the task is a leaf. Verdict `split` → recurse into Stage 2 on that task,
up to a **bounded depth** (e.g. 3). A task still not atomic at max depth is emitted as a leaf
**flagged `granularity.verdict = "split"` with low confidence** and added to `unresolved[]`
— we never silently pretend an oversized task is Gemma-ready (honesty principle). This is the
direct analog of reqoach's bounded segmentation gate loop.

### Stage 4 — Sequence  (dependencies + ordering)
Build the DAG. Two edge sources:
- **Structural**, from the frame's task-kind policy: `schema → code → test`, data model before
  the endpoints that use it, etc.
- **Semantic**, LLM-discovered (`planner_sequence` preset): tasks that share an entity or
  where one produces what another consumes. This is the plan-time analog of reqoach's
  set-level overlap detection (reranker + LLM confirm) — reuse that machinery if it fits.
Cycle-check; a cycle is an `unresolved[]` item, not a crash.

### Stage 5 — Acceptance
Each leaf task gets an **acceptance** check appropriate to its kind — a test to write, a
command to run, an assertion to hold, or (for `docs`/`process`) a review checklist. This is
what keeps *plan-only* honest: the plan is execution-ready — a future executor (or a human)
knows when each task is done — without Planner running anything.

### Stage 6 — Emit
Write `plan.json` (§7). Compute overall `confidence` from the per-task granularity
confidences and the fraction of items that landed atomic vs. flagged. Every task is traced;
every unresolved item carries its reason.

---

## 6. Quality scores drive decomposition (the reqoach synergy)

The C1–C9 characteristics are not just displayed — they are **decomposition inputs**. This is
the concrete payoff of consuming reqoach's judged output rather than raw requirement text:

| reqoach signal | What Planner does with it |
|---|---|
| **C5 Singular = low** (`was_compound`, R18/R19 triggered) | The requirement bundles N capabilities → decompose into ≥N tasks along the exact seams reqoach flagged. |
| **C3 Unambiguous / C4 Complete = low** | Tasks derived from it are marked **low-confidence**; a clarifying question is attached (or the item goes to `unresolved[]`) rather than fabricating a confident task. |
| **C7 Verifiable = low** | Planner cannot state a real acceptance check → the task's `acceptance.kind = "review"` and the missing verifiability is surfaced. |
| **C6 Feasible / set-level C11 Consistent = low** | Flags likely cross-task conflicts for the Sequence stage to resolve or surface. |
| **coverage `gap.severity`** | Becomes task `priority`; `critical`/`high` gaps sort to the front of the plan. |

A requirement that reqoach scored well decomposes cleanly and confidently; a requirement
reqoach scored poorly produces *flagged* tasks and questions. The plan's honesty is
inherited from the analyzer's honesty.

---

## 7. Output shape (`plan.json`)
```
plan:
  project_ref{ reqoach_project_id, quality_run_id, coverage_run_id, problem_statement_version }
  frame{ archetypes[], class, task_kind_policy{} }
  tasks[]:                 # leaves, each § Task in §4
    task_id, title, kind, deliverable, instructions, context_refs[],
    acceptance{ check, kind }, depends_on[], traces_to[], granularity{}, priority
  graph{ nodes[], edges[] }      # DAG; renders as a dependency view / topological task list
  confidence               # overall, explicitly caveated
  unresolved[]             # { item_ref, reason: too_ambiguous|too_large|cycle, question? }
  produced_in_s
```
Tasks render **priority-colored** (reuse reqoach's `--s1..--s5` criticality scale) and each
carries its `traces_to` so every task is auditable ("why does this task exist?") straight
back to a requirement or a coverage gap — the same auditability contract coverage findings
have.

---

## 8. Stack & reuse

Planner deliberately clones reqoach's proven infrastructure — same deploy model, same LLM
convention, same streaming — so it slots into the ecosystem with no new moving parts.

- **LLM: agent_server presets (`:7701`), the "A1 pattern."** Copy reqoach's 69-line
  `AgentServerClient.complete_json(agent, user_content)` verbatim: the preset name goes in
  `model`, only a user message is sent, `response_format={"type":"json_object"}`, 3-tier JSON
  parse. New presets: `planner_decompose`, `planner_granularity`, `planner_sequence`,
  `planner_frame` — registered idempotently by a `register_planner_agents.py` (POST, 409→PUT),
  exactly like `register_incose_judges.py`. **Every judge degrades, never raises** (bare
  `except` → null/flag), per reqoach's contract.
- **Streaming job core.** Reuse reqoach's `jobs.py` pattern: the pipeline is an
  event-emitting generator; a `task` event fires the moment a leaf passes the granularity
  gate, so a UI fills in live. Events: `stage`, `plan_item`, `task`, `sequence`, `plan`,
  `cancelled`, `error`. Cooperative cancellation via `should_cancel`.
- **Storage.** Mirror projects-mode layout: `store/plans/<plan_id>/plan.json` + `meta.json`
  (or, if Planner lives *inside* reqoach later, `store/projects/<pid>/plans/<run_id>/`).
  Atomic writes (tmp + `os.replace`).
- **Deploy.** Single FastAPI + socket.io container, `network_mode: host`, one origin; reads
  reqoach `store/` (bind-mount, read-only) or its API for input. `Cache-Control: no-cache` on
  static assets.
- **Catalog.** Read reqoach's `catalog/` (domains, archetypes, standards) read-only for the
  frame's task-kind policy — do not fork it.

**Local-only.** Like reqoach, the whole stack runs on the one active local model
(Gemma 4 E4B via agent_server) + the embeddings/reranker service if Stage 4 uses overlap
detection. No data leaves the host.

---

## 9. Non-goals (now) / future

- **Execution.** Planner emits the plan and stops. A separate **Executor** service (drive
  Gemma per task → verify against `acceptance` → loop) is the obvious Phase-next, and the
  Task shape is designed to feed it — but it is explicitly out of scope here.
- **Effort/estimation numbers.** No story points or hour estimates — granularity is a
  binary "Gemma-atomic or flagged," not a size score.
- **Live re-planning / diff vs previous plan** after requirements change — later.
- **Editing the plan in a UI** (accept/reorder/split tasks by hand) — later; the first cut
  emits a read-only plan + dependency view.
- **Cross-project / portfolio planning** — one plan is one reqoach project snapshot.

---

## 10. Open questions

1. **Input coupling.** Read reqoach's `store/` directly (fast, tight coupling, same host) or
   go through its API (clean boundary, needs endpoints)? Leaning: read `store/` read-only for
   the MVP, add an API import later.
2. **In-repo vs. inside reqoach.** Planner as its own service/repo (chosen — this directory)
   vs. a fifth reqoach `kind` alongside quality/coverage/framing. Standalone keeps reqoach
   focused; revisit if the coupling proves heavy.
3. **Granularity calibration.** The atomicity criteria (§5) need tuning against *actual*
   Gemma 4 success/failure on emitted tasks — which requires the Executor to exist to close
   the loop. Until then the gate is judged, not empirically validated; say so.
4. **Task-kind vocabulary.** Is the §4 `kind` set (code/test/config/schema/docs/infra/
   process/spec) sufficient across all 16 coverage domains, or does it need per-class
   variants?
