#!/usr/bin/env python3
"""Produce a plan.json for builder_agent from a reqoach project.

    python scripts/produce_plan.py <reqoach_project_dir> [--limit N] [--k 1] [--out plan.json]

Reads the project's scorecard requirements (drops duplicates) + coverage gaps + problem
statement, runs decompose -> gate/refine loop, and writes the plan.json handoff contract.
--limit samples N requirements across quality buckets (decomposing all can be a big batch).
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from planner import loader, pipeline  # noqa: E402
from planner.client import GemmaClient  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("project_dir", help="reqoach store/projects/<pid> directory")
    ap.add_argument("--limit", type=int, default=None, help="sample N requirements (default: all)")
    ap.add_argument("--k", type=int, default=1, help="refine_k self-consistency")
    ap.add_argument("--out", default=None, help="output path (default: <project>/plan.json under our data)")
    args = ap.parse_args()

    project_dir = os.path.expanduser(args.project_dir)
    scorecard = loader.find_scorecard(project_dir)
    reqs = loader.load_requirements(scorecard)
    if args.limit:
        reqs = loader.sample_by_bucket(reqs, max(1, args.limit // 4))[: args.limit]
    gaps = pipeline.load_coverage_gaps(project_dir)
    ps_version = pipeline.problem_statement_version(project_dir)
    print(f"reqoach project: {project_dir}")
    print(f"requirements: {len(reqs)} considered · coverage gaps: {len(gaps)} · "
          f"problem_statement v{ps_version}\n")

    client = GemmaClient()
    plan_result = pipeline.plan_project(client, reqs, refine_k=args.k)
    plan = pipeline.assemble_plan(project_dir, scorecard, plan_result, gaps, ps_version, len(reqs))

    out = args.out or os.path.join(os.path.dirname(__file__), "..", "data", "plans",
                                   os.path.basename(project_dir.rstrip("/")) + ".plan.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    json.dump(plan, open(out, "w"), indent=1)

    s = plan["summary"]
    print(f"\nPLAN: {s['feasible']} feasible · {s['questions']} questions · {s['flagged']} flagged · "
          f"{s['coverage_gaps']} coverage gaps  ({client.calls} LLM calls, {client.total_s:.0f}s)")
    print(f"  DAG: {len(plan['graph']['nodes'])} nodes, {len(plan['graph']['edges'])} edges")
    print("\nfeasible tasks (ready for builder_agent):")
    for t in plan["tasks"][:15]:
        dep = f"  depends_on={t['depends_on']}" if t["depends_on"] else ""
        print(f"  {t['task_id']} [{t['kind']}] {t['title'][:52]}  <- {t['traces_to']}{dep}")
    print(f"\n-> {out}")


if __name__ == "__main__":
    main()
