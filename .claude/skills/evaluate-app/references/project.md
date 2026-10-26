# Project adapter: AgenticDebtCollection evaluation

All paths below are relative to the repository root. Read `AGENTS.md` and the
current task's instructions for worktrees, branches, PR size, and review.

## Local scope

This instruction-only edition retains comparison design, code-first evaluators,
human calibration, missing-score handling, and evidence-based reporting. It
folds validation and troubleshooting into the relevant references and replaces
the RAG-specific adapter with collections contracts. It does not install the
course's scripts, corpus, datasets, dependencies, hooks, Claude settings, trace
quota, or prices. Upstream live-run evidence does not validate this adaptation.

Canonical files live in `.claude/skills/evaluate-app/`.
`.agents/skills/evaluate-app` links there for Codex. Invoke `/evaluate-app` in
Claude Code or `$evaluate-app` in Codex. Codex discovery and symlink support are
documented in [Build skills](https://learn.chatgpt.com/docs/build-skills).

## What exists

| Input (paths from repository root) | Use |
| --- | --- |
| `eval/scenarios.json` | Descriptions and expected outcomes, not a runnable dataset by itself |
| `docs/VALIDATION.md` | Reproducible mock conversations and verification boundaries |
| `tests/README.md`, `tests/test_workflow.py`, `tests/test_policy.py`, `tests/test_contact.py` | Deterministic checks and fixtures |
| `src/collections_lab/service.py`, `src/collections_lab/channels/contracts.py` | Request/response boundary for a runner |
| `src/agents/collections/runtime.py`, `src/agents/collections/memory.py` | Explicit clock, provider, and persistence |
| `src/collections_lab/config/settings.py`, `.env.example` | Model/provider settings and explicit credential loading |

Inspect the current branch before proposing a runner or command. At adaptation
time, `main` has no `collections-lab evaluate` command. A separate open PR for a
runner is not an installed capability: reuse it only after it is on the chosen
base. Do not copy the course's `evaluation.py` command.

## Environment and evidence

- Use uv and locked dependencies. Default to mock and disable
  `LANGSMITH_TRACING` and `LANGCHAIN_TRACING_V2` for offline checks.
- Use this checkout's explicit configuration. For authorized Anthropic tests,
  keep configured Haiku unless another model is selected. Do not copy the
  course's model ID, unsupported parameters, or price table.
- OpenEvals is not a declared direct dependency. LangSmith may be transitive;
  its presence does not configure evaluation or authorize uploads. Inspect the
  lock and APIs before adding integrations; version dependency changes together.
- Keep reports, transcripts, and manifests under ignored `.validation/evaluations/`.
  Use separate temporary SQLite databases per case/configuration with explicit
  clocks and session/event IDs. Replay/recovery cases reuse only their own state.
- Record commit, configuration, dataset/reference and rubric versions, model,
  clock, usage, and errors. Never record credentials.
- Do not import the course's 5,000-trace quota as a local entitlement. Estimate
  calls and uploads for the actual run and respect the authorized budget.
