"""The LLM stages of the validation harness.

- decompose:  requirement -> candidate tasks (the plan)
- feasibility: A PRIORI verdict — can Gemma implement this task in one shot?
- execute:    the calibration ground-truth — actually produce the artifact
- outcome_judge: did the produced artifact satisfy the task?

The feasibility verdict is the *prediction* under test; execute+outcome_judge is
the one-time ground truth we calibrate it against. Every stage degrades to a
safe default rather than raising (reqoach's contract).
"""

from __future__ import annotations

# --------------------------------------------------------------------------- #
# Stage: decompose                                                            #
# --------------------------------------------------------------------------- #
_DECOMPOSE_SYS = (
    "You are a software planning assistant. Given ONE software requirement, break it "
    "into the smallest independent implementation TASKS that together fully satisfy it. "
    "Each task must have exactly one deliverable (one file, function, schema, or config) "
    "and no unresolved design decision. If the requirement is already atomic, return one "
    "task. Prefer 1-5 tasks. Output ONLY JSON: "
    '{"tasks":[{"title":"imperative, one deliverable","kind":"code|test|schema|config|docs",'
    '"deliverable":"the single artifact","instructions":"self-contained, what to build"}]}'
)


def decompose(client, req_text: str, quality_hint: str = "", arch_context: str = "") -> list[dict]:
    user = req_text if not quality_hint else f"{req_text}\n\n[quality note: {quality_hint}]"
    if arch_context:
        # The Architect already decided what the system is made of — decompose against it
        # instead of inventing a structure per task.
        user += ("\n\nARCHITECTURE (authoritative — use these names, do not invent components):\n"
                 + arch_context)
    # Use the registered preset (temp 0, deterministic) — the prompt is identical to the
    # former inline _DECOMPOSE_SYS, so this only reconciles config, not behavior.
    res = client.preset_json("planner_decompose", user)
    if not res or not isinstance(res.get("tasks"), list):
        return []
    tasks = []
    for t in res["tasks"]:
        if isinstance(t, dict) and t.get("title"):
            tasks.append({
                "title": str(t.get("title", "")).strip(),
                "kind": str(t.get("kind", "code")).strip(),
                "deliverable": str(t.get("deliverable", "")).strip(),
                "instructions": str(t.get("instructions", "")).strip(),
            })
    return tasks


# --------------------------------------------------------------------------- #
# Stage: feasibility (a priori — the predictor under test)                    #
# --------------------------------------------------------------------------- #
_FEASIBILITY_SYS = (
    "You judge whether a SMALL local code model (Gemma 4 E4B, ~4B params) could implement "
    "the given task correctly IN ONE SHOT, without running it. Judge feasibility BEFORE any "
    "attempt. Apply these criteria: (1) single deliverable; (2) no open design decision left "
    "to the implementer; (3) instructions self-contained and unambiguous; (4) a concrete "
    "done-condition is statable; (5) no hidden fan-out (e.g. 'for all entities'). "
    "Return verdict 'feasible' only if all hold, 'borderline' if minor risk, 'infeasible' if "
    "a small model would likely fail. Output ONLY JSON: "
    '{"verdict":"feasible|borderline|infeasible","confidence":0.0-1.0,'
    '"rationale":"one sentence","blocking_criterion":"which criterion fails, or none"}'
)


def feasibility(client, task: dict) -> dict:
    user = (f"TITLE: {task['title']}\nKIND: {task['kind']}\n"
            f"DELIVERABLE: {task['deliverable']}\nINSTRUCTIONS: {task['instructions']}")
    res = client.complete_json(_FEASIBILITY_SYS, user, temperature=0.0)
    if not res or res.get("verdict") not in ("feasible", "borderline", "infeasible"):
        return {"verdict": "unknown", "confidence": 0.0,
                "rationale": "judge failed", "blocking_criterion": "unknown"}
    try:
        conf = float(res.get("confidence", 0.0))
    except (TypeError, ValueError):
        conf = 0.0
    return {
        "verdict": res["verdict"],
        "confidence": max(0.0, min(1.0, conf)),
        "rationale": str(res.get("rationale", ""))[:300],
        "blocking_criterion": str(res.get("blocking_criterion", "none"))[:80],
    }


# --------------------------------------------------------------------------- #
# Stage: execute (calibration ground truth)                                   #
# --------------------------------------------------------------------------- #
_EXECUTE_SYS = (
    "You are a senior engineer. Implement the given task. Produce the complete artifact "
    "described by the deliverable (code, schema, config, or doc) — nothing else, no "
    "explanation. If the task is under-specified or too large to do in one shot, output "
    "exactly the single line: CANNOT_IMPLEMENT: <short reason>."
)


def execute(client, task: dict) -> str:
    user = (f"TITLE: {task['title']}\nKIND: {task['kind']}\n"
            f"DELIVERABLE: {task['deliverable']}\nINSTRUCTIONS: {task['instructions']}")
    out = client.complete_text(_EXECUTE_SYS, user, temperature=0.2)
    return out or "CANNOT_IMPLEMENT: no output"


# --------------------------------------------------------------------------- #
# Stage: outcome judge (ground-truth label)                                   #
# --------------------------------------------------------------------------- #
_OUTCOME_SYS = (
    "You grade whether an ATTEMPT satisfies a task's deliverable and instructions. Be "
    "strict: the artifact must be complete, directly usable, and match what was asked. "
    "An 'CANNOT_IMPLEMENT' attempt is a fail. Output ONLY JSON: "
    '{"success":true|false,"confidence":0.0-1.0,"reason":"one sentence"}'
)


def outcome_judge(client, task: dict, attempt: str) -> dict:
    # Full attempt, no truncation: execute is capped at 8192 output tokens, so the
    # judge's input (task + attempt) stays well inside one 32K slot. Judging a
    # truncated artifact would be worse than not judging it.
    user = (f"TASK TITLE: {task['title']}\nDELIVERABLE: {task['deliverable']}\n"
            f"INSTRUCTIONS: {task['instructions']}\n\n--- ATTEMPT ---\n{attempt}")
    res = client.complete_json(_OUTCOME_SYS, user, temperature=0.0)
    if not res or not isinstance(res.get("success"), bool):
        return {"success": None, "confidence": 0.0, "reason": "judge failed"}
    try:
        conf = float(res.get("confidence", 0.0))
    except (TypeError, ValueError):
        conf = 0.0
    return {"success": res["success"], "confidence": max(0.0, min(1.0, conf)),
            "reason": str(res.get("reason", ""))[:300]}
