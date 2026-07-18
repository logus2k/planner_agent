#!/usr/bin/env python3
"""Concrete-outcome oracle via opencode: drive Gemma to produce REAL files per task,
then score the outcome objectively — replacing the saturated in-prompt self-judge.

Per task: fresh workdir -> `opencode run --auto` generates artifact(s) -> mechanical
checks (produced? compiles?) + a strict judge on the REAL file contents -> compare to
the validated feasibility verdict.

    python scripts/run_opencode_outcomes.py --idxs 1,0,10           # a few examples
    python scripts/run_opencode_outcomes.py --limit 23 [--attach URL]

--attach points runs at a persistent `opencode serve` (skips per-run cold boot).
"""

import argparse
import concurrent.futures
import json
import os
import py_compile
import re
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from planner.client import GemmaClient  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
TUNE = os.path.join(ROOT, "data", "validation", "tune")
OUT = os.path.join(ROOT, "data", "validation", "outcomes")
OPENCODE = os.path.expanduser("~/.opencode/bin/opencode")


def list_files(workdir):
    hits = []
    for root, dirs, files in os.walk(workdir):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for f in files:
            if f.startswith("."):
                continue
            p = os.path.join(root, f)
            if os.path.getsize(p) > 0:
                hits.append(os.path.relpath(p, workdir))
    return sorted(hits)


def run_opencode(task, workdir, attach=None, timeout=420):
    os.makedirs(workdir, exist_ok=True)
    instr = (f"{task['title']}. {task['instructions']} "
             f"Produce the deliverable file named '{task['deliverable']}' with the complete, "
             f"working implementation — no placeholders or TODOs.")
    cmd = [OPENCODE, "run", instr, "--auto", "-m", "local-llama/gemma-4", "--dir", workdir]
    if attach:
        cmd += ["--attach", attach]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           env={**os.environ, "PATH": os.path.expanduser("~/.opencode/bin") + ":" + os.environ["PATH"]})
        out = (r.stdout or "")[-1500:]
    except subprocess.TimeoutExpired:
        out = "TIMEOUT"
    return list_files(workdir), out


# Deterministic stub fingerprints, no LLM needed. HARD signals mean the deliverable is
# incomplete and ALWAYS disqualify. SOFT (mock/simulate) signals mean a FAKE implementation
# — disqualifying for an implementation, but harmless for a DEFINITIONAL deliverable
# (an interface/schema/contract may legitimately ship with a mock example alongside it).
_HARD_PATTERNS = [
    r"\bTODO\b", r"\bFIXME\b", r"NotImplemented", r"raise\s+NotImplementedError",
    r"\bplaceholder\b", r"pass\s*#\s*implement", r"return\s+None\s*#",
]
_SOFT_PATTERNS = [
    r"console\.log\(['\"][^'\"]*API", r"setTimeout\(", r"\bsimulat", r"\bmock",
    r"\bdummy\b",
]
_HARD_RE = re.compile("|".join(_HARD_PATTERNS), re.IGNORECASE)
_SOFT_RE = re.compile("|".join(_SOFT_PATTERNS), re.IGNORECASE)

_DEFN_TITLE = re.compile(r"\b(interface|schema|contract|protocol|data model|definition|structure)\b",
                         re.IGNORECASE)
_DEFN_EXT = (".json", ".yaml", ".yml", ".md", ".proto", ".graphql")


def _is_definitional(title: str, kind: str, files: list) -> bool:
    """A definitional deliverable declares a shape/contract (invents no behavior), so a
    mock alongside it is not a defect. Inferred from task title/kind or artifact type."""
    if kind in ("schema", "config", "docs"):
        return True
    if title and _DEFN_TITLE.search(title):
        return True
    return bool(files) and all(f.lower().endswith(_DEFN_EXT) for f in files)


def stub_signals(files, workdir, title: str = "", kind: str = ""):
    """Count stub fingerprints. HARD always count; SOFT (mock/simulate) count only when the
    deliverable is an IMPLEMENTATION — a definitional artifact (interface/schema) is not a
    stub just because a mock example ships beside it. Fixes the interface-with-mock FP."""
    definitional = _is_definitional(title, kind, files)
    hits = []
    for f in files:
        try:
            body = open(os.path.join(workdir, f)).read()
        except Exception:  # noqa: BLE001
            continue
        hits += [m.group(0) for m in _HARD_RE.finditer(body)]
        if not definitional:
            hits += [m.group(0) for m in _SOFT_RE.finditer(body)]
    return hits


