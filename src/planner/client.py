"""Minimal agent_server client for the validation harness.

Talks to agent_server's OpenAI-compatible endpoint the same way reqoach's proven
escape hatch does (score/setlevel.py): raw `model: "gemma-4"` with an inline
system+user message pair and `response_format: json_object`. Uses only the stdlib
so the harness runs with no dependencies.

Every call degrades, never raises (reqoach's contract): a transport/parse failure
returns None and the caller decides how to record it.
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
    """reqoach's 3-tier parse: raw -> fence-strip -> brace-slice."""
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
    # Slot context is 32K (input+output share it). Inputs here are small, so an
    # explicit generous output cap keeps artifacts from being silently truncated by
    # an unknown server default while staying well inside one slot's budget.
    def __init__(self, base_url: str | None = None, model: str = "gemma-4",
                 timeout: float = 300.0, max_tokens: int = 8192):
        self.base_url = (base_url or os.environ.get(
            "AGENT_SERVER_URL", "http://localhost:7701")).rstrip("/")
        self.model = model
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.calls = 0
        self.total_s = 0.0
        self.truncated = 0            # completions that hit the length cap
        self._lock = threading.Lock()  # counters are touched from worker threads

    def complete_json(self, system: str, user: str,
                      temperature: float | None = None,
                      max_tokens: int | None = None) -> dict | None:
        """Inline system+user -> parsed JSON dict, or None on any failure."""
        return self._call(system, user, temperature, max_tokens, json_mode=True)

    def complete_text(self, system: str, user: str,
                      temperature: float | None = None,
                      max_tokens: int | None = None) -> str | None:
        """Inline system+user -> raw text (for the execute stage's artifact)."""
        return self._call(system, user, temperature, max_tokens, json_mode=False)

    def _call(self, system: str, user: str, temperature, max_tokens, json_mode: bool):
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "chat_template_kwargs": {"enable_thinking": False},
            "max_tokens": max_tokens or self.max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        if temperature is not None:
            payload["temperature"] = temperature
        req = urllib.request.Request(
            f"{self.base_url}/v1/chat/completions",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
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
            print(f"    ! LLM call failed: {type(e).__name__}: {str(e)[:120]}")
            return None
        with self._lock:
            self.calls += 1
            self.total_s += time.time() - t0
            if hit_cap:
                self.truncated += 1
        if hit_cap:
            # Surface truncation loudly — a cut-off artifact would poison the
            # outcome judgment, exactly what we must not let happen silently.
            print(f"    ! TRUNCATED at max_tokens={payload['max_tokens']} "
                  f"(finish_reason=length) — result may be incomplete")
        return _parse_json(content) if json_mode else content
