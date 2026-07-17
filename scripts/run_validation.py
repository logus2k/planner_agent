#!/usr/bin/env python3
"""Run the feasibility calibration experiment against a reqoach project.

Usage:
    python scripts/run_validation.py <reqoach_project_dir> [--per-bucket N] [--out DIR]

Example (AI Job Matching Platform):
    python scripts/run_validation.py \
        ~/env/labs/requirements/store/projects/4a5f2e16210a434fae615e3a5bcc7c3c \
        --per-bucket 3
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from planner.experiment import run  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("project_dir", help="reqoach store/projects/<pid> directory")
    ap.add_argument("--per-bucket", type=int, default=3,
                    help="requirements sampled per quality bucket (default 3)")
    ap.add_argument("--out", default=None, help="output dir (default: <repo>/data/validation)")
    ap.add_argument("--base-url", default=None, help="agent_server URL")
    ap.add_argument("--concurrency", type=int, default=2,
                    help="parallel requests = llama.cpp slots (default 2)")
    args = ap.parse_args()

    project_dir = os.path.expanduser(args.project_dir)
    out = args.out or os.path.join(os.path.dirname(__file__), "..", "data", "validation")
    run(project_dir, per_bucket=args.per_bucket, out_dir=out, base_url=args.base_url,
        concurrency=args.concurrency)


if __name__ == "__main__":
    main()
