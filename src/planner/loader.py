"""Load a reqoach quality scorecard and select requirements by quality bucket.

Input is a reqoach project's latest quality run (`scorecard.json`). We drop
duplicates (reqoach's contract) and compute a per-requirement average of the
present C1..C9 scores as the primary "quality threshold" axis.
"""

from __future__ import annotations

import glob
import json
import os
from dataclasses import dataclass, field


@dataclass
class Requirement:
    req_id: str           # the TRACE KEY (Analyst `req_id`, e.g. REQ-0005) — use verbatim
    text: str
    section: str
    avg_score: float
    scores: dict          # {"C1": 5, "C4": 2, ...}
    quality_signals: dict = field(default_factory=dict)   # C4/C5/C7 pulled out
    # Analyst routing contract (empty until the Analyst's classify:run has been run):
    classes: list = field(default_factory=list)       # functional|structural|interface|…
    constraints: list = field(default_factory=list)   # closed vocab: latency, throughput, …


def _avg(scores: dict) -> float:
    vals = [v for v in scores.values() if isinstance(v, (int, float))]
    return round(sum(vals) / len(vals), 3) if vals else 0.0


def find_scorecard(project_dir: str) -> str:
    """Return the scorecard.json under a reqoach project's quality runs."""
    hits = sorted(glob.glob(os.path.join(project_dir, "quality", "*", "scorecard.json")))
    if not hits:
        raise FileNotFoundError(f"no scorecard.json under {project_dir}/quality/*/")
    return hits[-1]


def load_requirements(scorecard_path: str) -> list[Requirement]:
    with open(scorecard_path) as f:
        data = json.load(f)
    out: list[Requirement] = []
    for r in data.get("requirements", []):
        if r.get("lineage", {}).get("duplicate_of") is not None:
            continue
        scores = {cid: c.get("score") for cid, c in r.get("characteristics", {}).items()
                  if c.get("score") is not None}
        if not scores:
            continue
        prov = r.get("provenance", {})
        out.append(Requirement(
            req_id=r["req_id"],
            text=r["text"].strip(),
            section=(prov.get("section_path") or "")[:80],
            avg_score=_avg(scores),
            scores=scores,
            quality_signals={
                "C4_complete": scores.get("C4"),
                "C5_singular": scores.get("C5"),
                "C7_verifiable": scores.get("C7"),
            },
        ))
    return out


# Bucket edges (avg C-score). Half-open [lo, hi).
BUCKETS = [
    ("hi",  4.3, 5.01),
    ("mid", 4.0, 4.3),
    ("low", 3.5, 4.0),
    ("poor", 0.0, 3.5),
]


def bucket_of(avg: float) -> str:
    for name, lo, hi in BUCKETS:
        if lo <= avg < hi:
            return name
    return "poor"


def sample_by_bucket(reqs: list[Requirement], per_bucket: int,
                     seed: int = 7) -> list[Requirement]:
    """Deterministically sample up to `per_bucket` requirements from each bucket.

    Deterministic (no RNG import needed): sort each bucket by req_id and take an
    evenly-spaced stride so the sample spans the bucket rather than clustering.
    """
    chosen: list[Requirement] = []
    for name, lo, hi in BUCKETS:
        pool = sorted((r for r in reqs if lo <= r.avg_score < hi),
                      key=lambda r: r.req_id)
        if not pool:
            continue
        if len(pool) <= per_bucket:
            chosen.extend(pool)
            continue
        stride = len(pool) / per_bucket
        chosen.extend(pool[int((i + 0.5) * stride)] for i in range(per_bucket))
    return chosen
