"""Analyst Agent client — the Planner's requirements input.

One call returns the whole handover package (requirements + INCOSE scores + provenance +
problem statement + coverage + readiness manifest):

    GET {ANALYST_URL}/projects/{pid}/package

Replaces the old reqoach `store/` file reader (that store no longer exists). Contract:
`assets/analyst_agent/sdk/how_to.md`.

TRACE KEY: `req_id` (e.g. REQ-0005) — used verbatim so tasks join to the Analyst, the
Architect handover and the ADD traceability table. Never re-key it.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from .loader import Requirement

ANALYST_URL = os.environ.get("ANALYST_URL", "http://localhost:7803")


class AnalystError(RuntimeError):
    pass


def get_package(pid: str, base_url: str | None = None, run: str | None = None,
                timeout: float = 180.0) -> dict:
    """Fetch the Analyst handover package for a project (~MBs; allow a generous timeout)."""
    url = f"{(base_url or ANALYST_URL).rstrip('/')}/projects/{pid}/package"
    if run:
        url += f"?run={run}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except (urllib.error.URLError, ValueError, TimeoutError) as e:
        raise AnalystError(f"analyst package fetch failed ({url}): {e}") from e


def readiness(pkg: dict) -> dict:
    """Readiness verdict. Branch on `architect_ready`, NOT on data being present.
    Every package is `draft` today (the Analyst's release gate isn't built yet)."""
    m = pkg.get("manifest", {}) or {}
    return {
        "architect_ready": bool(m.get("architect_ready", False)),
        "release_status": m.get("release_status"),
        "threshold": m.get("threshold"),
        "blockers": m.get("blockers", []),
        "counts": m.get("counts", {}),
        "project_id": m.get("project_id"),
        "project_name": m.get("project_name"),
        "run_id": m.get("run_id"),
    }


def requirements_from_package(pkg: dict) -> list[Requirement]:
    """Analyst requirement records -> planner Requirement objects, keyed on req_id.

    Duplicates are already excluded upstream. `classes`/`constraints` are carried through
    for routing/acceptance; they are EMPTY until the Analyst's classify:run has been run.
    """
    out: list[Requirement] = []
    for r in pkg.get("requirements", []):
        rid = r.get("req_id")
        if not rid:
            continue
        analysis = r.get("analysis", {}) or {}
        scores = {k: v for k, v in (analysis.get("characteristic_scores") or {}).items()
                  if isinstance(v, (int, float)) and v > 0}
        avg = analysis.get("score")
        if avg is None and scores:
            avg = round(sum(scores.values()) / len(scores), 3)
        prov = r.get("provenance", {}) or {}
        out.append(Requirement(
            req_id=rid,                       # ← trace key, verbatim
            text=(r.get("text") or "").strip(),
            section=(prov.get("section_path") or "")[:80],
            avg_score=float(avg or 0.0),
            scores=scores,
            quality_signals={"C4_complete": scores.get("C4"),
                             "C5_singular": scores.get("C5"),
                             "C7_verifiable": scores.get("C7")},
            classes=list(r.get("classes") or []),
            constraints=list(r.get("constraints") or []),
        ))
    return out


def coverage_gaps_from_package(pkg: dict) -> list[dict]:
    """Coverage gaps ride in the same package (may be null if coverage:run wasn't run)."""
    cov = pkg.get("coverage") or {}
    out = []
    for g in cov.get("gaps", []) or []:
        out.append({"title": g.get("title", ""), "severity": g.get("severity", ""),
                    "detail": g.get("detail", ""), "question": g.get("question", ""),
                    "grounding": g.get("grounding", []), "domain": g.get("domain", ""),
                    "traces_to": "coverage"})
    return out


def problem_statement_version(pkg: dict):
    ps = pkg.get("problem_statement") or {}
    return ps.get("version")
