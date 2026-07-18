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

import glob
import json
import os

from . import architecture, loader, stages
from .loop import plan_tasks


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


def plan_project(client, requirements: list, refine_budget: int = 3, refine_k: int = 1,
                 handover: dict | None = None, log=print) -> dict:
    """Decompose requirements -> seed tasks (traced) -> gate/refine loop.

    When an Architect handover is supplied, each requirement is decomposed AGAINST the
    architecture (component/function/interface names) instead of inventing a structure.
    A requirement absent from the handover simply gets no context — not an error."""
    seeds = []
    with_arch = 0
    for r in requirements:
        ctx = architecture.architecture_context(handover, r.req_id) if handover else ""
        if ctx:
            with_arch += 1
        for t in stages.decompose(client, r.text, arch_context=ctx):
            t["traces_to"] = [r.req_id]
            seeds.append(t)
    extra = f" ({with_arch} with architecture context)" if handover else ""
    log(f"decomposed {len(requirements)} requirements -> {len(seeds)} seed tasks{extra}")
    return plan_tasks(client, seeds, refine_budget=refine_budget, refine_k=refine_k, log=log)


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
        # NAMING PRECEDENCE: if the Architect already named a component for this
        # requirement, its name wins over one we would invent.
        deliverable, source_of_name = t.deliverable, "planner"
        arch_name = architecture.architect_deliverable(
            handover, t.traces_to, t.kind,
            task_text=f"{t.title} {t.deliverable} {t.instructions}")
        if arch_name and arch_name != t.deliverable:
            deliverable, source_of_name = arch_name, "architect"
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
            "planner_proposed_deliverable": t.deliverable if source_of_name == "architect" else None,
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
