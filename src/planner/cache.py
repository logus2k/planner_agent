"""Per-project memoization cache for the planner (roadmap B4).

A cold run plans every requirement; a *re-run* after a small change should reprocess only
the requirements that actually changed. This cache stores each requirement's RAW gate/refine
loop result (feasible tasks + questions + flagged), keyed by a hash of its inputs, in a
per-project JSON-lines file (`data/cache/<project_id>.jsonl`).

Relationship to checkpoint.py:
  - checkpoint.py keys on `req_id` and is a WITHIN-run write-ahead log (resume an interrupted
    run). It is discarded once the run finishes.
  - this cache keys on the INPUT HASH and persists ACROSS runs. It reuses checkpoint's
    serialization (`_res_to_dict`/`_res_from_dict`) so both speak the same on-disk shape.

Invalidation is automatic: the key folds in `CACHE_VERSION`, which is derived from the
contents of the decompose/gate/refine prompt files — retune a preset's prompt and every
entry it could have influenced stops matching, so stale results are never reused.

Only the per-requirement result is cached. Naming, dedup, and assembly are cheap deterministic
code and are ALWAYS re-run fresh over the aggregated (cached + newly-planned) set, so
cross-requirement dedup stays correct when old and new results mix.

Task-id collisions are avoided by the caller: it calls `loop.advance_ids(cache.max_task_num())`
before planning, so newly-generated ids sit strictly above every id already in the cache, and
reused entries keep their (lower) ids. Because every run advances past the current max before
generating, the cache's ids stay globally distinct and monotonic across runs.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading

from .checkpoint import _max_task_num, _res_from_dict, _res_to_dict

# Prompt files whose content defines the per-requirement result. A change to any of them must
# invalidate cached results (a retuned gate/refiner/decomposer can change every verdict).
_PROMPT_FILES = (
    "planner_decompose.txt",
    "planner_feasibility_reason.txt",
    "planner_refine.txt",
)
_CACHE_SCHEMA = "v1"  # bump if the cached record shape changes (independent of prompt content)


def _prompts_dir() -> str:
    return os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "prompts"))


def cache_version() -> str:
    """A stable tag folding the schema + the decompose/gate/refine prompt contents, so
    retuning any of those prompts auto-invalidates every entry it could have shaped. A
    missing prompt file still yields a stable, distinct tag (rather than raising)."""
    h = hashlib.sha1(_CACHE_SCHEMA.encode())
    d = _prompts_dir()
    for name in _PROMPT_FILES:
        h.update(b"\0" + name.encode() + b"\0")
        try:
            with open(os.path.join(d, name), "rb") as f:
                h.update(f.read())
        except OSError:
            h.update(b"<missing>")
    return h.hexdigest()[:16]


def _key(req_text: str, arch_context: str, version: str) -> str:
    """sha1(version + requirement text + architecture context) — the memoization key."""
    return hashlib.sha1(
        f"{version}\0{req_text}\0{arch_context}".encode()
    ).hexdigest()


class Cache:
    """A per-project memoization cache backed by a JSON-lines file. Append-only; on load,
    the last record for a key wins. Records whose `version` differs from the current
    `cache_version()` are ignored (stale after a prompt retune)."""

    def __init__(self, path: str, version: str | None = None):
        self.path = path
        self.version = version or cache_version()
        self._lock = threading.Lock()
        self._entries: dict[str, dict] = self._load()
        self.hits = 0
        self.misses = 0

    def _load(self) -> dict[str, dict]:
        entries: dict[str, dict] = {}
        if os.path.isfile(self.path):
            with open(self.path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    if d.get("version") == self.version and "key" in d:
                        entries[d["key"]] = d  # last write wins
        return entries

    def get(self, req_text: str, arch_context: str) -> dict | None:
        """Return the cached loop result (PlanTask objects reconstructed) or None on miss."""
        d = self._entries.get(_key(req_text, arch_context, self.version))
        with self._lock:
            if d is None:
                self.misses += 1
            else:
                self.hits += 1
        return _res_from_dict(d) if d is not None else None

    def put(self, req_id: str, req_text: str, arch_context: str, res: dict) -> None:
        """Store a requirement's loop result under its input hash (thread-safe append)."""
        key = _key(req_text, arch_context, self.version)
        rec = _res_to_dict(req_id, res)
        rec["key"] = key
        rec["version"] = self.version
        line = json.dumps(rec)
        with self._lock:
            self._entries[key] = rec
            with open(self.path, "a") as f:
                f.write(line + "\n")
                f.flush()

    def max_task_num(self) -> int:
        """Highest Tnnn id across all live entries — the caller advances past this so newly
        generated ids never collide with cached ones."""
        records = [(d.get("req_id", ""), _res_from_dict(d)) for d in self._entries.values()]
        return _max_task_num(records)
