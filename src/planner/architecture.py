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


def _candidate_paths(project_id: str, root: str | None = None) -> list[str]:
    base = root or ARCH_ROOT
    # Two layouts are supported: the project GIT REPO (`<repo>/<pid>/architecture/…`, where the
    # Architect now publishes) and the legacy flat data dir (`<data>/<pid>/…`).
    return [os.path.join(base, project_id, "architecture", "planner_handover.json"),
            os.path.join(base, project_id, "planner_handover.json")]


def handover_path(project_id: str, root: str | None = None) -> str:
    for p in _candidate_paths(project_id, root):
        if os.path.isfile(p):
            return p
    return _candidate_paths(project_id, root)[-1]


def load_handover(project_id: str, root: str | None = None) -> dict | None:
    """Load the architecture handover, or None if the Architect hasn't produced one."""
    for path in _candidate_paths(project_id, root):
        if not os.path.isfile(path):
            continue
        try:
            with open(path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return None
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


def _component_index(h: dict | None) -> dict:
    """Map component name -> {attributes, module, responsibility}, resolved across handover
    layouts. In a 2.0 handover the per-requirement `components` are bare NAME STRINGS and the
    typed attributes live only in the top-level `components` list (responsibility under
    `by_aspect`); this index resolves a name to the full record either way."""
    idx: dict[str, dict] = {}
    for c in (h or {}).get("components", []) or []:
        n = c.get("name") if isinstance(c, dict) else None
        if not n:
            continue
        idx[n] = {"attributes": c.get("attributes") or [],
                  "module": c.get("suggested_module") or snake(n),
                  "responsibility": c.get("responsibility", "")}
    # by_aspect components carry `responsibility` (and attributes) — fill in what's missing.
    for asp in ((h or {}).get("by_aspect") or {}).values():
        for c in (asp or {}).get("components", []) or []:
            n = c.get("name") if isinstance(c, dict) else None
            if not n:
                continue
            rec = idx.setdefault(n, {"attributes": [], "module": snake(n), "responsibility": ""})
            if not rec.get("responsibility"):
                rec["responsibility"] = c.get("responsibility", "")
            if not rec.get("attributes"):
                rec["attributes"] = c.get("attributes") or []
    return idx


def component_names(h: dict | None, req_id: str) -> list[dict]:
    """Components the Architect defined for this requirement, with a module hint and the
    Architect's typed attributes (its data model). Carrying the attributes through is what lets
    the decomposer build against the real schema instead of asking the author to re-supply
    fields the Architect already specified. Handles BOTH handover versions: 1.0 (per-requirement
    components are objects carrying their own attributes) and 2.0 (name strings; attributes
    resolved from the top-level `components` list)."""
    if not h:
        return []
    idx = _component_index(h)
    out = []
    for c in for_requirement(h, req_id).get("components", []) or []:
        name = c if isinstance(c, str) else c.get("name")
        if not name:
            continue
        info = idx.get(name)
        if info is None and isinstance(c, dict):        # 1.0 inline object absent from the index
            info = {"attributes": c.get("attributes") or [],
                    "module": c.get("suggested_module") or snake(name),
                    "responsibility": c.get("responsibility", "")}
        info = info or {"attributes": [], "module": snake(name), "responsibility": ""}
        attrs = [{"name": a.get("name"), "type": a.get("type")}
                 for a in info["attributes"] if isinstance(a, dict) and a.get("name")]
        out.append({"name": name, "module": info["module"],
                    "responsibility": info["responsibility"], "attributes": attrs})
    return out


def constraints_for(h: dict | None, req_id: str) -> list[dict]:
    """Validated SysML constraint expressions — safe to quote in acceptance criteria.
    `expression: null` means the model proposed nothing usable, so we skip it."""
    return [c for c in for_requirement(h, req_id).get("constraints", []) or []
            if isinstance(c, dict) and c.get("expression")]


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


def architecture_context(h: dict | None, req_id: str, max_chars: int = 1500) -> str:
    """Compact context block to give the decomposer, so tasks are generated against the
    Architect's structure instead of inventing one. Includes each component's typed attributes
    (the Architect's data model) so the decomposer builds the schema the Architect already
    specified rather than escalating a question for fields that are right here."""
    d = for_requirement(h, req_id)
    if not d:
        return ""
    parts = []
    comps = component_names(h, req_id)
    if comps:
        lines = []
        for c in comps:
            line = f"{c['name']} — {c['responsibility']}".strip(" —")
            if c.get("attributes"):
                line += " [fields: " + ", ".join(
                    f"{a['name']}: {a['type']}" for a in c["attributes"]) + "]"
            lines.append(line)
        parts.append("COMPONENTS (use these names AND their listed fields — do NOT ask for "
                     "fields already given here): " + "; ".join(lines))
    # functions/interfaces are objects in 1.0 and bare name strings in 2.0 — accept both.
    fns = [(f if isinstance(f, str) else f.get("name")) for f in d.get("functions") or []]
    fns = [f for f in fns if f]
    if fns:
        parts.append("FUNCTIONS: " + ", ".join(fns))
    ifaces = []
    for i in d.get("interfaces") or []:
        if isinstance(i, str):
            ifaces.append(i)
        elif isinstance(i, dict) and i.get("name"):
            ifaces.append(f"{i.get('name')} ({i.get('supplier')}→{i.get('consumer')})")
    if ifaces:
        parts.append("INTERFACES: " + "; ".join(ifaces))
    cons = [f"{c.get('name')}: {c.get('expression')}" for c in constraints_for(h, req_id)]
    if cons:
        parts.append("CONSTRAINTS (must hold): " + "; ".join(cons))
    return ("\n".join(parts))[:max_chars]
