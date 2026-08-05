"""Platform capabilities the runtime provides — fetched from agent_server's manifest.

The decomposer and the feasibility judge consult this so a task that USES a provided capability
(describe an image via the vision model, authenticate via OAuth2, rerank via the embeddings
server, …) is treated as FEASIBLE: the Builder CALLS the capability, it does not implement the
model/algorithm from scratch. This is PLATFORM state, project-agnostic — it never mentions any
particular project. Degrades gracefully: any failure yields no capabilities, and planning
proceeds exactly as before.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

AGENT_SERVER_URL = os.environ.get("AGENT_SERVER_URL", "http://localhost:7701")


def fetch(base_url: str | None = None, timeout: float = 8.0) -> list[dict]:
    """The platform capabilities manifest, or [] on any failure."""
    url = f"{(base_url or AGENT_SERVER_URL).rstrip('/')}/admin/api/capabilities"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            data = json.loads(r.read().decode())
        return [c for c in (data or {}).get("capabilities", []) if isinstance(c, dict)]
    except (urllib.error.URLError, ValueError, TimeoutError, OSError):
        return []


def compact(caps: list[dict]) -> str:
    """One line per capability — small enough to inject into a judge prompt. Empty if none."""
    lines = []
    for c in caps:
        provides = (c.get("provides") or "").strip()
        via = (c.get("via") or "").strip()
        lines.append(f"- {c.get('id')}: {provides}" + (f" [{via}]" if via else ""))
    return "\n".join(lines)


def block(caps_text: str) -> str:
    """Wrap the compact capability list in the header the prompts key off. Empty if no caps."""
    if not caps_text:
        return ""
    return ("\n\nAVAILABLE PLATFORM CAPABILITIES (the runtime PROVIDES these — a task that USES "
            "one is feasible: the implementer CALLS the capability, it does NOT build the "
            "model/algorithm/provider from scratch):\n" + caps_text)
