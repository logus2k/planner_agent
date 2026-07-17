"""Calibration experiment: does the a-priori feasibility signal predict reality?

For each sampled requirement we run two arms:
  - direct:     treat the whole requirement as one task
  - decomposed: split into tasks (the plan), each judged/executed separately
For every task we record the A-PRIORI signals (feasibility verdict + structural
proxies) and the GROUND-TRUTH outcome (execute -> outcome judge). The analysis
then asks how well the a-priori signals predict the outcome.
"""

from __future__ import annotations

import concurrent.futures
import json
import math
import os
import time

from . import loader, proxies, stages


def _quality_hint(req: loader.Requirement) -> str:
    s = req.quality_signals
    notes = []
    if s.get("C5_singular") is not None and s["C5_singular"] <= 2:
        notes.append("bundles multiple capabilities (split them)")
    if s.get("C4_complete") is not None and s["C4_complete"] <= 2:
        notes.append("incomplete/ambiguous — may need assumptions")
    return "; ".join(notes)


def _run_task(client, task: dict, source_req, depth: int) -> dict:
    """Full record for one task: a-priori signals + ground-truth outcome."""
    feas = stages.feasibility(client, task)
    prox = proxies.compute(task, source_req, depth)
    attempt = stages.execute(client, task)
    outcome = stages.outcome_judge(client, task, attempt)
    return {
        "task": task,
        "feasibility": feas,          # a-priori prediction under test
        "proxies": prox,              # a-priori structural features
        "attempt_len": len(attempt),
        "attempt_head": attempt[:200],
        "cannot_implement": attempt.strip().startswith("CANNOT_IMPLEMENT"),
        "outcome": outcome,           # ground truth
    }


def _process_requirement(client, req) -> dict:
    """All work for one requirement (both arms). Runs in a worker thread; the
    calls inside are sequential, so each worker holds one slot at a time."""
    bkt = loader.bucket_of(req.avg_score)
    # Arm 1 — direct: the whole requirement as a single task.
    direct_task = {
        "title": f"Implement {req.req_id}",
        "kind": "code",
        "deliverable": "implementation satisfying the requirement",
        "instructions": req.text,
    }
    direct = _run_task(client, direct_task, req, depth=-1)
    # Arm 2 — decomposed: plan into tasks, judge/execute each.
    tasks = stages.decompose(client, req.text, _quality_hint(req))
    task_recs = [_run_task(client, t, req, depth=0) for t in tasks]
    return {
        "req_id": req.req_id, "bucket": bkt, "avg_score": req.avg_score,
        "quality_signals": req.quality_signals, "text": req.text,
        "direct": direct, "decomposed": task_recs,
    }


def run(project_dir: str, per_bucket: int, out_dir: str,
        base_url: str | None = None, concurrency: int = 2) -> dict:
    from .client import GemmaClient

    client = GemmaClient(base_url=base_url)
    scorecard = loader.find_scorecard(project_dir)
    reqs = loader.load_requirements(scorecard)
    sample = loader.sample_by_bucket(reqs, per_bucket)
    print(f"scorecard: {scorecard}")
    print(f"requirements: {len(reqs)} total, {len(sample)} sampled "
          f"({per_bucket}/bucket) · concurrency={concurrency} (llama.cpp slots)\n")

    # concurrency workers, each holding one slot -> uses all configured slots
    # without oversubscribing (a 3rd concurrent request would queue on the server).
    records = []
    t0 = time.time()
    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as ex:
        futures = {ex.submit(_process_requirement, client, req): req for req in sample}
        for fut in concurrent.futures.as_completed(futures):
            req = futures[fut]
            done += 1
            try:
                rec = fut.result()
            except Exception as e:  # noqa: BLE001 — never let one req kill the run
                print(f"[{done}/{len(sample)}] {req.req_id} FAILED: "
                      f"{type(e).__name__}: {e}", flush=True)
                continue
            records.append(rec)
            dv = rec["direct"]["feasibility"]["verdict"]
            do = rec["direct"]["outcome"]["success"]
            npass = sum(1 for t in rec["decomposed"] if t["outcome"]["success"])
            print(f"[{done}/{len(sample)}] {rec['req_id']} ({rec['bucket']}) "
                  f"direct[{dv}->{do}] decomposed[{npass}/{len(rec['decomposed'])} pass]",
                  flush=True)

    elapsed = time.time() - t0
    if client.truncated:
        print(f"\n!! {client.truncated} completion(s) hit the token cap — "
              f"inspect before trusting outcomes")
    result = {
        "project_dir": project_dir, "scorecard": scorecard,
        "n_requirements_total": len(reqs), "n_sampled": len(sample),
        "per_bucket": per_bucket, "concurrency": concurrency,
        "elapsed_s": round(elapsed, 1),
        "llm_calls": client.calls, "llm_total_s": round(client.total_s, 1),
        "truncated_completions": client.truncated,
        "records": records,
    }
    os.makedirs(out_dir, exist_ok=True)
    raw_path = os.path.join(out_dir, "validation_raw.json")
    with open(raw_path, "w") as f:
        json.dump(result, f, indent=2)
    print(f"\nraw results -> {raw_path}  ({client.calls} LLM calls, {elapsed:.0f}s)")

    analysis = analyze(result)
    ana_path = os.path.join(out_dir, "validation_analysis.json")
    with open(ana_path, "w") as f:
        json.dump(analysis, f, indent=2)
    report = render_report(result, analysis)
    rep_path = os.path.join(out_dir, "validation_report.md")
    with open(rep_path, "w") as f:
        f.write(report)
    print(f"analysis    -> {ana_path}\nreport      -> {rep_path}")
    print("\n" + report)
    return result


