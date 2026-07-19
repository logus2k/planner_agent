"""The operational planner: a reqoach project -> a validated plan.json for builder_agent.

reqoach project (scorecard requirements + coverage gaps + problem statement)
  -> decompose each requirement into seed tasks (traced to the requirement)
  -> bounded gate/refine loop (feasible / question / flagged)
  -> assemble plan.json: feasible tasks with the full handoff contract + a dependency DAG,
     escalated questions, flagged tasks, and coverage gaps (as escalations to the author).

Local-only (Gemma presets + deterministic code). Every emitted task traces to a source
requirement (anti-hallucination). Coverage gaps are NOT forced through decomposition — a gap
is a MISSING requirement that needs author input, so it is surfaced as an escalation.
"""

from __future__ import annotations

import concurrent.futures
import glob
import json
import os

from . import architecture, loader, stages
from .loop import advance_ids, dedup_plan, plan_tasks


def _acceptance(kind: str) -> dict:
    """Deterministic, kind-based acceptance check builder_agent verifies against."""
    k = (kind or "").lower()
    if k == "test":
        return {"kind": "run", "check": "the test executes and passes"}
    if k in ("schema", "config"):
        return {"kind": "parse", "check": "the file parses as valid and is complete (no placeholders)"}
    if k == "docs":
        return {"kind": "review", "check": "human review of completeness"}
    return {"kind": "build+verify",
            "check": "the artifact compiles/parses and contains no stub/placeholder fingerprints"}


def _plan_one_requirement(client, r, handover, refine_budget, refine_k):
    """Decompose ONE requirement against the architecture and run its gate/refine loop.
    Self-contained (own seeds, own loop) so requirements can run concurrently and each
    result can be checkpointed independently. Returns the loop result dict."""
    ctx = architecture.architecture_context(handover, r.req_id) if handover else ""
    seeds = []
    for t in stages.decompose(client, r.text, arch_context=ctx):
        t["traces_to"] = [r.req_id]
        seeds.append(t)
    res = plan_tasks(client, seeds, refine_budget=refine_budget, refine_k=refine_k,
                     log=lambda *a: None)          # quiet per-req; caller logs a summary
    res["_arch_ctx"] = bool(ctx)
    return res


def plan_project(client, requirements: list, refine_budget: int = 3, refine_k: int = 1,
                 handover: dict | None = None, workers: int = 1, checkpoint=None,
                 log=print) -> dict:
    """Plan a requirement set: process each requirement independently (decompose -> gate/
    refine loop), then globally name (architect precedence) and dedup.

    Per-requirement processing is what makes this both concurrent AND resumable:
      - workers > 1 fans requirements out over a thread pool (the client is thread-safe;
        with 1 backend slot they queue, but it stays correct and speeds up when slots rise).
      - checkpoint (a JSON-lines WAL) commits each requirement's result as it finishes, so a
        restart skips already-done req_ids and plans only the delta.
    A requirement absent from the Architect handover simply gets no context — not an error."""
    prior = []
    done = set()
    if checkpoint is not None:
        prior = checkpoint.load_all()
        done = {rid for rid, _ in prior}
        advance_ids(checkpoint.max_task_num())     # new ids won't collide with committed ones
        if done:
            log(f"resuming: {len(done)} requirement(s) already checkpointed, skipping them")
    todo = [r for r in requirements if r.req_id not in done]
    log(f"planning {len(todo)} requirement(s) (workers={workers})"
        + (f" · handover: {sum(1 for r in todo if architecture.for_requirement(handover, r.req_id))} modelled"
           if handover else ""))

    new_results = []
    total = len(todo) + len(done)

    def _record(rid, res):
        if checkpoint is not None:
            checkpoint.append(rid, res)
        new_results.append((rid, res))
        n = len(new_results) + len(done)
        log(f"  [{n}/{total}] {rid}: {len(res['feasible'])}F {len(res['questions'])}Q "
            f"{len(res['flagged'])}Fl")

    if workers > 1 and todo:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(_plan_one_requirement, client, r, handover, refine_budget, refine_k): r
                    for r in todo}
            for fut in concurrent.futures.as_completed(futs):
                r = futs[fut]
                try:
                    _record(r.req_id, fut.result())
                except Exception as e:             # noqa: BLE001 — one req must not kill the run
                    log(f"  ! {r.req_id} failed: {type(e).__name__}: {e}")
    else:
        for r in todo:
            try:
                _record(r.req_id, _plan_one_requirement(client, r, handover, refine_budget, refine_k))
            except Exception as e:                 # noqa: BLE001
                log(f"  ! {r.req_id} failed: {type(e).__name__}: {e}")

    # aggregate prior (from checkpoint) + newly planned
    agg = {"feasible": [], "questions": [], "flagged": []}
    for _, res in prior + new_results:
        agg["feasible"].extend(res["feasible"])
        agg["questions"].extend(res["questions"])
        agg["flagged"].extend(res["flagged"])

    # Naming BEFORE dedup: the Architect's component names are canonical, so two tasks that
    # resolve to the same component must merge, not overwrite.
    if handover:
        renamed = _apply_architect_naming(agg["feasible"], handover)
        log(f"architect naming applied to {renamed} task(s)")
    # One artifact = one task, across ALL requirements: merge equivalents.
    return dedup_plan(agg, log=log)


