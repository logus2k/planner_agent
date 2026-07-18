#!/usr/bin/env python3
"""Guided refinement loop: use the feasibility judge's justification to fix a
not-feasible task, then re-judge — the intelligent form of the granularity gate.

For each seed task: judge feasibility (planner_feasibility_reason). If not feasible,
feed the task + its 'missing' gap + blocking criterion to planner_refine, which picks
split / prerequisite / resolve / question. Re-judge any produced tasks. Show before→after.

    python scripts/refine_demo.py --idxs 0,6,10
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from planner.client import GemmaClient  # noqa: E402

TUNE = os.path.join(os.path.dirname(__file__), "..", "data", "validation", "tune")


def judge_feasibility(client, task):
    u = (f"TITLE: {task['title']}\nKIND: {task['kind']}\n"
         f"DELIVERABLE: {task['deliverable']}\nINSTRUCTIONS: {task['instructions']}")
    return client.preset_json("planner_feasibility_reason", u) or {}


def refine(client, task, feas):
    u = (f"TASK TITLE: {task['title']}\nKIND: {task['kind']}\n"
         f"DELIVERABLE: {task['deliverable']}\nINSTRUCTIONS: {task['instructions']}\n"
         f"MISSING (the gap): {feas.get('missing','')}\n"
         f"BLOCKING CRITERION: {feas.get('blocking_criterion','')}")
    return client.preset_json("planner_refine", u) or {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--idxs", required=True)
    args = ap.parse_args()
    tasks = json.load(open(os.path.join(TUNE, "frozen_tasks.json")))
    by_idx = {t["idx"]: t for t in tasks}
    client = GemmaClient()

    for i in [int(x) for x in args.idxs.split(",")]:
        t = by_idx[i]
        feas = judge_feasibility(client, t)
        v = feas.get("verdict")
        print("=" * 78)
        print(f"[idx {i}] {t['title']}  ->  {v}")
        print(f"  why    : {feas.get('reasoning','')[:120]}")
        if v == "feasible":
            print("  (already feasible — no refinement)")
            continue
        print(f"  missing: {feas.get('missing','')[:120]}")
        ref = refine(client, t, feas)
        action = ref.get("action")
        print(f"  --REFINE--> action={action}: {ref.get('rationale','')[:100]}")
        if ref.get("assumption"):
            print(f"     assumption: {ref['assumption'][:110]}")
        if ref.get("question"):
            print(f"     QUESTION: {ref['question'][:110]}")
        dep = ref.get("depends_on_new_task_index")
        new = ref.get("new_tasks") or []
        # re-judge the produced tasks
        for j, nt in enumerate(new):
            nt = {"kind": nt.get("kind", "code"), "title": nt.get("title", ""),
                  "deliverable": nt.get("deliverable", ""),
                  "instructions": nt.get("instructions", "")}
            nf = judge_feasibility(client, nt)
            tag = " (PREREQUISITE)" if dep == j else (f" (depends on #{dep})" if dep is not None else "")
            print(f"     -> [{nf.get('verdict','?'):10}] {nt['title'][:52]}{tag}")
        print()
    print(f"({client.calls} LLM calls)")


if __name__ == "__main__":
    main()
