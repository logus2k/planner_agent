"""The planner's operational core: decompose -> gate -> refine, bounded-recursively,
until every task is feasible, escalated as a question, or flagged as unresolvable.

Local-only (Gemma presets + code); no Claude, works offline. This produces the
validated task set planner_agent hands to builder_agent — every emitted task passed
the feasibility gate; genuine unknowns become questions, never fabricated tasks.
"""

from __future__ import annotations

import itertools
import json
from collections import Counter
from dataclasses import dataclass, field

_ids = itertools.count(1)


@dataclass
class PlanTask:
    task_id: str
    title: str
    kind: str
    deliverable: str
    instructions: str
    depends_on: list[str] = field(default_factory=list)
    feasibility: dict | None = None      # the gate verdict that admitted it
    origin: str = "decomposed"           # decomposed | split | prerequisite | resolved
    traces_to: list[str] = field(default_factory=list)   # source requirement id(s) — no task without a trace


def _mk(t: dict, origin: str, traces_to=None) -> PlanTask:
    return PlanTask(task_id=f"T{next(_ids):03d}", title=t.get("title", ""),
                    kind=t.get("kind", "code"), deliverable=t.get("deliverable", ""),
                    instructions=t.get("instructions", ""), origin=origin,
                    traces_to=list(traces_to if traces_to is not None else t.get("traces_to") or []))


def _gate(client, task: PlanTask, available: list[str] | None = None) -> dict:
    """Feasibility gate. `available` lists prerequisite deliverables assumed present
    (prerequisite-aware re-judge — fixes 'dependent task infeasible forever')."""
    u = (f"TITLE: {task.title}\nKIND: {task.kind}\n"
         f"DELIVERABLE: {task.deliverable}\nINSTRUCTIONS: {task.instructions}")
    if available:
        u += ("\nAVAILABLE PREREQUISITES (assume these already exist and are usable): "
              + "; ".join(available))
    return client.preset_json("planner_feasibility_reason", u) or {"verdict": "unknown"}


def _refine_once(client, task: PlanTask, feas: dict) -> dict:
    u = (f"TASK TITLE: {task.title}\nKIND: {task.kind}\n"
         f"DELIVERABLE: {task.deliverable}\nINSTRUCTIONS: {task.instructions}\n"
         f"MISSING (the gap): {feas.get('missing','')}\n"
         f"BLOCKING CRITERION: {feas.get('blocking_criterion','')}")
    return client.preset_json("planner_refine", u) or {}


def _title_key(ref: dict) -> tuple:
    """Canonical key for a refine result: (action, sorted new-task titles)."""
    return (ref.get("action"),
            tuple(sorted((t.get("title", "") for t in (ref.get("new_tasks") or [])))))


def _refine(client, task: PlanTask, feas: dict, k: int = 1) -> dict:
    """Self-consistent refine. With k=1, a single call. With k>1, sample k times,
    MAJORITY-VOTE the action (the categorical decision that drives control flow),
    then among the winning-action samples pick the modal (title-set) representative —
    deterministic tie-break. Turns per-call generative variation into a stable consensus."""
    if k <= 1:
        return _refine_once(client, task, feas)
    samples = [_refine_once(client, task, feas) for _ in range(k)]
    samples = [s for s in samples if s.get("action")]
    if not samples:
        return {}
    maj_action = Counter(s["action"] for s in samples).most_common(1)[0][0]
    winners = [s for s in samples if s["action"] == maj_action]
    # representative: modal title-set among winners; tie-break by canonical JSON.
    key_counts = Counter(_title_key(s) for s in winners)
    top_key = key_counts.most_common(1)[0][0]
    reps = [s for s in winners if _title_key(s) == top_key]
    return sorted(reps, key=lambda s: json.dumps(s, sort_keys=True))[0]


def plan_tasks(client, seed_tasks: list[dict], refine_budget: int = 3,
               refine_k: int = 1, log=print) -> dict:
    """Run the bounded gate->refine loop over seed tasks. Returns the plan:
    feasible tasks (+ dependency edges), escalated questions, and flagged tasks.

    Termination guard: refine_budget. `refines` counts how many times a task lineage
    has been REPROCESSED — by any mechanism: a split (deeper), a prerequisite re-judge,
    or an in-place resolve (same level, still counts). It increments on every re-queue.
    When a lineage has been refined refine_budget times and still isn't feasible, it is
    flagged, never re-queued. Feasible and question outcomes are terminal. So a task
    can be revisited at most refine_budget times => the loop always stops.
    """
    # work items: (PlanTask, refines_so_far, available_prereq_deliverables)
    work = [(_mk(t, "decomposed"), 0, []) for t in seed_tasks]
    feasible: list[PlanTask] = []
    questions: list[dict] = []
    flagged: list[dict] = []

    while work:
        task, refines, avail = work.pop(0)
        feas = _gate(client, task, avail)
        v = feas.get("verdict")
        if v == "feasible":
            task.feasibility = feas
            feasible.append(task)
            log(f"  [feasible] {task.title[:60]}")
            continue
        if refines >= refine_budget:
            flagged.append({"task": task, "feasibility": feas,
                            "reason": f"did not converge within refine_budget ({refine_budget})"})
            log(f"  [FLAGGED after {refines} refines] {task.title[:52]} ({v})")
            continue

        ref = _refine(client, task, feas, k=refine_k)
        action = ref.get("action")
        new = ref.get("new_tasks") or []
        if action == "question":
            questions.append({"task": task, "question": ref.get("question", ""),
                              "gap": feas.get("missing", "")})
            log(f"  [QUESTION] {task.title[:45]} -> {ref.get('question','')[:70]}")
        elif action == "split":
            children = [_mk(nt, "split", traces_to=task.traces_to) for nt in new]
            for c in children:
                work.append((c, refines + 1, avail))
            log(f"  [split -> {len(children)}] {task.title[:50]}")
        elif action == "prerequisite":
            di = ref.get("depends_on_new_task_index")
            prereq = _mk(new[di], "prerequisite", traces_to=task.traces_to) if (
                new and di is not None and di < len(new)) else None
            if prereq is None:
                flagged.append({"task": task, "feasibility": feas,
                                "reason": "prerequisite action without a valid new task"})
                continue
            work.append((prereq, refines + 1, avail))               # build the prereq
            task.depends_on.append(prereq.task_id)                  # wire the edge
            work.append((task, refines + 1, avail + [prereq.deliverable]))  # re-judge w/ prereq
            log(f"  [prerequisite] {task.title[:45]} needs -> {prereq.title[:35]}")
        elif action == "resolve":
            rt = _mk(new[0], "resolved", traces_to=task.traces_to) if new else task
            rt.depends_on = task.depends_on
            work.append((rt, refines + 1, avail))
            log(f"  [resolve] {task.title[:50]} (assume: {ref.get('assumption','')[:40]})")
        else:
            flagged.append({"task": task, "feasibility": feas,
                            "reason": f"refine returned no usable action ({action})"})
            log(f"  [FLAGGED] {task.title[:55]} (bad refine)")

    return {"feasible": feasible, "questions": questions, "flagged": flagged}
