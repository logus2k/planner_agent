#!/usr/bin/env python3
"""Tune planner_refine's action calibration — the honesty crux: choose `question`
for genuine unknowns instead of fabricating a `resolve`.

Fixed labeled set (build-time judgment as reference). For each not-feasible task:
feasibility judge -> missing/blocking -> planner_refine -> chosen action, compared to
the reference action. Iterate prompts/planner_refine.txt + re-register between runs.

    python scripts/tune_refine.py
"""

import concurrent.futures
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from planner.client import GemmaClient  # noqa: E402

TUNE = os.path.join(os.path.dirname(__file__), "..", "data", "validation", "tune")

# Reference: the correct refine action per task (my build-time judgment).
#   question    = genuine unknown, no reasonable default (formula, provider, business rule)
#   prerequisite= needs a missing artifact another task produces (a schema, a client)
#   split       = too large / multi-concern
#   resolve     = a truly conventional default any engineer would pick
REF = {
    6:  "question",      # matching similarity FORMULA — product decision
    7:  "question",      # ranking/relevance FORMULA — product decision
    13: "question",      # SMS provider + credentials — business/infra decision
    2:  "prerequisite",  # taxonomy service needs the schema (produced elsewhere)
    14: "prerequisite",  # welcome-SMS logic needs the SMS gateway client
    10: "split",         # tutorial mgmt: create+edit+publish UI — multi-concern
    16: "resolve",       # enforce TLS 1.3 — conventional default (e.g. nginx)
    0:  "resolve",       # taxonomy CRUD endpoints — standard REST conventions
}
GENUINE_UNKNOWNS = {i for i, a in REF.items() if a == "question"}


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
    tasks = {t["idx"]: t for t in json.load(open(os.path.join(TUNE, "frozen_tasks.json")))}
    client = GemmaClient()

    def process(idx):
        t = tasks[idx]
        feas = judge_feasibility(client, t)
        ref = refine(client, t, feas)
        return idx, ref.get("action"), (ref.get("question") or ref.get("assumption") or "")[:70]

    got = {}
    detail = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        for idx, action, note in ex.map(process, list(REF)):
            got[idx] = action
            detail[idx] = note

    exact = sum(1 for i in REF if got.get(i) == REF[i])
    q_recall = sum(1 for i in GENUINE_UNKNOWNS if got.get(i) == "question")
    over_q = sum(1 for i in REF if REF[i] != "question" and got.get(i) == "question")
    fabricated = sum(1 for i in GENUINE_UNKNOWNS if got.get(i) == "resolve")

    print(f"tasks: {len(REF)}  ·  LLM calls: {client.calls}  ({client.total_s:.0f}s)\n")
    print(f"{'idx':>3} {'reference':>13} {'chosen':>13}   note")
    for i in sorted(REF):
        mark = "OK " if got.get(i) == REF[i] else "XX "
        print(f"{i:>3} {REF[i]:>13} {str(got.get(i)):>13} {mark} {detail.get(i,'')}")
    print()
    print(f"** QUESTION-RECALL (genuine unknowns -> question): {q_recall}/{len(GENUINE_UNKNOWNS)}"
          f"   [fabricated a resolve instead: {fabricated}]")
    print(f"   over-question (should NOT be question but was): {over_q}")
    print(f"   exact action match: {exact}/{len(REF)}")


if __name__ == "__main__":
    main()
