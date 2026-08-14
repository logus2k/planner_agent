"""Planner PLAN-PLAUSIBILITY / completeness judge — advisory self-assessment of a plan.

The feasibility stage judges each task IN ISOLATION (can a small model build THIS deliverable
in one shot?). It never asks the plan-level question: do the tasks traced to a requirement,
TAKEN TOGETHER, actually realize that requirement — without inventing work it never asked for?
This judge does, per requirement.

Symmetric to the Analyst `analyst_sense_judge` and the Architect design judge. Same hard lesson
applies: the E4B model OVER-FLAGS completeness ("you should also add a task for X") — coverage
judgement is the unreliable dimension. So the reliable signal we trust is DRIFT (a task traced
to a requirement that does something the requirement never asked for) plus the deterministic
ZERO-TASK case (a requirement with no tasks at all). `complete` is advisory-soft.

ADVISORY, not a gate. Runs through the `planner_plan_judge` agent_server persona (the house
pattern) — the prompt lives on the server, like every other planner role.
"""

from __future__ import annotations

PLAN_JUDGE_AGENT = "planner_plan_judge"


def _fmt_task(t: dict) -> str:
    title = t.get("title", "")
    deliv = t.get("deliverable", "")
    instr = t.get("instructions", "") or ""
    return f"- {title} [deliverable: {deliv}]: {instr}"


def judge_requirement(req_text: str, tasks: list[dict], client) -> dict:
    """Advisory plan verdict for one requirement's tasks. Fails OPEN (complete/plausible=true)
    on empty output. A requirement with ZERO tasks is a deterministic INCOMPLETE — no LLM call.
    Only DRIFT and INCOMPLETE issue types survive; the model's other noise is dropped."""
    if not tasks:
        return {"complete": False, "plausible": True,
                "issues": [{"type": "incomplete", "detail": "No tasks are traced to this requirement."}],
                "confidence": 1.0}
    user = ("REQUIREMENT:\n" + (req_text or "") + "\n\nTASKS decomposed from it:\n"
            + "\n".join(_fmt_task(t) for t in tasks))
    out = client.preset_json(PLAN_JUDGE_AGENT, user) or {}
    raw = out.get("issues") or []
    valid = [i for i in raw if isinstance(i, dict)
             and str(i.get("type", "")).strip().lower().replace("-", "_") in ("drift", "incomplete")]
    drift = [i for i in valid if str(i.get("type", "")).strip().lower() == "drift"]
    incomplete = [i for i in valid if str(i.get("type", "")).strip().lower() == "incomplete"]
    return {"complete": not incomplete, "plausible": not drift,
            "issues": valid, "confidence": out.get("confidence")}


def run(plan: dict, req_text_by_id: dict[str, str], client) -> dict:
    """Judge every requirement's tasks. Groups `plan['tasks']` by `traces_to`. Returns
    {results:{req_id:verdict}, implausible:[req_id,…], incomplete:[req_id,…]}."""
    by_req: dict[str, list[dict]] = {}
    for t in plan.get("tasks") or []:
        for rid in (t.get("traces_to") or []):
            by_req.setdefault(rid, []).append(t)
    # Requirements that exist but got no tasks at all are still worth flagging.
    for rid in req_text_by_id:
        by_req.setdefault(rid, [])

    results: dict[str, dict] = {}
    for rid, tasks in by_req.items():
        results[rid] = judge_requirement(req_text_by_id.get(rid, rid), tasks, client)
    return {"results": results,
            "implausible": sorted(r for r, v in results.items() if not v["plausible"]),
            "incomplete": sorted(r for r, v in results.items() if not v["complete"])}
