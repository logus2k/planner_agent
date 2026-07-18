#!/usr/bin/env python3
"""Measure gate/loop RECALL — does it wrongly escalate buildable tasks?

Precision (measured elsewhere) = of feasible tasks, how many build clean. Recall = of
tasks that were ACTUALLY buildable, how many did the loop keep as feasible vs wrongly
escalate to question/flagged. Two escalation types, handled differently:

  - FLAGGED (loop gave up): force-build + deterministic verify. A CLEAN build = a RECALL
    MISS (the loop over-flagged a buildable task).
  - QUESTION (genuine unknown): a forced build would just FABRICATE the missing decision,
    so we do NOT build these; we print them for dev-time judgment (is the gap real, or an
    obvious default that should have been resolve->feasible?).

    python scripts/measure_recall.py [--frozen ...] [--k 1] [--cap 8] [--retries 2]
"""

import argparse
import json
import os
import shutil
import sys

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "src"))
sys.path.insert(0, HERE)
from planner.client import GemmaClient  # noqa: E402
from planner.loop import plan_tasks  # noqa: E402
import run_opencode_outcomes as oc  # noqa: E402

TUNE = os.path.join(HERE, "..", "data", "validation", "tune")
OUT = os.path.join(HERE, "..", "data", "validation", "recall")


def build_with_retry(td, wd_base, retries):
    files, wd = [], wd_base
    for attempt in range(retries + 1):
        wd = wd_base + (f"_try{attempt}" if attempt else "")
        if os.path.isdir(wd):
            shutil.rmtree(wd)
        files, _ = oc.run_opencode(td, wd)
        if files:
            return files, wd, attempt + 1
    return [], wd, retries + 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--frozen", default=None)
    ap.add_argument("--k", type=int, default=1)
    ap.add_argument("--cap", type=int, default=8)
    ap.add_argument("--retries", type=int, default=2)
    args = ap.parse_args()

    all_tasks = json.load(open(os.path.join(TUNE, "frozen_tasks.json")))
    seeds = ([t for t in all_tasks if t["idx"] in {int(i) for i in args.frozen.split(",")}]
             if args.frozen else all_tasks)
    client = GemmaClient()
    print(f"planning {len(seeds)} seeds (k={args.k}) ...")
    plan = plan_tasks(client, seeds, refine_budget=3, refine_k=args.k, log=lambda *a: None)
    flagged = plan["flagged"]
    questions = plan["questions"]
    print(f"plan: {len(plan['feasible'])} feasible · {len(questions)} questions · {len(flagged)} flagged\n")

    # --- FLAGGED: force-build to test for over-flagging (recall misses) ---
    os.makedirs(OUT, exist_ok=True)
    build = flagged[: args.cap]
    print(f"force-building {len(build)} FLAGGED tasks (clean build = recall MISS) ...")
    rows = []
    for i, f in enumerate(build):
        t = f["task"]
        td = {"title": t.title, "kind": t.kind, "deliverable": t.deliverable,
              "instructions": t.instructions}
        files, wd, tries = build_with_retry(td, os.path.join(OUT, f"flagged_{i}_{t.task_id}"), args.retries)
        if not files:
            cat = "builder_flake"; clean = False; stubs = 0; produced = compiles = False
        else:
            produced, compiles, _ = oc.mechanical(files, wd)
            stubs = len(oc.stub_signals(files, wd, td["title"], td["kind"]))
            clean = bool(produced and compiles and stubs < 2)
            cat = "RECALL-MISS(clean)" if clean else "correctly-flagged(stub/broken)"
        rows.append({"title": t.title, "deliverable": t.deliverable, "clean": clean,
                     "category": cat, "stub_hits": stubs, "tries": tries})
        print(f"  [{cat:32}] {t.title[:46]}")

    n = len(rows)
    misses = sum(1 for r in rows if r["clean"])
    print("\n" + "=" * 66)
    print(f"FLAGGED recall check: {misses}/{n} built clean = recall MISSES "
          f"(loop over-flagged a buildable task)")
    print("  low is good: it means flagged tasks genuinely were not cleanly buildable\n")

    # --- QUESTIONS: surface for dev-time judgment (build would fabricate) ---
    print(f"QUESTIONS ({len(questions)}) — judge dev-time: genuine gap, or obvious default (recall miss)?")
    for q in questions:
        print(f"  ? [{q['task'].title[:40]:40}] {q['question'][:78]}")

    json.dump({"flagged_built": n, "recall_misses": misses, "rows": rows,
               "questions": [{"title": q["task"].title, "question": q["question"]} for q in questions]},
              open(os.path.join(OUT, "recall.json"), "w"), indent=1)
    print(f"\n-> {os.path.join(OUT, 'recall.json')}")


if __name__ == "__main__":
    main()
