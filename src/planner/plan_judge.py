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

ADVISORY, not a gate. Uses the planner's inline system-prompt path (like the feasibility /
outcome judges) — no separate agent registration. PLAN_JUDGE_SYSTEM_PROMPT is the source of
truth.
"""

from __future__ import annotations

PLAN_JUDGE_SYSTEM_PROMPT = (
    "You are a software PLAN judge. You are given ONE requirement and the list of TASKS a "
    "planner decomposed it into (each task has a title, a single deliverable, and "
    "instructions). Judge whether the tasks, TAKEN TOGETHER, plausibly realize the "
    "requirement.\n\n"
    "Flag ONLY these — name the category in each issue:\n"
    "1. DRIFT — a specific task whose actual PURPOSE is a DIFFERENT feature or an unrelated "
    "capability: work that belongs to another requirement's domain entirely, not this one. "
    "Name the offending task. This is NARROW. Do NOT flag: reasonable supporting work a stated "
    "capability obviously needs (a schema, model, interface or type DEFINITION, repository, "
    "endpoint, or storage/util helper); a task that merely DEFINES an interface/schema/"
    "signature instead of implementing behavior (that is normal decomposition, not drift); an "
    "IMPLEMENTATION DETAIL a task names (a specific database such as SQLite, a library, a data "
    "type, or an added id/foreign-key field); or extra fields and plumbing that support the "
    "capability. A task is DRIFT only if it implements a capability the requirement never "
    "asks for — not because it is low-level, a definition, or names a technical choice.\n"
    "2. INCOMPLETE — an ESSENTIAL, explicitly-stated part of the requirement that NO task "
    "addresses at all. Only flag a clearly-missing core action the requirement names in "
    "words; do NOT invent extra tasks, do NOT demand tests/validation/error-handling/edge "
    "cases the requirement does not state, and do NOT flag thinness or missing detail. In "
    "particular, do NOT demand a specific internal structure the requirement does not name — "
    "a repository/data-access layer, an interface/abstraction, a schema, a service class, or "
    "any particular decomposition: if a task performs the stated action, the requirement is "
    "covered even when the plan realizes it differently than you would.\n\n"
    "Set \"plausible\": false ONLY if you find a DRIFT task. Set \"complete\": false ONLY if an "
    "explicitly-stated essential part is entirely missing. When unsure, choose true. NEVER "
    "flag: naming, task granularity, ordering, non-functional concerns (auth, logging, "
    "performance) the requirement does not mention, or a task merely being high-level. "
    "CRITICALLY: a task that only DEFINES or DECLARES something (an interface, schema, model, "
    "type, or signature) WITHOUT implementing behavior is NORMAL decomposition — it is NEVER "
    "drift and NEVER incomplete; and a task that adds a field, column, id, or foreign key not "
    "spelled out in the requirement is NOT drift. \"It only defines and does not implement\" is "
    "NOT a valid reason to flag anything.\n\n"
    "Output ONLY JSON: {\"complete\": true|false, \"plausible\": true|false, "
    "\"issues\": [{\"type\": \"drift|incomplete\", \"detail\": \"<one sentence>\"}], "
    "\"confidence\": 0.0-1.0}"
)


def _fmt_task(t: dict) -> str:
    title = t.get("title", "")
    deliv = t.get("deliverable", "")
    instr = (t.get("instructions", "") or "")[:240]
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
    out = client.complete_json(PLAN_JUDGE_SYSTEM_PROMPT, user, temperature=0.0) or {}
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
