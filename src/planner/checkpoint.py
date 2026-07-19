"""Per-requirement checkpoint (JSON-lines write-ahead log) for the planner.

A 386-requirement run is hours of serial LLM calls; an interruption must not discard
everything. The loop now processes ONE requirement at a time, and its result (feasible
tasks + questions + flagged) is appended here the moment it completes. On restart, the
already-done `req_id`s are skipped and only the delta is planned.

Append is lock-guarded so concurrent worker threads can commit safely. Records are plain
dicts (PlanTask serialized via dataclasses.asdict) so the WAL survives process restarts.
"""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import asdict

from .loop import PlanTask


def _res_to_dict(req_id: str, res: dict) -> dict:
    return {
        "req_id": req_id,
        "feasible": [asdict(t) for t in res.get("feasible", [])],
        "questions": [{"task": asdict(q["task"]), "question": q.get("question", ""),
                       "gap": q.get("gap", "")} for q in res.get("questions", [])],
        "flagged": [{"task": asdict(f["task"]), "reason": f.get("reason", "")}
                    for f in res.get("flagged", [])],
    }


def _res_from_dict(d: dict) -> dict:
    return {
        "feasible": [PlanTask(**t) for t in d.get("feasible", [])],
        "questions": [{"task": PlanTask(**q["task"]), "question": q.get("question", ""),
                       "gap": q.get("gap", "")} for q in d.get("questions", [])],
        "flagged": [{"task": PlanTask(**f["task"]), "feasibility": None,
                     "reason": f.get("reason", "")} for f in d.get("flagged", [])],
    }


def _max_task_num(records: list[dict]) -> int:
    """Highest numeric part of any Tnnn id in the WAL — so a resumed run's new ids don't
    collide with ids already committed."""
    hi = 0
    for _, res in records:
        for bucket in ("feasible", "questions", "flagged"):
            for item in res.get(bucket, []):
                t = item if bucket == "feasible" else item["task"]
                m = re.match(r"T0*(\d+)", t.task_id)
                if m:
                    hi = max(hi, int(m.group(1)))
    return hi


class Checkpoint:
    """A JSON-lines WAL at `path`. One line per completed requirement."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()

    def completed_req_ids(self) -> set:
        ids = set()
        if os.path.isfile(self.path):
            with open(self.path) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            ids.add(json.loads(line)["req_id"])
                        except (ValueError, KeyError):
                            continue
        return ids

    def append(self, req_id: str, res: dict) -> None:
        rec = json.dumps(_res_to_dict(req_id, res))
        with self._lock:                       # concurrent worker threads commit safely
            with open(self.path, "a") as f:
                f.write(rec + "\n")
                f.flush()

    def load_all(self) -> list[tuple]:
        """Return [(req_id, result-with-PlanTask-objects), …] for every committed requirement."""
        out = []
        if os.path.isfile(self.path):
            with open(self.path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                        out.append((d["req_id"], _res_from_dict(d)))
                    except (ValueError, KeyError):
                        continue
        return out

    def max_task_num(self) -> int:
        return _max_task_num(self.load_all())
