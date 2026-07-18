#!/usr/bin/env python3
"""Produce a plan.json from an Analyst Agent project.

    python scripts/produce_plan.py <PROJECT_ID> [--limit N] [--k 1] [--analyst URL] [--out FILE]

Fetches the Analyst handover package (requirements + INCOSE scores + coverage + problem
statement + readiness manifest) in one call, runs decompose -> gate/refine loop, and writes
the plan.json handed to builder_agent. Tasks trace to Analyst `req_id` verbatim.

Readiness: every package is `draft` / architect_ready:false today (the Analyst release gate
isn't built). That is fine for development; the flag is recorded in the plan so downstream
consumers can branch on it rather than assume approval.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from planner import analyst, architecture, loader, pipeline  # noqa: E402
from planner.client import GemmaClient  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("project_id", help="Analyst project id (GET /projects to list)")
    ap.add_argument("--limit", type=int, default=None, help="sample N requirements (default: all)")
    ap.add_argument("--k", type=int, default=1, help="refine_k self-consistency")
    ap.add_argument("--analyst", default=None, help=f"analyst base url (default {analyst.ANALYST_URL})")
    ap.add_argument("--run", default=None, help="pin a specific quality run id")
    ap.add_argument("--out", default=None)
    ap.add_argument("--arch-dir", default=None, help="architect data/architecture root")
    ap.add_argument("--modelled-only", action="store_true",
                    help="plan only requirements the Architect modelled (exercises the join)")
    args = ap.parse_args()

    pkg = analyst.get_package(args.project_id, base_url=args.analyst, run=args.run)
    ready = analyst.readiness(pkg)
    reqs = analyst.requirements_from_package(pkg)
    gaps = analyst.coverage_gaps_from_package(pkg)
    ps_version = analyst.problem_statement_version(pkg)

    print(f"analyst project: {ready['project_name']} ({ready['project_id']})")
    print(f"requirements: {len(reqs)} · coverage gaps: {len(gaps)} · problem_statement v{ps_version}")
    print(f"readiness: release_status={ready['release_status']} architect_ready={ready['architect_ready']}")
    for b in ready["blockers"][:3]:
        print(f"  ! blocker: {b}")
    if not ready["architect_ready"]:
        print("  NOTE: draft input — fine for development, NOT approved input.")

    # Architect handover (optional — absent means we plan without architecture context)
    handover = architecture.load_handover(args.project_id, root=args.arch_dir)
    if handover:
        ar = architecture.readiness(handover)
        modelled = set((handover.get("by_requirement") or {}).keys())
        print(f"architecture: {ar['requirements_modelled']} requirements modelled · "
              f"{len(handover.get('components') or [])} components · "
              f"{len(handover.get('open_issues') or [])} open issues · "
              f"architect_ready={ar['architect_ready']}")
        if args.modelled_only:
            reqs = [r for r in reqs if r.req_id in modelled]
            print(f"  --modelled-only: planning {len(reqs)} architect-modelled requirements")
    else:
        print("architecture: no handover found — planning without it (names will be ours)")

    if args.limit:
        reqs = (reqs[: args.limit] if args.modelled_only
                else loader.sample_by_bucket(reqs, max(1, args.limit // 4))[: args.limit])
        print(f"sampled {len(reqs)} requirements")
    print()

    client = GemmaClient()
    plan_result = pipeline.plan_project(client, reqs, refine_k=args.k, handover=handover)
    plan = pipeline.assemble_plan(ready, plan_result, gaps, ps_version, len(reqs),
                                  handover=handover, planned_req_ids=[r.req_id for r in reqs])

    out = args.out or os.path.join(os.path.dirname(__file__), "..", "data", "plans",
                                   f"{args.project_id}.plan.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    json.dump(plan, open(out, "w"), indent=1)

    s = plan["summary"]
    print(f"\nPLAN: {s['feasible']} feasible · {s['questions']} questions · {s['flagged']} flagged · "
          f"{s['coverage_gaps']} coverage gaps  ({client.calls} LLM calls, {client.total_s:.0f}s)")
    print(f"  DAG: {len(plan['graph']['nodes'])} nodes, {len(plan['graph']['edges'])} edges")
    print("\nfeasible tasks (ready for builder_agent):")
    for t in plan["tasks"][:12]:
        dep = f"  depends_on={t['depends_on']}" if t["depends_on"] else ""
        print(f"  {t['task_id']} [{t['kind']}] {t['title'][:50]}  <- {t['traces_to']}{dep}")
    print(f"\n-> {out}")


if __name__ == "__main__":
    main()