# --------------------------------------------------------------------------- #
# Analysis                                                                    #
# --------------------------------------------------------------------------- #
def _pearson(xs: list[float], ys: list[float]) -> float | None:
    """Correlation (point-biserial when ys is 0/1). None if undefined."""
    pairs = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    n = len(pairs)
    if n < 3:
        return None
    mx = sum(p[0] for p in pairs) / n
    my = sum(p[1] for p in pairs) / n
    num = sum((p[0] - mx) * (p[1] - my) for p in pairs)
    dx = math.sqrt(sum((p[0] - mx) ** 2 for p in pairs))
    dy = math.sqrt(sum((p[1] - my) ** 2 for p in pairs))
    if dx == 0 or dy == 0:
        return None
    return round(num / (dx * dy), 3)


def _all_task_records(result: dict, arm: str) -> list[dict]:
    out = []
    for r in result["records"]:
        if arm == "direct":
            out.append(r["direct"])
        else:
            out.extend(r["decomposed"])
    return out


def analyze(result: dict) -> dict:
    dec = _all_task_records(result, "decomposed")
    direct = _all_task_records(result, "direct")

    def outcome_bool(rec):
        return rec["outcome"]["success"]

    # Confusion of a-priori verdict vs ground truth (decomposed arm).
    conf = {"feasible": {"pass": 0, "fail": 0}, "borderline": {"pass": 0, "fail": 0},
            "infeasible": {"pass": 0, "fail": 0}, "unknown": {"pass": 0, "fail": 0}}
    for rec in dec:
        v = rec["feasibility"]["verdict"]
        o = outcome_bool(rec)
        if o is None or v not in conf:
            continue
        conf[v]["pass" if o else "fail"] += 1

    # Predictor quality: treat verdict=='feasible' as "predict success".
    tp = conf["feasible"]["pass"]
    fp = conf["feasible"]["fail"]
    fn = conf["borderline"]["pass"] + conf["infeasible"]["pass"]
    tn = conf["borderline"]["fail"] + conf["infeasible"]["fail"]
    precision = round(tp / (tp + fp), 3) if (tp + fp) else None
    recall = round(tp / (tp + fn), 3) if (tp + fn) else None
    n_labeled = tp + fp + fn + tn
    accuracy = round((tp + tn) / n_labeled, 3) if n_labeled else None

    # Proxy correlations with success (decomposed arm).
    proxy_keys = ["depth", "context_tokens", "n_fanout_markers", "n_clauses",
                  "n_entities", "src_avg_score", "src_C4_complete",
                  "src_C5_singular", "src_C7_verifiable"]
    labeled = [(rec, outcome_bool(rec)) for rec in dec if outcome_bool(rec) is not None]
    proxy_corr = {}
    for k in proxy_keys:
        xs = [rec["proxies"].get(k) for rec, _ in labeled]
        ys = [1.0 if o else 0.0 for _, o in labeled]
        proxy_corr[k] = _pearson(xs, ys)
    # Feasibility confidence as a predictor too.
    xs = [rec["feasibility"]["confidence"] for rec, _ in labeled]
    ys = [1.0 if o else 0.0 for _, o in labeled]
    proxy_corr["feasibility_confidence"] = _pearson(xs, ys)

    def rate(recs):
        labs = [outcome_bool(r) for r in recs]
        labs = [x for x in labs if x is not None]
        return (round(sum(1 for x in labs if x) / len(labs), 3), len(labs)) if labs else (None, 0)

    dec_rate, dec_n = rate(dec)
    dir_rate, dir_n = rate(direct)

    # Per-bucket success (decomposed).
    per_bucket = {}
    for r in result["records"]:
        b = r["bucket"]
        labs = [t["outcome"]["success"] for t in r["decomposed"]
                if t["outcome"]["success"] is not None]
        d = per_bucket.setdefault(b, {"pass": 0, "n": 0})
        d["pass"] += sum(1 for x in labs if x)
        d["n"] += len(labs)

    return {
        "confusion_verdict_vs_outcome": conf,
        "feasible_as_predictor": {
            "precision": precision, "recall": recall, "accuracy": accuracy,
            "tp": tp, "fp": fp, "fn": fn, "tn": tn, "n_labeled": n_labeled,
        },
        "proxy_correlation_with_success": proxy_corr,
        "success_rate": {
            "decomposed": {"rate": dec_rate, "n": dec_n},
            "direct": {"rate": dir_rate, "n": dir_n},
        },
        "per_bucket_decomposed": {
            b: {"rate": round(d["pass"] / d["n"], 3) if d["n"] else None, "n": d["n"]}
            for b, d in sorted(per_bucket.items())},
    }


