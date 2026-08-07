"""Reranker access for the planner (embeddings_server `/v1/rerank`).

Used to decide whether two tasks are THE SAME WORK before the dedup step merges them.
A shared deliverable filename (`reservation.py`) is NOT proof of sameness — many distinct
endpoints/models legitimately live in one file — so string-matching on title/filename
over-merges, collapsing distinct operations on one entity into a single task and
shotgunning its `traces_to` across unrelated requirements. The reranker scores true
same-work (~0.99) vs same-entity-different-work (~0.07), so a fixed threshold separates them.

Stdlib only (like client.py) so the harness has no extra dependencies. Fails OPEN by
returning [] — the caller then treats "unconfirmed" as "do not merge" (over-merge is the harm).
"""

from __future__ import annotations

import json
import math
import os
import urllib.error
import urllib.request

EMBEDDINGS_URL = os.environ.get("EMBEDDINGS_URL", "http://localhost:8601").rstrip("/")
RERANK_MODEL = os.environ.get("RERANK_MODEL_NAME", "bge-reranker")

_warned = False


def _sigmoid(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    e = math.exp(x)
    return e / (1.0 + e)


def rerank(query: str, documents: list[str], timeout: float = 30.0) -> list[float]:
    """One 0-1 relevance score per document, in INPUT order. Sigmoid of the raw logit so a
    fixed threshold is meaningful. Returns [] on empty input or any transport/parse failure
    (fail open — the caller decides, and for merging that means "do not merge")."""
    global _warned
    if not documents:
        return []
    payload = {"model": RERANK_MODEL, "query": query, "documents": list(documents)}
    req = urllib.request.Request(
        f"{EMBEDDINGS_URL}/v1/rerank", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode())
    except (urllib.error.URLError, ValueError, TimeoutError) as e:
        if not _warned:
            print(f"    ! rerank unavailable ({type(e).__name__}: {str(e)[:80]}) — "
                  f"tasks will NOT be merged without confirmation")
            _warned = True
        return []
    results = body.get("results") or body.get("data") or []
    scored = [0.0] * len(documents)
    for item in results:
        idx = int(item.get("index", 0))
        raw = item.get("relevance_score", item.get("score", 0.0))
        if 0 <= idx < len(scored):
            scored[idx] = _sigmoid(float(raw))
    return scored
