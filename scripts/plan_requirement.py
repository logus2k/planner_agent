#!/usr/bin/env python3
"""Demo the bounded gate->refine loop end-to-end on real reqoach requirements.

Decompose each requirement (planner_decompose) into seed tasks, then run the loop
until every task is feasible / a question / flagged. Prints the resulting plan.

    python scripts/plan_requirement.py --reqoach <project_dir> --idxs 6,10 [--depth 3]
    python scripts/plan_requirement.py --frozen 2,6,10                 # use frozen tasks
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from planner import loader, stages  # noqa: E402
from planner.client import GemmaClient  # noqa: E402
from planner.loop import plan_tasks  # noqa: E402

TUNE = os.path.join(os.path.dirname(__file__), "..", "data", "validation", "tune")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reqoach", help="reqoach project dir (decompose its requirements)")
    ap.add_argument("--idxs", help="requirement sample idxs (with --reqoach)")
    ap.add_argument("--frozen", help="comma idxs of frozen tasks to use as seeds directly")
    ap.add_argument("--budget", type=int, default=3, help="refine_budget: max reprocessings per task lineage")
    args = ap.parse_args()
    client = GemmaClient()

    seeds = []
    if args.frozen:
        tasks = {t["idx"]: t for t in json.load(open(os.path.join(TUNE, "frozen_tasks.json")))}
        seeds = [tasks[int(i)] for i in args.frozen.split(",")]
        print(f"seeds: {len(seeds)} frozen tasks\n")
    else:
        sc = loader.find_scorecard(args.reqoach)
        reqs = loader.load_requirements(sc)
        sample = loader.sample_by_bucket(reqs, 2)
        pick = [sample[int(i)] for i in (args.idxs or "0").split(",")]
        for r in pick:
            print(f"REQUIREMENT {r.req_id}: {r.text[:90]}")
            dec = stages.decompose(client, r.text)
            print(f"  decomposed -> {len(dec)} seed tasks")
            seeds.extend(dec)
        print()

    print("=== loop ===")
    plan = plan_tasks(client, seeds, refine_budget=args.budget)

    print("\n" + "=" * 70)
    print(f"PLAN: {len(plan['feasible'])} feasible · {len(plan['questions'])} questions · "
          f"{len(plan['flagged'])} flagged  ({client.calls} LLM calls, {client.total_s:.0f}s)")
    print("\nFEASIBLE (ready for builder_agent):")
    for t in plan["feasible"]:
        dep = f"  depends_on={t.depends_on}" if t.depends_on else ""
        print(f"  {t.task_id} [{t.origin}] {t.title[:60]}{dep}")
    if plan["questions"]:
        print("\nESCALATED QUESTIONS (need requirement/stakeholder input — NOT built):")
        for q in plan["questions"]:
            print(f"  ? {q['task'].title[:45]} -> {q['question'][:80]}")
    if plan["flagged"]:
        print("\nFLAGGED (did not converge):")
        for f in plan["flagged"]:
            print(f"  ! {f['task'].title[:55]} ({f['reason']})")

    out = os.path.join(TUNE, "plan_demo.json")
    json.dump({
        "feasible": [vars(t) for t in plan["feasible"]],
        "questions": [{"title": q["task"].title, "question": q["question"]} for q in plan["questions"]],
        "flagged": [{"title": f["task"].title, "reason": f["reason"]} for f in plan["flagged"]],
    }, open(out, "w"), indent=1)
    print(f"\n-> {out}")


if __name__ == "__main__":
    main()
