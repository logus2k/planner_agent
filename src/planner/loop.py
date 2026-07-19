"""The planner's operational core: decompose -> gate -> refine, bounded-recursively,
until every task is feasible, escalated as a question, or flagged as unresolvable.

Local-only (Gemma presets + code); no Claude, works offline. This produces the
validated task set planner_agent hands to builder_agent — every emitted task passed
the feasibility gate; genuine unknowns become questions, never fabricated tasks.
"""

from __future__ import annotations

import itertools
import json
import os
import re
from collections import Counter
from dataclasses import dataclass, field

_ids = itertools.count(1)


def advance_ids(past: int) -> None:
    """Restart task-id generation past `past` — used on resume so new tasks don't reuse
    ids already committed to a checkpoint."""
    global _ids
    if past > 0:
        _ids = itertools.count(past + 1)


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
    named_by: str = "planner"                            # planner | architect (naming precedence)
    proposed_deliverable: str | None = None              # what we'd have called it, if renamed


def _norm(s: str) -> str:
    """Normalize for equivalence: lowercase alphanumerics only."""
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def dedup_key(title: str, deliverable: str) -> tuple:
    """Two tasks are the same work if they produce the same artifact, or are the same
    stated task. Deliverable is the stronger signal — two tasks writing one file would
    overwrite each other."""
    d = _norm(os.path.basename((deliverable or "").strip().split()[0])) if deliverable else ""
    return ("deliverable", d) if d else ("title", _norm(title))


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
    # Every task we have created, by equivalence key — so a prerequisite that several
    # tasks need is built ONCE and they all depend on it, instead of each spawning its own.
    created: dict[tuple, PlanTask] = {}
    for t, _, _ in work:
        created.setdefault(dedup_key(t.title, t.deliverable), t)
    reused = 0

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
                created.setdefault(dedup_key(c.title, c.deliverable), c)
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
            # Reuse an equivalent prerequisite if one already exists (several tasks often
            # need the same schema/interface) — wire the edge, don't build it twice.
            key = dedup_key(prereq.title, prereq.deliverable)
            existing = created.get(key)
            if existing is not None and existing.task_id != task.task_id:
                prereq = existing
                reused += 1
                log(f"  [prerequisite REUSED] {task.title[:40]} -> {prereq.title[:32]}")
            else:
                created[key] = prereq
                work.append((prereq, refines + 1, avail))           # build the prereq
                log(f"  [prerequisite] {task.title[:45]} needs -> {prereq.title[:35]}")
            if prereq.task_id not in task.depends_on:
                task.depends_on.append(prereq.task_id)              # wire the edge
            work.append((task, refines + 1, avail + [prereq.deliverable]))  # re-judge w/ prereq
        elif action == "resolve":
            rt = _mk(new[0], "resolved", traces_to=task.traces_to) if new else task
            rt.depends_on = task.depends_on
            work.append((rt, refines + 1, avail))
            log(f"  [resolve] {task.title[:50]} (assume: {ref.get('assumption','')[:40]})")
        else:
            flagged.append({"task": task, "feasibility": feas,
                            "reason": f"refine returned no usable action ({action})"})
            log(f"  [FLAGGED] {task.title[:55]} (bad refine)")

    if reused:
        log(f"  ({reused} prerequisite(s) reused instead of duplicated)")
    return {"feasible": feasible, "questions": questions, "flagged": flagged}


def dedup_plan(plan: dict, log=print) -> dict:
    """Merge equivalent tasks produced from different requirements/branches.

    Two tasks that write the same artifact (or state the same work) are ONE task: keeping
    both makes the builder build it twice, or overwrite it. The survivor absorbs the others'
    traces_to and dependencies, and every dependency pointing at a merged-away task is
    rewired onto the survivor so the DAG stays intact.
    """
    feasible = plan.get("feasible", [])
    # Two tasks are the same work if they share EITHER signal: the same artifact (they would
    # overwrite each other) OR the same stated task (same work, differently named files).
    # Union-find so a chain (A~B by title, B~C by deliverable) collapses to one task.
    parent = {t.task_id: t.task_id for t in feasible}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    first_by_title: dict[str, str] = {}
    first_by_deliv: dict[str, str] = {}
    for t in feasible:
        tk = _norm(t.title)
        dv = (t.deliverable or "").strip()
        dk = _norm(os.path.basename(dv.split()[0])) if dv else ""
        if tk:
            union(first_by_title.setdefault(tk, t.task_id), t.task_id)
        if dk:
            union(first_by_deliv.setdefault(dk, t.task_id), t.task_id)

    alias: dict[str, str] = {}          # merged-away task_id -> surviving task_id
    kept: list[PlanTask] = []
    survivor: dict[str, PlanTask] = {}  # root -> surviving task
    for t in feasible:
        root = find(t.task_id)
        winner = survivor.get(root)
        if winner is None:
            survivor[root] = t
            kept.append(t)
            continue
        alias[t.task_id] = winner.task_id
        for r in t.traces_to:                       # a merged task still serves its requirement
            if r not in winner.traces_to:
                winner.traces_to.append(r)
        for d in t.depends_on:
            if d not in winner.depends_on:
                winner.depends_on.append(d)
        if len(t.instructions or "") > len(winner.instructions or ""):
            winner.instructions = t.instructions    # keep the fuller instructions

    for t in kept:                                   # rewire + drop self-edges
        t.depends_on = sorted({alias.get(d, d) for d in t.depends_on} - {t.task_id})

    # questions/flagged: collapse repeats of the same ask
    def _uniq(items, keyf):
        seen, out = set(), []
        for i in items:
            k = keyf(i)
            if k in seen:
                continue
            seen.add(k)
            out.append(i)
        return out

    questions = _uniq(plan.get("questions", []),
                      lambda q: (_norm(q["task"].title), _norm(q.get("question", ""))))
    flagged = _uniq(plan.get("flagged", []), lambda f: _norm(f["task"].title))

    merged = len(feasible) - len(kept)
    if merged or len(questions) != len(plan.get("questions", [])):
        log(f"  dedup: merged {merged} duplicate task(s); "
            f"questions {len(plan.get('questions', []))}->{len(questions)}, "
            f"flagged {len(plan.get('flagged', []))}->{len(flagged)}")
    return {"feasible": kept, "questions": questions, "flagged": flagged,
            "merged_task_ids": alias}
