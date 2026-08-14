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
# All prompts live in agent_server `planner_*` personas (the house pattern); this module only
# builds the user content and calls the preset by name.


def decompose(client, req_text: str, quality_hint: str = "", arch_context: str = "",
              capabilities: str = "") -> list[dict]:
    user = req_text if not quality_hint else f"{req_text}\n\n[quality note: {quality_hint}]"
    if arch_context:
        # The Architect already decided what the system is made of — decompose against it
        # instead of inventing a structure per task.
        user += ("\n\nARCHITECTURE (authoritative — use these names, do not invent components):\n"
                 + arch_context)
    if capabilities:
        # A requirement backed by a provided capability → a task that CALLS it, not one that
        # builds the model/algorithm (already a formatted block from capabilities.block()).
        user += capabilities
    # The prompt + sampling live in the `planner_decompose` persona (the house pattern); we send
    # only the user content.
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
def feasibility(client, task: dict) -> dict:
    user = (f"TITLE: {task['title']}\nKIND: {task['kind']}\n"
            f"DELIVERABLE: {task['deliverable']}\nINSTRUCTIONS: {task['instructions']}")
    res = client.preset_json("planner_feasibility", user)
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
def execute(client, task: dict) -> str:
    user = (f"TITLE: {task['title']}\nKIND: {task['kind']}\n"
            f"DELIVERABLE: {task['deliverable']}\nINSTRUCTIONS: {task['instructions']}")
    out = client.preset_text("planner_execute", user)
    return out or "CANNOT_IMPLEMENT: no output"


# --------------------------------------------------------------------------- #
# Stage: outcome judge (ground-truth label)                                   #
# --------------------------------------------------------------------------- #
def outcome_judge(client, task: dict, attempt: str) -> dict:
    # Full attempt, no truncation: execute is capped at 8192 output tokens, so the
    # judge's input (task + attempt) stays well inside one 32K slot. Judging a
    # truncated artifact would be worse than not judging it.
    user = (f"TASK TITLE: {task['title']}\nDELIVERABLE: {task['deliverable']}\n"
            f"INSTRUCTIONS: {task['instructions']}\n\n--- ATTEMPT ---\n{attempt}")
    res = client.preset_json("planner_outcome", user)
    if not res or not isinstance(res.get("success"), bool):
        return {"success": None, "confidence": 0.0, "reason": "judge failed"}
    try:
        conf = float(res.get("confidence", 0.0))
    except (TypeError, ValueError):
        conf = 0.0
    return {"success": res["success"], "confidence": max(0.0, min(1.0, conf)),
            "reason": str(res.get("reason", ""))[:300]}
