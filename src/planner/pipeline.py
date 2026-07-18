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

from . import loader, stages
from .loop import plan_tasks


def find_coverage(project_dir: str) -> str | None:
    hits = sorted(glob.glob(os.path.join(project_dir, "coverage", "*", "coverage.json")))
    return hits[-1] if hits else None


def load_coverage_gaps(project_dir: str) -> list[dict]:
    path = find_coverage(project_dir)
    if not path:
        return []
    data = json.load(open(path))
    out = []
    for g in data.get("gaps", []):
        out.append({"title": g.get("title", ""), "severity": g.get("severity", ""),
                    "detail": g.get("detail", ""), "question": g.get("question", ""),
                    "grounding": g.get("grounding", []), "domain": g.get("domain", ""),
                    "traces_to": "coverage"})
    return out


def problem_statement_version(project_dir: str) -> int | None:
    path = os.path.join(project_dir, "problem_statement.json")
    if not os.path.isfile(path):
        return None
    try:
        return json.load(open(path)).get("version")
    except Exception:  # noqa: BLE001
        return None


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
                 log=print) -> dict:
    """Decompose requirements -> seed tasks (traced) -> gate/refine loop."""
    seeds = []
    for r in requirements:
        for t in stages.decompose(client, r.text):
            t["traces_to"] = [r.req_id]
            seeds.append(t)
    log(f"decomposed {len(requirements)} requirements -> {len(seeds)} seed tasks")
    return plan_tasks(client, seeds, refine_budget=refine_budget, refine_k=refine_k, log=log)


def assemble_plan(project_dir: str, scorecard: str, plan: dict, coverage_gaps: list[dict],
                  ps_version, n_requirements: int) -> dict:
    """Build the plan.json handoff contract from the loop result."""
    feasible = plan["feasible"]
    feasible_ids = {t.task_id for t in feasible}
    tasks = []
    for t in feasible:
        tasks.append({
            "task_id": t.task_id, "title": t.title, "kind": t.kind,
            "deliverable": t.deliverable, "instructions": t.instructions,
            "acceptance": _acceptance(t.kind),
            "depends_on": t.depends_on, "traces_to": t.traces_to, "origin": t.origin,
            "feasibility": {"verdict": (t.feasibility or {}).get("verdict"),
                            "reasoning": (t.feasibility or {}).get("reasoning", "")},
        })
    edges = [[t.task_id, dep] for t in feasible for dep in t.depends_on if dep in feasible_ids]
    return {
        "project_ref": {
            "reqoach_project_dir": project_dir, "scorecard": os.path.basename(scorecard),
            "problem_statement_version": ps_version, "requirements_considered": n_requirements,
        },
        "summary": {"feasible": len(feasible), "questions": len(plan["questions"]),
                    "flagged": len(plan["flagged"]), "coverage_gaps": len(coverage_gaps)},
        "tasks": tasks,
        "questions": [{"task_title": q["task"].title, "question": q["question"],
                       "gap": q["gap"], "traces_to": q["task"].traces_to} for q in plan["questions"]],
        "flagged": [{"task_title": f["task"].title, "reason": f["reason"],
                     "traces_to": f["task"].traces_to} for f in plan["flagged"]],
        "coverage_gaps": coverage_gaps,
        "graph": {"nodes": [t.task_id for t in feasible], "edges": edges},
    }
