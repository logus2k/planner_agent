"""Planner HTTP service — mirrors the Analyst/Architect job pattern.

The Planner was a batch CLI (`scripts/produce_plan.py`). This thin server exposes the same
run as an async job so FACTORY (reqoach) can trigger it and poll progress, exactly like the
Architect's `architect:run`:

  POST /projects/{pid}/planner:run   -> start a run, returns {job_id}
  GET  /jobs/{job_id}                -> status snapshot (status, stage, progress, error)
  GET  /health

A run: fetch the Analyst package -> decompose + gate/refine per requirement (local Gemma) ->
assemble plan.json -> write to data/plans AND publish into the project repo's `plans/` area so
FACTORY commits it (mirrors how the Architect publishes `architecture/`). Each run executes in a
worker thread (many LLM calls); the JobManager keeps a snapshot the UI polls. No socket.io.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
import uuid
from dataclasses import dataclass, field

from fastapi import FastAPI, HTTPException, Request

from . import analyst, architecture, pipeline
from .cache import Cache
from .client import GemmaClient

__version__ = "0.1.0"

ANALYST_URL = os.environ.get("ANALYST_URL", analyst.ANALYST_URL)
# Root under which the Architect handover is found; both the repo layout
# (<root>/<pid>/architecture/…) and the flat data layout (<root>/<pid>/…) are tried.
ARCH_DIR = os.environ.get("ARCHITECT_ARCH_DIR")
REPOS_ROOT = os.environ.get("PROJECT_REPOS_ROOT")
REQOACH_URL = os.environ.get("REQOACH_URL", "http://localhost:7802").rstrip("/")
DATA_DIR = os.environ.get(
    "PLANNER_DATA_DIR", os.path.join(os.path.dirname(__file__), "..", "..", "data"))
WORKERS = int(os.environ.get("PLANNER_WORKERS", "2"))


def publish_plan_to_repo(pid: str, plan: dict, repos_root: str | None = None) -> dict | None:
    """Write plan.json into the project repo's plans/ area and ask FACTORY (reqoach) to commit
    it — the Planner never touches git (reqoach owns it, exactly like the Architect). No-op if
    PROJECT_REPOS_ROOT is unset. Returns {path, committed, sha} or None."""
    repos_root = repos_root or REPOS_ROOT
    if not repos_root:
        return None
    plans_dir = os.path.join(repos_root, pid, "plans")
    os.makedirs(plans_dir, exist_ok=True)
    path = os.path.join(plans_dir, "plan.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=1)
    committed = None
    try:
        body = json.dumps({"area": "plans", "agent": "planner",
                           "message": "Planner: publish plan"}).encode()
        req = urllib.request.Request(
            f"{REQOACH_URL}/repos/{pid}/commit", data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            committed = json.loads(resp.read().decode())
    except Exception as e:  # noqa: BLE001 — commit is best-effort; the file is already written
        committed = {"committed": False, "error": f"{type(e).__name__}: {e}"}
    return {"path": path, **(committed or {})}


def plan_to_gaps(plan: dict) -> list[dict]:
    """The Planner's genuine questions as requirement gaps for the Analyst resolver.
    Each carries the req_id it traces to + the gap + the question. requirement_text is
    left for the Analyst to fill from its own review (authoritative current text)."""
    gaps = []
    for q in plan.get("questions", []):
        traces = q.get("traces_to") or []
        if traces:
            gaps.append({"req_id": traces[0], "gap": q.get("gap", ""),
                         "question": q.get("question", "")})
    return gaps


def _analyst_settings(pid: str) -> dict:
    """Fetch the project's gap-loop settings from the Analyst (GET is not owner-gated).
    Returns {} on any failure so the loop simply defaults to off."""
    try:
        with urllib.request.urlopen(
                f"{ANALYST_URL}/projects/{pid}/settings", timeout=30) as resp:
            return json.loads(resp.read().decode())
    except Exception:  # noqa: BLE001 — settings are advisory; absence => loop off
        return {}


@dataclass
class Job:
    job_id: str
    project_id: str
    actor: str | None = None               # authenticated email; needed to call the gated resolve
    status: str = "queued"                 # queued | running | done | error
    stage: str | None = None
    progress: dict = field(default_factory=dict)
    error: str | None = None
    result: dict | None = None
    started_at: float | None = None

    def snapshot(self) -> dict:
        return {"job_id": self.job_id, "project_id": self.project_id, "kind": "planner",
                "status": self.status, "stage": self.stage, "progress": self.progress,
                "error": self.error, "result": self.result,
                "elapsed_s": round(time.time() - self.started_at) if self.started_at else None}


class JobManager:
    """Owns planner jobs; runs each in a worker thread. Polling-only (no socket.io)."""

    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}

    def _plan_once(self, job: Job, client: GemmaClient, cache: Cache, phase: str) -> dict:
        """One full plan pass over the CURRENT Analyst package. Re-fetches every time, so a
        re-plan after refinement sees the updated requirement text. The per-project `cache`
        makes this affected-only for free: a refined requirement's text changed -> cache miss
        -> recompute; every unchanged requirement hits -> reuse. `phase` labels progress
        ("plan" cold, "replan" after gap resolution)."""
        job.stage = phase
        job.progress = {"stage": phase, "status": "progress"}
        pkg = analyst.get_package(job.project_id, base_url=ANALYST_URL)
        ready = analyst.readiness(pkg)
        reqs = analyst.requirements_from_package(pkg)
        gaps = analyst.coverage_gaps_from_package(pkg)
        ps_version = analyst.problem_statement_version(pkg)
        handover = architecture.load_handover(job.project_id, root=ARCH_DIR)

        # Progress: the pipeline logs "[i/n] REQ-xxxx: …" per requirement; surface the count.
        total = len(reqs)
        done = {"n": 0}

        def _log(msg: str = "") -> None:
            s = str(msg)
            if "] REQ-" in s or s.strip().startswith("["):
                done["n"] += 1
                job.progress = {"stage": phase, "status": "progress",
                                "done": done["n"], "total": total, "last": s.strip()[:120]}

        job.progress = {"stage": phase, "status": "progress", "done": 0, "total": total}
        plan_result = pipeline.plan_project(
            client, reqs, handover=handover, workers=WORKERS, cache=cache, log=_log)
        return pipeline.assemble_plan(
            ready, plan_result, gaps, ps_version, len(reqs),
            handover=handover, planned_req_ids=[r.req_id for r in reqs])

    def _publish(self, job: Job, plan: dict) -> dict:
        """Write plan.json to data/plans and publish+commit into the project repo."""
        out = os.path.join(DATA_DIR, "plans", f"{job.project_id}.plan.json")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            json.dump(plan, f, ensure_ascii=False, indent=1)
        return {"out": out, "repo": publish_plan_to_repo(job.project_id, plan)}

    @staticmethod
    def _summ(plan: dict) -> dict:
        s = plan.get("summary", {})
        return {"tasks": len(plan.get("tasks", [])), "feasible": s.get("feasible"),
                "questions": s.get("questions"), "flagged": s.get("flagged"),
                "coverage_gaps": s.get("coverage_gaps")}

    def _resolve_gaps(self, job: Job, gaps: list[dict], apply: bool) -> dict | None:
        """Hand the plan's genuine questions to the Analyst gap resolver and wait for it.
        The Analyst resolve endpoint is owner-gated, so `job.actor` is forwarded as the
        `X-Auth-Request-Email` identity. Mirrors the Analyst job's progress into this job.
        Returns the resolution result ({affected_req_ids, counts, …}) or None on failure."""
        body = json.dumps({"gaps": gaps, "apply": apply}).encode()
        req = urllib.request.Request(
            f"{ANALYST_URL}/projects/{job.project_id}/planner-gaps:resolve",
            data=body, headers={"Content-Type": "application/json"}, method="POST")
        if job.actor:
            req.add_header("X-Auth-Request-Email", job.actor)
        with urllib.request.urlopen(req, timeout=60) as resp:
            gid = json.loads(resp.read().decode()).get("job_id")
        if not gid:
            return None
        while True:
            time.sleep(2)
            with urllib.request.urlopen(
                    f"{ANALYST_URL}/gap-jobs/{gid}", timeout=30) as resp:
                snap = json.loads(resp.read().decode())
            p = snap.get("progress") or {}
            job.progress = {"stage": "resolve", "status": "progress",
                            "done": p.get("done"), "total": p.get("total"), "last": p.get("last")}
            if snap.get("status") in ("done", "error"):
                job.progress = {"stage": "resolve", "status": snap.get("status"), **p}
                return snap.get("result") if snap.get("status") == "done" else None

    def _run_planner(self, job: Job) -> None:
        job.status = "running"
        job.started_at = time.time()
        try:
            client = GemmaClient()
            cache = Cache(os.path.join(DATA_DIR, "cache", f"{job.project_id}.jsonl"))
            os.makedirs(os.path.dirname(cache.path), exist_ok=True)

            plan = self._plan_once(job, client, cache, phase="plan")
            result = {**self._summ(plan), "llm_calls": client.calls, **self._publish(job, plan)}

            # --- Planner -> Analyst gap feedback loop (auto by default, per-project setting) ---
            settings = (_analyst_settings(job.project_id).get("gap_loop") or {})
            trigger = settings.get("trigger", "manual")
            apply_auto = settings.get("apply", "manual") == "auto"
            questions = plan.get("questions", [])
            if trigger == "auto" and questions:
                if not job.actor:
                    # The resolve endpoint is owner-gated; without an identity we cannot apply.
                    result["gap_resolution"] = {"skipped": "no authenticated actor"}
                else:
                    gaps = plan_to_gaps(plan)
                    if gaps:
                        job.stage = "resolve"
                        gres = self._resolve_gaps(job, gaps, apply=apply_auto)
                        if gres:
                            affected = gres.get("affected_req_ids") or []
                            result["gap_resolution"] = {
                                "total": gres.get("total"), "counts": gres.get("counts"),
                                "affected_req_ids": affected, "applied": apply_auto}
                            if affected:
                                # Only the refined requirements changed text -> cache-miss and
                                # recompute; everything else hits the cache. Affected-only re-plan.
                                plan = self._plan_once(job, client, cache, phase="replan")
                                result = {**result, **self._summ(plan),
                                          "llm_calls": client.calls,
                                          **self._publish(job, plan),
                                          "reqs_refined": len(affected)}

            job.status = "done"
            job.stage = "done"
            job.result = result
            job.progress = {"stage": "done", "status": "done",
                            **{k: result.get(k) for k in
                               ("feasible", "questions", "flagged", "coverage_gaps",
                                "reqs_refined")}}
        except Exception as e:  # noqa: BLE001 — surface any pipeline failure to the client
            job.status = "error"
            job.error = f"{type(e).__name__}: {e}"

    def create_planner_run(self, pid: str, actor: str | None = None) -> Job:
        job = Job(job_id=uuid.uuid4().hex, project_id=pid, actor=actor)
        self.jobs[job.job_id] = job
        threading.Thread(target=self._run_planner, args=(job,), daemon=True).start()
        return job


api = FastAPI(title="planner-agent", version=__version__)
jm = JobManager()


@api.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "planner-agent", "version": __version__,
            "jobs": len(jm.jobs)}


@api.post("/projects/{pid}/planner:run")
def planner_run(pid: str, request: Request) -> dict:
    """Start a planning run for a project. Returns the job id to poll. The authenticated
    email (forwarded by the edge on the gated POST) is captured as the run's actor so the
    gap-resolution loop can call the owner-gated Analyst resolve endpoint on its behalf."""
    actor = request.headers.get("x-auth-request-email")
    job = jm.create_planner_run(pid, actor=actor)
    return {"job_id": job.job_id, "project_id": pid, "status": job.status}


@api.get("/jobs/{job_id}")
def job_status(job_id: str) -> dict:
    job = jm.jobs.get(job_id)
    if not job:
        raise HTTPException(404, "unknown job")
    return job.snapshot()
