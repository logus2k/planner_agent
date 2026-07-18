#!/usr/bin/env python3
"""Register the Planner Agent presets in agent_server (idempotent).

Each preset's system prompt is a static file under prompts/. Create-or-update via
the admin API (POST, 409 -> PUT), following reqoach's register_incose_judges.py.
The deployable feasibility gate IS the `planner_feasibility` preset — tuning it
means editing prompts/planner_feasibility.txt and re-running this script.

    python scripts/register_planner_agents.py [name ...]

With no args, registers all. With names, only those (e.g. `planner_feasibility`).
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

AGENT_SERVER = os.environ.get("AGENT_SERVER_URL", "http://localhost:7701")
PROMPTS = os.path.join(os.path.dirname(__file__), "..", "prompts")

# preset name -> (prompt file, params_override)
JUDGE_PARAMS = {"max_tokens": 512, "temperature": 0.0, "top_p": 0.9,
                "chat_template_kwargs": {"enable_thinking": False}}
# Thinking variant: uses a reasoning-SCRIPTED prompt (planner_feasibility_reason.txt)
# that tells the model exactly what to reason about, in order. Reasoning tokens precede
# the JSON, so the cap is high to avoid truncating the answer.
JUDGE_THINK_PARAMS = {"max_tokens": 4096, "temperature": 0.0, "top_p": 0.9,
                      "chat_template_kwargs": {"enable_thinking": True}}
# temperature 0 for reproducible plans — the planner must be deterministic run-to-run
# (up to GPU non-determinism), not creative.
DECOMPOSE_PARAMS = {"max_tokens": 2048, "temperature": 0.0, "top_p": 0.9,
                    "chat_template_kwargs": {"enable_thinking": False}}

# Hybrid: reasoning-scripted prompt but thinking OFF — the per-criterion reasoning is
# emitted in the JSON "reasoning" field. Bigger cap than the plain judge so it fits.
JUDGE_REASON_NOTHINK_PARAMS = {"max_tokens": 1024, "temperature": 0.0, "top_p": 0.9,
                               "chat_template_kwargs": {"enable_thinking": False}}

PRESETS = {
    "planner_feasibility": ("planner_feasibility.txt", JUDGE_PARAMS),
    "planner_feasibility_think": ("planner_feasibility_reason.txt", JUDGE_THINK_PARAMS),
    "planner_feasibility_reason": ("planner_feasibility_reason.txt", JUDGE_REASON_NOTHINK_PARAMS),
    "planner_outcome": ("planner_outcome.txt", JUDGE_PARAMS),
    "planner_decompose": ("planner_decompose.txt", DECOMPOSE_PARAMS),
    "planner_refine": ("planner_refine.txt", DECOMPOSE_PARAMS),
}


def _req(method: str, url: str, payload: dict):
    data = json.dumps(payload).encode()
    r = urllib.request.Request(url, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            return resp.status, resp.read().decode()[:120]
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:120]


def register(name: str, prompt_file: str, params: dict) -> None:
    with open(os.path.join(PROMPTS, prompt_file), encoding="utf-8") as f:
        system_prompt = f.read().strip()
    preset = {"name": name, "system_prompt": system_prompt,
              "params_override": dict(params), "memory_policy": "none"}
    status, body = _req("POST", f"{AGENT_SERVER}/admin/api/agents", preset)
    if status == 409:
        status, body = _req("PUT", f"{AGENT_SERVER}/admin/api/agents/{name}", preset)
    print(f"[{name}] HTTP {status} {body}")
    if status >= 400:
        raise SystemExit(f"failed to register {name}: {status} {body}")


def main() -> None:
    names = sys.argv[1:] or list(PRESETS)
    for name in names:
        if name not in PRESETS:
            raise SystemExit(f"unknown preset {name}; known: {list(PRESETS)}")
        register(name, *PRESETS[name])


if __name__ == "__main__":
    main()
