#!/usr/bin/env python3
"""Held-out validation of a feasibility preset on the 240-task set.

Judges every task in scale60/tasks_for_claude.json with the given preset and compares
to Claude's verdicts already on disk (scale60/claude_verdicts_*.json). This is the
generalization check: the preset was tuned on a 23-task/6-req subset; here we see
whether it holds on 240 tasks it was not tuned against.

    python scripts/validate_feasibility.py [--preset planner_feasibility_reason]
"""

import argparse
import concurrent.futures
import glob
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from planner.client import GemmaClient  # noqa: E402

SC = os.path.join(os.path.dirname(__file__), "..", "data", "validation", "scale60")
ORDER = ["feasible", "borderline", "infeasible"]
SCORE = {"feasible": 2, "borderline": 1, "infeasible": 0}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--preset", default="planner_feasibility_reason")
    preset = ap.parse_args().preset

    tasks = json.load(open(os.path.join(SC, "tasks_for_claude.json")))
    for i, t in enumerate(tasks):
        t["idx"] = i
    ref = {}
    for f in sorted(glob.glob(os.path.join(SC, "claude_verdicts_*.json"))):
        for v in json.load(open(f)):
            ref[v["idx"]] = v["verdict"]
    print(f"preset: {preset}  |  tasks: {len(tasks)}  |  claude refs: {len(ref)}")

    client = GemmaClient()

    def judge(t):
        user = (f"TITLE: {t['title']}\nKIND: {t['kind']}\n"
                f"DELIVERABLE: {t['deliverable']}\nINSTRUCTIONS: {t['instructions']}")
        res = client.preset_json(preset, user)
        v = res.get("verdict") if res else None
        return t["idx"], (v if v in ORDER else "unknown")

    gemma = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        for idx, v in ex.map(judge, tasks):
            gemma[idx] = v

    n = exact = binary = 0
    conf = {a: {b: 0 for b in ORDER} for a in ORDER}
    gdist = {o: 0 for o in ORDER}
    rdist = {o: 0 for o in ORDER}
    dangerous = over_reject = 0
    gs = rs = 0
    for t in tasks:
        g, r = gemma.get(t["idx"]), ref.get(t["idx"])
        if g not in ORDER or r not in ORDER:
            continue
        n += 1
        gdist[g] += 1
        rdist[r] += 1
        conf[r][g] += 1
        gs += SCORE[g]
        rs += SCORE[r]
        if g == r:
            exact += 1
        if (g == "feasible") == (r == "feasible"):
            binary += 1
        if g == "feasible" and r == "infeasible":
            dangerous += 1
        if g == "infeasible" and r in ("feasible", "borderline"):
            over_reject += 1

    lines = []
    lines.append(f"# 240-task feasibility validation — preset `{preset}`\n")
    lines.append(f"- tasks judged: {n} / {len(tasks)}  ·  LLM calls: {client.calls}  ·  {client.total_s:.0f}s")
    if client.truncated:
        lines.append(f"- ⚠️ truncated completions: {client.truncated}")
    lines.append(f"- gemma dist: {gdist}")
    lines.append(f"- claude ref: {rdist}")
    lines.append(f"- mean optimism (2=feas..0=infeas): gemma {gs/n:.2f} · claude {rs/n:.2f}\n")
    lines.append(f"**EXACT 3-way agreement: {100*exact/n:.1f}%** ({exact}/{n})")
    lines.append(f"**BINARY feasible-vs-not: {100*binary/n:.1f}%** ({binary}/{n})\n")
    lines.append(f"- DANGEROUS (gemma feasible, claude infeasible): **{dangerous}**  (keep ~0)")
    lines.append(f"- OVER-REJECT (gemma infeasible, claude feas/border): {over_reject}\n")
    lines.append("confusion (rows=Claude, cols=Gemma):")
    lines.append("| ref\\gemma | " + " | ".join(ORDER) + " |")
    lines.append("|" + "---|" * (len(ORDER) + 1))
    for a in ORDER:
        lines.append(f"| {a} | " + " | ".join(str(conf[a][b]) for b in ORDER) + " |")
    report = "\n".join(lines)
    out = os.path.join(SC, f"validation_feasibility_240_{preset}.md")
    with open(out, "w") as f:
        f.write(report)
    print("\n" + report + f"\n\n-> {out}")


if __name__ == "__main__":
    main()
