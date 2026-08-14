"""agent_server client — the house pattern (same as analyst_agent / architect_agent).

One stateless call per item: `model` carries the agent PRESET NAME (a registered persona), we
send only the user content, and the preset supplies the system prompt + sampling. The Planner's
reasoning roles are `planner_*` personas registered in agent_server (decompose, feasibility,
refine, outcome, plan_judge, execute) — the prompts live on the server, not in this code.

Stdlib-only. Every call degrades, never raises: a transport/parse failure returns None (JSON) or
None (text) and the caller decides how to record it.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def _parse_json(content: str) -> dict | None:
    """3-tier parse: raw -> fence-strip -> brace-slice. Strips a leading <think>...</think> block
    first (thinking-enabled models emit reasoning, with braces, before the JSON)."""
    if "</think>" in content:
        content = content.rsplit("</think>", 1)[1]
    content = content.strip()
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass
    stripped = _FENCE_RE.sub("", content).strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        start, end = stripped.find("{"), stripped.rfind("}")
        if 0 <= start < end:
            try:
                return json.loads(stripped[start:end + 1])
            except json.JSONDecodeError:
                pass
    return None


class GemmaClient:
    """Calls agent_server presets by name (the documented A1 pattern). Not a raw-model client —
    every reasoning role is a registered `planner_*` persona; sampling + system prompt live in
    the preset, callers send only user content."""

    def __init__(self, base_url: str | None = None, timeout: float = 300.0):
        self.base_url = (base_url or os.environ.get(
            "AGENT_SERVER_URL", "http://localhost:7701")).rstrip("/")
        self.timeout = timeout
        self.calls = 0
        self.total_s = 0.0
        self.truncated = 0            # completions that hit the length cap
        self._lock = threading.Lock()  # counters are touched from worker threads

    def _post(self, agent: str, user: str, json_mode: bool):
        """POST one user message to a preset. Returns (content, hit_cap) or (None, False)."""
        payload = {"model": agent, "messages": [{"role": "user", "content": user}]}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        req = urllib.request.Request(
            f"{self.base_url}/v1/chat/completions",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode())
            choice = data["choices"][0]
            content = choice["message"]["content"]
            hit_cap = choice.get("finish_reason") == "length"
        except (urllib.error.URLError, KeyError, IndexError, ValueError,
                TimeoutError) as e:
            with self._lock:
                self.calls += 1
                self.total_s += time.time() - t0
            print(f"    ! preset {agent} failed: {type(e).__name__}: {str(e)[:100]}")
            return None, False
        with self._lock:
            self.calls += 1
            self.total_s += time.time() - t0
            if hit_cap:
                self.truncated += 1
        if hit_cap:
            print(f"    ! {agent} TRUNCATED (finish_reason=length) — result may be incomplete")
        return content, hit_cap

    def preset_json(self, agent: str, user: str) -> dict | None:
        """Call a preset expecting a JSON object back. Parsed dict, or None on failure."""
        content, _ = self._post(agent, user, json_mode=True)
        return _parse_json(content) if content is not None else None

    def preset_text(self, agent: str, user: str) -> str | None:
        """Call a preset expecting raw text back (e.g. the execute stage's artifact)."""
        content, _ = self._post(agent, user, json_mode=False)
        return content
