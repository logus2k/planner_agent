"""Structural proxy features — cheap, no-LLM feasibility predictors.

These are computed from a task dict (and its source requirement) without any model
call. The calibration run measures which of these actually correlate with the
ground-truth outcome, alongside the LLM feasibility verdict.
"""

from __future__ import annotations

import re

# Coarse token estimate (~4 chars/token) — good enough for a bounded-context proxy.
def est_tokens(text: str) -> int:
    return max(1, round(len(text) / 4))


# Conjunctions / list markers that signal hidden fan-out or multiple deliverables.
_FANOUT_RE = re.compile(r"\b(and|or|as well as|including|each|every|all|both)\b",
                        re.IGNORECASE)
_SENT_RE = re.compile(r"[.;\n•\-]\s+")


def _count_entities(text: str) -> int:
    """Rough count of distinct capitalized / quoted nouns as a stand-in for the
    number of concepts the task touches."""
    caps = set(re.findall(r"\b[A-Z][a-zA-Z]{2,}\b", text))
    quoted = set(re.findall(r"[\"'`]([^\"'`]{2,40})[\"'`]", text))
    return len(caps | quoted)


def compute(task: dict, source_req, depth: int) -> dict:
    """Return the structural proxy feature vector for one task.

    `task` has at least {title, instructions, deliverable}. `source_req` is the
    loader.Requirement it traces to. `depth` is the decomposition depth (0 = a
    task produced directly from the requirement).
    """
    title = task.get("title", "")
    instr = task.get("instructions", "")
    deliverable = task.get("deliverable", "")
    blob = f"{title}\n{instr}\n{deliverable}"

    n_verbs_est = len(_FANOUT_RE.findall(blob))
    n_clauses = len([s for s in _SENT_RE.split(instr) if s.strip()])
    return {
        "depth": depth,
        "context_tokens": est_tokens(blob),
        "n_fanout_markers": n_verbs_est,       # and/or/each/all ...
        "n_clauses": n_clauses,                # instruction sentence/clause count
        "n_entities": _count_entities(blob),
        "n_deliverables_declared": 1 if deliverable else 0,
        # carry the source requirement's reqoach signals as candidate predictors
        "src_avg_score": source_req.avg_score,
        "src_C4_complete": source_req.quality_signals.get("C4_complete"),
        "src_C5_singular": source_req.quality_signals.get("C5_singular"),
        "src_C7_verifiable": source_req.quality_signals.get("C7_verifiable"),
    }
