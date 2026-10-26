# Validate and deliver

## Offline first

Run from the isolated worktree with its locked environment. Set
`COLLECTIONS_LLM_PROVIDER=mock`, `LANGSMITH_TRACING=false`, and
`LANGCHAIN_TRACING_V2=false`. Use synthetic fixtures and temporary databases.

```bash
uv run --locked ruff check src tests scripts
uv run --locked ruff format --check src tests scripts
uv run --locked pytest -q
git diff --check
```

Choose relevant tests from `tests/README.md` for a narrow code change; use the
full suite when shared behavior changes. For documentation-only changes, check
skill metadata, links, paths, and the diff instead of calling a model. Run the
startup smoke check in `docs/VALIDATION.md` when graph registration or server
integration changes; run a build when packaging changes.

| Changed behavior | Check |
| --- | --- |
| Prompt | Inspect formatted messages; missing Jinja fields fail visibly |
| Typed extraction | Bad input fails validation; no unsafe action is inferred |
| Tool | Found, not-found, invalid argument, timeout/error when applicable |
| Policy | Boundary amounts, dates, and forbidden terms remain deterministic |
| Workflow | Identity before disclosure; confirmation before one agreement |
| Persistence | Replay, isolation, stale offers, rollback, and recovery |
| Optional RAG | Expected page retrieved; fabricated citation rejected; absent answer handled |

The mock interpreter covers a limited vocabulary. Passing a mock conversation
does not prove arbitrary Spanish understanding. Offline voice tests do not
establish browser playback or telephony behavior.

## Authorized live run

State the command, configured model, synthetic inputs, bounded estimate of model
calls and retries, and trace uploads. Reuse explicit authorization for that scope;
otherwise obtain it before running. Keep Haiku unless the user selects another
model. Do not claim exact cost from an unverified price or equate traces with
calls when there are nested runs.

Read the transcript, state changes, and usage. Confirm that tool results reach
subsequent model calls and outcomes meet the criteria. Fix specific failures,
repeat offline checks, and stay within the authorized retry budget. If access is
missing, leave the command to run later and report what remains unverified.

## Delivery

Give paths, checks and outcomes, a reproducible command, and evidence locations.
Distinguish offline, live model, server, and browser verification. To compare
quality, use `evaluate-app` with fixed cases and references; one successful demo
is not an evaluation.