def _apply_architect_naming(tasks: list, handover: dict) -> int:
    """Rename deliverables to the Architect's component names (its name wins). Records what
    the planner would have called it, so the divergence stays visible."""
    n = 0
    for t in tasks:
        arch = architecture.architect_deliverable(
            handover, t.traces_to, t.kind,
            task_text=f"{t.title} {t.deliverable} {t.instructions}")
        if arch and arch != t.deliverable:
            t.proposed_deliverable = t.deliverable
            t.deliverable = arch
            t.named_by = "architect"
            n += 1
    return n


def assemble_plan(source: dict, plan: dict, coverage_gaps: list[dict],
                  ps_version, n_requirements: int, handover: dict | None = None,
                  planned_req_ids=None) -> dict:
    """Build the plan.json handoff contract from the loop result.

    `source` is the Analyst readiness/provenance block (analyst.readiness()) — recorded
    verbatim so downstream consumers can branch on `architect_ready` rather than on data
    being present. Tasks trace to Analyst `req_id`s."""
    feasible = plan["feasible"]
    feasible_ids = {t.task_id for t in feasible}
    tasks = []
    for t in feasible:
        # Naming precedence was already applied (before dedup) — just report it.
        deliverable, source_of_name = t.deliverable, t.named_by
        # ACCEPTANCE: cite a validated constraint expression where one exists.
        acc = _acceptance(t.kind)
        cons = [c for rid in t.traces_to for c in architecture.constraints_for(handover, rid)]
        if cons:
            acc = {"kind": "constraint",
                   "check": "; ".join(f"{c['name']}: {c['expression']}" for c in cons),
                   "source": "architect"}
        tasks.append({
            "task_id": t.task_id, "title": t.title, "kind": t.kind,
            "deliverable": deliverable, "deliverable_named_by": source_of_name,
            "planner_proposed_deliverable": t.proposed_deliverable,
            "instructions": t.instructions,
            "acceptance": acc,
            "depends_on": t.depends_on, "traces_to": t.traces_to, "origin": t.origin,
            "feasibility": {"verdict": (t.feasibility or {}).get("verdict"),
                            "reasoning": (t.feasibility or {}).get("reasoning", "")},
        })
    edges = [[t.task_id, dep] for t in feasible for dep in t.depends_on if dep in feasible_ids]
    return {
        "contract_version": "1.0",
        "source": {                      # provenance from the Analyst package (§2.1)
            "producer": "analyst-agent",
            "project_id": source.get("project_id"), "project_name": source.get("project_name"),
            "run_id": source.get("run_id"), "release_status": source.get("release_status"),
            "architect_ready": source.get("architect_ready"),
            "threshold": source.get("threshold"), "blockers": source.get("blockers", []),
            "problem_statement_version": ps_version,
            "requirements_considered": n_requirements,
            "trace_key": "req_id",
        },
        "summary": {"feasible": len(feasible), "questions": len(plan["questions"]),
                    "flagged": len(plan["flagged"]), "coverage_gaps": len(coverage_gaps)},
        "tasks": tasks,
        "questions": [{"task_title": q["task"].title, "question": q["question"],
                       "gap": q["gap"], "traces_to": q["task"].traces_to} for q in plan["questions"]],
        "flagged": [{"task_title": f["task"].title, "reason": f["reason"],
                     "traces_to": f["task"].traces_to} for f in plan["flagged"]],
        "coverage_gaps": coverage_gaps,
        # Architect-flagged issues touching the requirements we planned. NOT decoration:
        # a semantic_defect is valid SysML that may say the wrong thing — do not build silently.
        "architecture_open_issues": architecture.open_issues_for(
            handover, planned_req_ids or [r for t in feasible for r in t.traces_to]),
        "architecture": ({**architecture.readiness(handover),
                          "components_named": len(handover.get("components") or []),
                          "note": "depends_on is interface direction, not build order"}
                         if handover else None),
        "graph": {"nodes": [t.task_id for t in feasible], "edges": edges},
    }