def mechanical(files, workdir):
    """Objective checks: produced any file? do code files compile/parse?"""
    produced = len(files) > 0
    checks = {}
    for f in files:
        p = os.path.join(workdir, f)
        if f.endswith(".py"):
            try:
                py_compile.compile(p, doraise=True)
                checks[f] = "py:ok"
            except py_compile.PyCompileError as e:
                checks[f] = f"py:FAIL {str(e)[:60]}"
        elif f.endswith(".json"):
            try:
                json.load(open(p))
                checks[f] = "json:ok"
            except Exception as e:  # noqa: BLE001
                checks[f] = f"json:FAIL {str(e)[:40]}"
        else:
            checks[f] = "n/a"
    compiles = all(v.endswith("ok") or v == "n/a" for v in checks.values()) if checks else False
    return produced, compiles, checks


def read_artifacts(files, workdir, cap=6000):
    parts = []
    for f in files:
        try:
            body = open(os.path.join(workdir, f)).read()
        except Exception:  # noqa: BLE001
            continue
        parts.append(f"--- {f} ---\n{body}")
    blob = "\n\n".join(parts)
    return blob[:cap]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--idxs", help="comma-separated task idxs")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--attach", default=None, help="persistent opencode serve URL")
    args = ap.parse_args()

    tasks = json.load(open(os.path.join(TUNE, "frozen_tasks.json")))
    ref = {v["idx"]: v["verdict"] for v in json.load(open(os.path.join(TUNE, "claude_ref.json")))}
    if args.idxs:
        want = [int(x) for x in args.idxs.split(",")]
        sel = [t for t in tasks if t["idx"] in want]
    else:
        sel = tasks[: args.limit] if args.limit else tasks
    print(f"outcome run over {len(sel)} tasks · opencode {'attach '+args.attach if args.attach else 'per-run boot'}\n")

    client = GemmaClient()
    rows = []
    for t in sel:
        wd = os.path.join(OUT, f"task_{t['idx']}")
        if os.path.isdir(wd):
            import shutil
            shutil.rmtree(wd)
        print(f"[idx {t['idx']}] {t['title'][:55]} (deliv {t['deliverable']})", flush=True)

        # a-priori feasibility (validated hybrid gate)
        fu = (f"TITLE: {t['title']}\nKIND: {t['kind']}\n"
              f"DELIVERABLE: {t['deliverable']}\nINSTRUCTIONS: {t['instructions']}")
        feas = client.preset_json("planner_feasibility_reason", fu)
        fverdict = (feas or {}).get("verdict", "unknown")

        # concrete outcome via opencode
        files, oclog = run_opencode(t, wd, attach=args.attach)
        produced, compiles, checks = mechanical(files, wd)
        stubs = stub_signals(files, wd, t.get("title", ""), t.get("kind", ""))

        # strict judge on the REAL artifact
        judge = None
        if produced:
            blob = read_artifacts(files, wd)
            ju = (f"TASK TITLE: {t['title']}\nDELIVERABLE: {t['deliverable']}\n"
                  f"INSTRUCTIONS: {t['instructions']}\n\n--- PRODUCED FILES ---\n{blob}")
            jr = client.preset_json("planner_outcome", ju)
            judge = (jr or {}).get("success")

        row = {"idx": t["idx"], "title": t["title"], "deliverable": t["deliverable"],
               "claude_feasibility": ref.get(t["idx"]), "gemma_feasibility": fverdict,
               "files": files, "produced": produced, "compiles": compiles,
               "checks": checks, "judge_success": judge,
               "stub_hits": stubs, "stub_flagged": len(stubs) >= 2}
        rows.append(row)
        print(f"    feasibility: claude={ref.get(t['idx'])} gemma={fverdict}")
        print(f"    files={files}")
        print(f"    produced={produced} compiles={compiles} judge_success={judge}")
        print(f"    STUB-DETECT: {len(stubs)} hits {stubs[:6]} -> flagged={len(stubs) >= 2}")
        print()

    os.makedirs(OUT, exist_ok=True)
    json.dump(rows, open(os.path.join(OUT, "outcomes.json"), "w"), indent=1)
    print(f"-> {os.path.join(OUT, 'outcomes.json')}  ({client.calls} judge calls)")


if __name__ == "__main__":
    main()
