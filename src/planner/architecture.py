"""Architect Agent handover reader (contract: `architect_agent/sdk/how_to.md`).

    data/architecture/<project_id>/planner_handover.json

Two intended uses on our side:
  1. NAMING      — derive artifact names from components the Architect already named
                   (`MatchingService` -> `matching_service.py`). If we would invent a
                   component the Architect already defined for that requirement, ITS NAME WINS.
  2. ACCEPTANCE  — cite a validated constraint (`latencyMs <= 200`) instead of "should be fast".

What we must NOT do:
  - build silently on `open_issues` (unquantified_constraint / semantic_defect) — surface them.
  - treat `depends_on` as build order — it is interface direction. Sequencing stays ours.
  - assume approval: mirror `architect_ready` / `release_status`, branch on the flag.

Everything degrades gracefully: no handover file -> None -> the planner works exactly as before.
"""

from __future__ import annotations

import json
import os
import re

ARCH_ROOT = os.environ.get(
    "ARCHITECT_ARCH_DIR",
    "/home/logus/env/assets/architect_agent/data/architecture")

# Language/extension choice stays the PLANNER's call (architect doc §5).
_EXT_BY_KIND = {"schema": ".json", "config": ".yaml", "docs": ".md",
                "test": ".py", "code": ".py"}


def handover_path(project_id: str, root: str | None = None) -> str:
    return os.path.join(root or ARCH_ROOT, project_id, "planner_handover.json")


def load_handover(project_id: str, root: str | None = None) -> dict | None:
    """Load the architecture handover, or None if the Architect hasn't produced one."""
    path = handover_path(project_id, root)
    if not os.path.isfile(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def readiness(h: dict | None) -> dict:
    sp = (h or {}).get("source_package", {}) or {}
    return {"architect_ready": bool(sp.get("architect_ready", False)),
            "release_status": sp.get("release_status"),
            "requirements_modelled": sp.get("requirements_modelled"),
            "requirements_received": sp.get("requirements_received"),
            "contract_version": (h or {}).get("contract_version")}


def for_requirement(h: dict | None, req_id: str) -> dict:
    """The Architect's elements for one requirement (empty dict if absent — not an error)."""
    if not h:
        return {}
    return (h.get("by_requirement") or {}).get(req_id) or {}


def snake(name: str) -> str:
    """MatchingService -> matching_service (used when suggested_module is absent)."""
    s = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s).lower()


def component_names(h: dict | None, req_id: str) -> list[dict]:
    """Components the Architect defined for this requirement, with a module hint."""
    out = []
    for c in for_requirement(h, req_id).get("components", []) or []:
        name = c.get("name")
        if not name:
            continue
        out.append({"name": name,
                    "module": c.get("suggested_module") or snake(name),
                    "responsibility": c.get("responsibility", "")})
    return out


def constraints_for(h: dict | None, req_id: str) -> list[dict]:
    """Validated SysML constraint expressions — safe to quote in acceptance criteria.
    `expression: null` means the model proposed nothing usable, so we skip it."""
    return [c for c in for_requirement(h, req_id).get("constraints", []) or []
            if c.get("expression")]


def open_issues_for(h: dict | None, req_ids) -> list[dict]:
    """Architect-flagged issues touching these requirements. NOT decoration — a
    semantic_defect means valid SysML that may say the wrong thing."""
    if not h:
        return []
    want = set(req_ids or [])
    return [i for i in (h.get("open_issues") or []) if i.get("req_id") in want]


def architect_deliverable(h: dict | None, req_ids, kind: str, task_text: str = "") -> str | None:
    """Preferred deliverable filename from the Architect's component naming.

    Precedence: the Architect's name wins over one we would invent — but only for the
    component this task is actually ABOUT. A requirement often defines several components
    (e.g. EmployerRegistration AND CompanyProfile); blindly taking the first collapses
    distinct tasks onto one filename and they overwrite each other. If the task doesn't
    clearly match exactly one component, we keep the planner's name rather than collide.
    Extension is ours to choose (architect doc §5: file layout stays the Planner's).
    """
    comps = [c for rid in (req_ids or []) for c in component_names(h, rid)]
    if not comps:
        return None
    ext = _EXT_BY_KIND.get((kind or "code").lower(), ".py")
    if len(comps) == 1:
        return comps[0]["module"] + ext
    blob = (task_text or "").lower()
    norm = re.sub(r"[^a-z0-9]+", "_", blob)
    matched = [c for c in comps
               if c["name"].lower() in blob or c["module"] in norm]
    if len(matched) == 1:
        return matched[0]["module"] + ext
    return None            # ambiguous -> keep the planner's own (more specific) name


def architecture_context(h: dict | None, req_id: str, max_chars: int = 700) -> str:
    """Compact context block to give the decomposer, so tasks are generated against the
    Architect's structure instead of inventing one."""
    d = for_requirement(h, req_id)
    if not d:
        return ""
    parts = []
    comps = component_names(h, req_id)
    if comps:
        parts.append("COMPONENTS (use these names): "
                     + "; ".join(f"{c['name']} — {c['responsibility']}".strip(" —") for c in comps))
    fns = [f.get("name") for f in d.get("functions", []) or [] if f.get("name")]
    if fns:
        parts.append("FUNCTIONS: " + ", ".join(fns))
    ifaces = [f"{i.get('name')} ({i.get('supplier')}→{i.get('consumer')})"
              for i in d.get("interfaces", []) or [] if i.get("name")]
    if ifaces:
        parts.append("INTERFACES: " + "; ".join(ifaces))
    cons = [f"{c.get('name')}: {c.get('expression')}" for c in constraints_for(h, req_id)]
    if cons:
        parts.append("CONSTRAINTS (must hold): " + "; ".join(cons))
    return ("\n".join(parts))[:max_chars]
