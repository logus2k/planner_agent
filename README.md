# Planner Agent

**Planner Agent turns software requirements into a plan of small, buildable tasks — and tells
you honestly what it still needs you to decide.**

Requirements describe *what* a system should do, but they aren't directly actionable. Handing a
whole requirement to an AI coder tends to produce vague, half-working output. Breaking
requirements into good tasks by hand is slow. Planner Agent does that breakdown automatically,
sizing each task so a **local model can actually build it** — and refusing to paper over the
parts that genuinely require a human decision.

It runs entirely on a **local model (Gemma 4)** — nothing leaves your machine, and it works
offline.

```
reqoach  ──►  Planner Agent  ──►  builder_agent
requirements   a plan of buildable   builds each task
+ gaps         tasks + open questions  (also local)
```

---

## What it does

Given a set of requirements, Planner Agent:

1. **Breaks each requirement into small tasks** — one deliverable each, no giant "implement the
   whole feature" blobs.
2. **Decides which tasks are actually ready to build** — a task is "ready" only if it can be
   implemented without inventing something that isn't specified.
3. **Asks instead of guessing.** When a task depends on a real unknown — a scoring formula, a
   business rule, which vendor to use — it turns that into a **question for you**, rather than
   fabricating a plausible-but-wrong answer.
4. **Produces a `plan.json`** — the ready-to-build tasks (with instructions, dependencies, and a
   link back to the requirement they came from), plus the open questions and anything it couldn't
   reduce to a buildable task.

The output is designed to be handed straight to [builder_agent](../builder_agent), which builds
the tasks — or to a human team.

---

## Why use it

- **You get a plan you can trust.** Everything marked "ready" is genuinely buildable. The things
  that *aren't* nailed down show up as explicit questions instead of silent guesses — so you
  never discover a fabricated assumption after the code is written.
- **It's built for small/local models.** Tasks are sized so a modest local model can implement
  them one at a time. No frontier API required to actually build.
- **It runs locally and offline.** No data leaves the machine — useful for private or regulated
  requirements.
- **Everything is traceable.** Every task points back to the requirement it satisfies, so you can
  always answer "why is this task here?".
- **It closes the loop from requirements to code.** Paired with reqoach (which produces good
  requirements) and builder_agent (which builds the plan), you get requirements → plan → working
  artifacts, all on local infrastructure.

---

## How it works

```
requirements ─► break into tasks ─► "is this task buildable as-is?"
                                        ├─ yes ───────────────► ready to build
                                        └─ no  ─► try to fix it (split it, add a
                                                  prerequisite, or apply an obvious default)
                                                    ├─ now buildable ─► ready to build
                                                    ├─ needs a human decision ─► question
                                                    └─ still too fuzzy ─► flagged
```

The "is this buildable?" judge and the fix step both run as local-model prompts; the routing,
dependency graph, and the final plan are assembled by plain code. The judge is deliberately
strict about one distinction: a **conventional technical choice** (which web framework, which
database) is fine to just pick, but **unspecified product content** (a formula, a business rule,
a data source) is a real unknown worth asking about.

---

## Quickstart

You'll need agent_server running locally with a Gemma model, and a reqoach project to plan from.

```bash
# register the local-model prompts (one-time, idempotent)
python scripts/register_planner_agents.py

# turn a reqoach project into a plan
python scripts/produce_plan.py <reqoach_project_dir> --limit 10
#   -> data/plans/<project>.plan.json
```

---

## What's in `plan.json`

- **tasks** — the ready-to-build work: each has a title, the file/artifact to produce, concrete
  instructions, its dependencies, and a link back to its source requirement.
- **questions** — genuine unknowns for the requirements author to answer.
- **flagged** — requirements/tasks that couldn't be reduced to something buildable.
- **coverage_gaps** — missing-requirement concerns carried over from reqoach.
- **graph** — how the ready tasks depend on each other.

---

## Where it fits

| Stage | Project | Role |
|---|---|---|
| Requirements | [reqoach](../../labs/requirements) | score requirements, find coverage gaps |
| **Plan** | **Planner Agent** (this repo) | **break into buildable tasks + surface open questions** |
| Build | [builder_agent](../builder_agent) | build each task locally and verify it |

Planner Agent only plans — it never builds anything itself. Design details live in
[`documents/`](documents/).

---

## License

Licensed under the Apache License, Version 2.0 — see [LICENSE](LICENSE).