def render_report(result: dict, a: dict) -> str:
    L = []
    L.append("# Planner Agent — Feasibility Calibration Report\n")
    L.append(f"- Project dir: `{result['project_dir']}`")
    L.append(f"- Requirements sampled: **{result['n_sampled']}** of "
             f"{result['n_requirements_total']} ({result['per_bucket']}/bucket)")
    L.append(f"- LLM calls: {result['llm_calls']} · wall time: {result['elapsed_s']}s "
             f"· concurrency: {result.get('concurrency', 1)}")
    tc = result.get("truncated_completions", 0)
    flag = "" if tc == 0 else "  ⚠️ inspect — truncated artifacts poison outcomes"
    L.append(f"- Truncated completions (finish_reason=length): **{tc}**{flag}")
    L.append("- Executor & judge: Gemma 4 E4B (same model — self-judged; treat as a "
             "soft/optimistic ground truth, spot-check by hand)\n")

    p = a["feasible_as_predictor"]
    L.append("## Does the a-priori feasibility verdict predict reality?\n")
    L.append(f"Treating verdict=`feasible` as a prediction of success "
             f"(n={p['n_labeled']} labeled tasks):\n")
    L.append(f"- **precision** {p['precision']}  (of tasks called feasible, how many passed)")
    L.append(f"- **recall** {p['recall']}  (of tasks that passed, how many were called feasible)")
    L.append(f"- **accuracy** {p['accuracy']}")
    L.append(f"- confusion: TP={p['tp']} FP={p['fp']} FN={p['fn']} TN={p['tn']}\n")

    L.append("### Verdict × outcome\n")
    L.append("| a-priori verdict | passed | failed |")
    L.append("|---|---|---|")
    for v, d in a["confusion_verdict_vs_outcome"].items():
        if d["pass"] or d["fail"]:
            L.append(f"| {v} | {d['pass']} | {d['fail']} |")
    L.append("")

    L.append("## Which signal predicts feasibility? (correlation with success)\n")
    L.append("Point-biserial correlation of each a-priori signal with the ground-truth "
             "pass/fail. Positive = higher value → more likely to succeed.\n")
    L.append("| signal | corr |")
    L.append("|---|---|")
    for k, v in sorted(a["proxy_correlation_with_success"].items(),
                       key=lambda kv: (kv[1] is None, -(kv[1] or 0))):
        L.append(f"| {k} | {v if v is not None else '—'} |")
    L.append("")

    sr = a["success_rate"]
    L.append("## Decomposition effect (arm comparison)\n")
    L.append(f"- decomposed tasks: success **{sr['decomposed']['rate']}** "
             f"(n={sr['decomposed']['n']})")
    L.append(f"- direct (whole requirement): success **{sr['direct']['rate']}** "
             f"(n={sr['direct']['n']})\n")

    L.append("## Success by quality bucket (decomposed)\n")
    L.append("| bucket | success rate | n tasks |")
    L.append("|---|---|---|")
    for b, d in a["per_bucket_decomposed"].items():
        L.append(f"| {b} | {d['rate']} | {d['n']} |")
    L.append("")

    n = result["n_sampled"]
    if n < 12:
        L.append("> **Pilot run — sample too small for statistical weight.** These numbers "
                 "prove the harness works end-to-end; scale `--per-bucket` up for a real signal.")
    return "\n".join(L)
