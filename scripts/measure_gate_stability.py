#!/usr/bin/env python3
"""Measure a feasibility gate's FLIP-PRONENESS and quality.

Runs the gate K times on each frozen task. The loop routes on feasible-vs-not (borderline
and infeasible both go to refine), so the flip that hurts reproducibility is the BINARY
feasible <-> not-feasible one. We report:
  - flip-prone tasks: binary verdict not unanimous across K repeats (the reproducibility risk)
  - borderline share and 3-way distribution (sharpness)
  - vs Claude reference: dangerous (majority feasible while Claude infeasible), binary agreement

    python scripts/measure_gate_stability.py --preset planner_feasibility_reason --repeats 6
"""

import argparse
import concurrent.futures
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from planner.client import GemmaClient  # noqa: E402

TUNE = os.path.join(os.path.dirname(__file__), "..", "data", "validation", "tune")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--preset", default="planner_feasibility_reason")
    ap.add_argument("--repeats", type=int, default=6)
    args = ap.parse_args()
    K = args.repeats

    tasks = json.load(open(os.path.join(TUNE, "frozen_tasks.json")))
    ref = {v["idx"]: v["verdict"] for v in json.load(open(os.path.join(TUNE, "claude_ref.json")))}
    client = GemmaClient()

    def gate_once(args_):
        t, _ = args_
        u = (f"TITLE: {t['title']}\nKIND: {t['kind']}\n"
             f"DELIVERABLE: {t['deliverable']}\nINSTRUCTIONS: {t['instructions']}")
        r = client.preset_json(args.preset, u)
        return t["idx"], (r or {}).get("verdict", "unknown")

    # K repeats x each task
    jobs = [(t, k) for t in tasks for k in range(K)]
    results = {t["idx"]: [] for t in tasks}
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        for idx, v in ex.map(gate_once, jobs):
            results[idx].append(v)

    flip_prone = []
    borderline_total = 0
    dist = Counter()
    dangerous = 0
    binary_agree = 0
    n = 0
    for t in tasks:
        vs = results[t["idx"]]
        dist.update(vs)
        borderline_total += sum(1 for v in vs if v == "borderline")
        binary = ["feasible" if v == "feasible" else "not" for v in vs]
        if len(set(binary)) > 1:
            flip_prone.append((t["idx"], t["title"][:45], Counter(vs)))
        # majority verdict for quality
        maj = Counter(vs).most_common(1)[0][0]
        r = ref.get(t["idx"])
        if r in ("feasible", "borderline", "infeasible"):
            n += 1
            if (maj == "feasible") == (r == "feasible"):
                binary_agree += 1
            if maj == "feasible" and r == "infeasible":
                dangerous += 1

    total_calls = len(tasks) * K
    print(f"preset: {args.preset}  ·  {len(tasks)} tasks x {K} repeats = {total_calls} calls "
          f"({client.total_s:.0f}s)\n")
    print(f"3-way verdict distribution (all repeats): {dict(dist)}")
    print(f"borderline share: {borderline_total}/{total_calls} = {borderline_total/total_calls:.0%}")
    print(f"\n** FLIP-PRONE tasks (binary feasible<->not not unanimous over {K} repeats): "
          f"{len(flip_prone)}/{len(tasks)}")
    for idx, title, c in flip_prone:
        print(f"   idx {idx}: {title}  {dict(c)}")
    print(f"\nquality vs Claude ref (majority verdict):")
    print(f"   dangerous (majority feasible, claude infeasible): {dangerous}  (keep 0)")
    print(f"   binary agreement: {binary_agree}/{n} = {binary_agree/n:.0%}")


if __name__ == "__main__":
    main()
