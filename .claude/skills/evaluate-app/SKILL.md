---
name: evaluate-app
description: Evaluate an existing LLM app or compare prompts, models, or configurations using fixed datasets, deterministic evaluators, calibrated judges, and evidence on quality, latency, and cost. Use for benchmarks, regression scenarios, or experiment analysis; not ordinary test execution, building a new agent, or production monitoring.
---

# Evaluate an existing LLM app

Answer one decision with evidence: which configuration should be kept, and what
is still missing to decide? One successful conversation is not an evaluation.

1. Read [project.md](references/project.md) for contracts, data, commands, model
   settings, and upstream provenance.
2. Read [design.md](references/design.md). Fix the decision, configurations,
   cases, human-reviewed references, metrics, and resource budget. Reuse choices
   already made; ask only for consequential missing information.
3. Read [evaluators.md](references/evaluators.md). Prefer deterministic checks.
   Use a judge for criteria needing language interpretation and calibrate it
   against fixed human labels before trusting results.
4. Validate the runner, evaluators, and report offline with mock outputs. Reading
   this skill does not authorize model calls or external dataset uploads.
5. Run within the authorized scope, reporting calls, traces, retries, and
   experiments. Obtain missing authorization before paid calls or uploads; do
   not rerun identical experiments merely to get nicer results.
6. Read [results.md](references/results.md), inspect failures and paired cases,
   then report quality, latency, cost, missing scores, and limitations together.

Use the same cases and evaluator versions for every configuration. Ground
references in human-approved requirements or authoritative deterministic rules,
never in the app's own answers. A judge failure is `None`, not an incorrect answer.
Report the denominator next to every score. Use synthetic or anonymized customer
data, including traces.

These are development instructions. Use the sibling `agentic-workflow` skill
when the task is to design or build the app rather than measure it.
