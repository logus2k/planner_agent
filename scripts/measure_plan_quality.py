#!/usr/bin/env python3
"""Measure PLAN QUALITY: build the feasible tasks a plan emits, verify deterministically.

Pipeline: seeds -> plan_tasks (gate + refine loop) -> take FEASIBLE tasks -> build each
via opencode -> verify with code only (compile/parse + stub-detector, NO LLM judge) ->
report the feasible-build-clean rate. That rate is the core plan-quality metric: of the
tasks the gate called ready-for-builder, how many actually build without stubs?

    python scripts/measure_plan_quality.py [--frozen 1,2,...] [--k 1] [--cap 12]
"""

import argparse
import json
import os
import sys

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "src"))
sys.path.insert(0, HERE)
from planner.client import GemmaClient  # noqa: E402
from planner.loop import plan_tasks  # noqa: E402
import run_opencode_outcomes as oc  # noqa: E402

TUNE = os.path.join(HERE, "..", "data", "validation", "tune")
OUT = os.path.join(HERE, "..", "data", "validation", "plan_quality")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--frozen", default=None, help="comma idxs of frozen seed tasks (default: all)")
    ap.add_argument("--k", type=int, default=1, help="refine_k self-consistency")
    ap.add_argument("--cap", type=int, default=12, help="max feasible tasks to build")
    ap.add_argument("--retries", type=int, default=2, help="retry builder on no-output (flakiness)")
    args = ap.parse_args()

    all_tasks = json.load(open(os.path.join(TUNE, "frozen_tasks.json")))
    if args.frozen:
        want = {int(i) for i in args.frozen.split(",")}
        seeds = [t for t in all_tasks if t["idx"] in want]
    else:
        seeds = all_tasks
    client = GemmaClient()

    print(f"planning {len(seeds)} seed tasks (refine_k={args.k}) ...")
    plan = plan_tasks(client, seeds, refine_budget=3, refine_k=args.k, log=lambda *a: None)
    feasible = plan["feasible"]
    print(f"plan: {len(feasible)} feasible · {len(plan['questions'])} questions · "
          f"{len(plan['flagged'])} flagged  ({client.calls} planning calls, {client.total_s:.0f}s)")
    build = feasible[: args.cap]
    print(f"building {len(build)} feasible tasks via opencode + deterministic verify ...\n")

    import shutil
    os.makedirs(OUT, exist_ok=True)

    def build_once(td, wd):
        if os.path.isdir(wd):
            shutil.rmtree(wd)
        files, _ = oc.run_opencode(td, wd)
        return files

    RETRIES = args.retries
    rows = []
    for i, t in enumerate(build):
        td = {"title": t.title, "kind": t.kind, "deliverable": t.deliverable,
              "instructions": t.instructions}
        # Retry on no-output (builder flakiness). Any produced artifact ends the retries.
        files, wd, tries = [], None, 0
        for attempt in range(RETRIES + 1):
            wd = os.path.join(OUT, f"task_{i}_{t.task_id}" + (f"_try{attempt}" if attempt else ""))
            files = build_once(td, wd)
            tries = attempt + 1
            if files:
                break

        if not files:
            # Never produced an artifact -> BUILDER reliability issue, not a plan-quality one.
            category, produced, compiles, stubs, clean = "builder_flake", False, False, 0, False
        else:
            produced, compiles, _ = oc.mechanical(files, wd)
            stubs = len(oc.stub_signals(files, wd))
            clean = bool(produced and compiles and stubs < 2)
            category = "clean" if clean else "quality_fail"  # produced but stub/broken

        rows.append({"task_id": t.task_id, "title": t.title, "origin": t.origin,
                     "deliverable": t.deliverable, "files": files, "tries": tries,
                     "produced": produced, "compiles": compiles, "stub_hits": stubs,
                     "clean": clean, "category": category})
        tag = {"clean": "CLEAN", "quality_fail": "QUALITY-FAIL", "builder_flake": "builder-flake"}[category]
        extra = "" if category == "clean" else f"(produced={produced} compiles={compiles} stubs={stubs} tries={tries})"
        print(f"  [{tag:12}] {t.title[:50]:50} {extra}")

    n = len(rows)
    clean = sum(1 for r in rows if r["category"] == "clean")
    qfail = sum(1 for r in rows if r["category"] == "quality_fail")
    flake = sum(1 for r in rows if r["category"] == "builder_flake")
    judged = clean + qfail  # excludes builder flakes
    print("\n" + "=" * 66)
    print(f"feasible tasks built: {n}   (clean {clean} · quality-fail {qfail} · builder-flake {flake})")
    if judged:
        print(f"** GATE PRECISION (clean / non-flake): {clean}/{judged} = {clean/judged:.0%}")
        print("   of ready tasks the builder DID produce, fraction that were real & clean")
    print(f"   builder reliability (produced / total): {n-flake}/{n} = {(n-flake)/n:.0%}" if n else "")
    json.dump({"refine_k": args.k, "n": n, "clean": clean, "quality_fail": qfail,
               "builder_flake": flake, "gate_precision": (clean/judged) if judged else None,
               "rows": rows,
               "questions": [q["question"] for q in plan["questions"]],
               "flagged": [f["task"].title for f in plan["flagged"]]},
              open(os.path.join(OUT, "plan_quality.json"), "w"), indent=1)
    print(f"-> {os.path.join(OUT, 'plan_quality.json')}")


if __name__ == "__main__":
    main()
