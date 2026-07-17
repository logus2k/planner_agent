#!/usr/bin/env python3
"""Tune the planner_feasibility preset against Claude's reference verdicts.

Fixed inputs: frozen tasks (data/validation/tune/frozen_tasks.json) + Claude's
calibrated reference (claude_ref.json). Each run calls the CURRENT planner_feasibility
preset on agent_server for every task and reports agreement with the reference.

Loop: edit prompts/planner_feasibility.txt -> `python scripts/register_planner_agents.py
planner_feasibility` -> `python scripts/tune_feasibility.py`. The task set and reference
never change, so agreement across runs isolates the prompt as the only variable.
"""

import argparse
import concurrent.futures
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from planner.client import GemmaClient  # noqa: E402

TUNE = os.path.join(os.path.dirname(__file__), "..", "data", "validation", "tune")
ORDER = ["feasible", "borderline", "infeasible"]
SCORE = {"feasible": 2, "borderline": 1, "infeasible": 0}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--preset", default="planner_feasibility")
    preset = ap.parse_args().preset
    print(f"preset: {preset}")
    tasks = json.load(open(os.path.join(TUNE, "frozen_tasks.json")))
    ref = {v["idx"]: v["verdict"] for v in json.load(open(os.path.join(TUNE, "claude_ref.json")))}
    client = GemmaClient()

    def judge(t):
        user = (f"TITLE: {t['title']}\nKIND: {t['kind']}\n"
                f"DELIVERABLE: {t['deliverable']}\nINSTRUCTIONS: {t['instructions']}")
        res = client.preset_json(preset, user)
        v = res.get("verdict") if res else None
        miss = (res.get("missing") or "") if res else ""
        reason = (res.get("reasoning") or "") if res else ""
        return t["idx"], (v if v in ORDER else "unknown"), miss, reason

    gemma = {}
    missing = {}
    reasoning = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        for idx, v, miss, reason in ex.map(judge, tasks):
            gemma[idx] = v
            missing[idx] = miss
            reasoning[idx] = reason

    # metrics vs reference
    n = exact = 0
    conf = {a: {b: 0 for b in ORDER} for a in ORDER}
    gdist = {o: 0 for o in ORDER}
    rdist = {o: 0 for o in ORDER}
    dangerous = []      # gemma feasible but claude infeasible (must stay ~0)
    over_reject = []    # gemma infeasible but claude feasible/borderline
    gs = rs = 0
    for t in tasks:
        i = t["idx"]
        g, r = gemma.get(i), ref.get(i)
        if g not in ORDER or r not in ORDER:
            continue
        n += 1
        gdist[g] += 1
        rdist[r] += 1
        conf[r][g] += 1              # rows = reference (Claude), cols = gemma
        gs += SCORE[g]
        rs += SCORE[r]
        if g == r:
            exact += 1
        if g == "feasible" and r == "infeasible":
            dangerous.append((t["req_id"], t["title"][:55], g, r))
        if g == "infeasible" and r in ("feasible", "borderline"):
            over_reject.append((t["req_id"], t["title"][:55], r))

    binary = sum(1 for t in tasks
                 if gemma.get(t["idx"]) in ORDER and ref.get(t["idx"]) in ORDER
                 and (gemma[t["idx"]] == "feasible") == (ref[t["idx"]] == "feasible"))

    print(f"tasks judged: {n}  |  LLM calls: {client.calls}  ({client.total_s:.0f}s)")
    print(f"gemma dist : {gdist}")
    print(f"claude ref : {rdist}")
    print(f"mean optimism (2=feas..0=infeas): gemma {gs/n:.2f}  claude {rs/n:.2f}")
    print()
    print(f"EXACT 3-way agreement : {100*exact/n:.1f}%  ({exact}/{n})")
    print(f"BINARY feas-vs-not    : {100*binary/n:.1f}%  ({binary}/{n})")
    print()
    print("confusion (rows=Claude ref, cols=Gemma):")
    print("%-12s %s" % ("", "".join("%11s" % o for o in ORDER)))
    for a in ORDER:
        print("%-12s %s" % (a, "".join("%11d" % conf[a][b] for b in ORDER)))
    print()
    print(f"DANGEROUS (gemma=feasible, claude=infeasible): {len(dangerous)}  <- keep ~0")
    for r, ti, g, rr in dangerous:
        print(f"   {r} :: {ti}")
    print(f"OVER-REJECT (gemma=infeasible, claude=feas/border): {len(over_reject)}  <- shrink this")
    for r, ti, rr in over_reject:
        print(f"   {r} [claude={rr}] :: {ti}")

    # Show the self-justifying reasoning + the constructive "missing" gap.
    print("\nsample judgements (reasoning justifies the verdict; missing = the gap):")
    shown = 0
    for t in tasks:
        i = t["idx"]
        if reasoning.get(i) or missing.get(i):
            print(f"   [{gemma[i]:10}] {t['title'][:42]:42}")
            if reasoning.get(i):
                print(f"        why : {reasoning[i][:110]}")
            if missing.get(i):
                print(f"        gap : {missing[i][:110]}")
            shown += 1
        if shown >= 8:
            break


if __name__ == "__main__":
    main()
