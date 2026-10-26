# Design the comparison

State the decision first. Ask one material unanswered question at a time using
the host's question tool or plain text. Do not repeat decisions already supplied.

| Decision | Define |
| --- | --- |
| Comparison | Baseline and candidate; one changed component at a time |
| Cases | Stable ID, turns, account fixture, clock, and initial state |
| References | Human-reviewed response properties and state transitions |
| Metrics | Deterministic business invariants; language quality where needed |
| Calibration | Fixed labeled responses with representative passes and failures |
| Size | Diagnostic subset, then the agreed broader set or repetitions |
| Resources | Provider, calls, retries, concurrency, uploads, and stop condition |

Start with [project.md](project.md). A scenario description is not executable
evidence: supply turns, fixtures, and assertions. Include colloquial Spanish,
ambiguous acceptance, refusal, reported payment, out-of-policy terms, and missing
identity when relevant. Mark unsupported scenarios as skipped with a reason;
do not count them as passes.

## Comparability

- Freeze cases and references across configurations. If a reference changes,
  rerun affected comparisons under an authorized budget.
- Reset business state between configurations; use the same clock and fixture
  values with isolated session/event IDs except in deliberate replay tests.
- Score persisted outcomes as well as text. A fluent promise is not proof of an
  agreement, and a stored commitment is not proof of a completed payment.
- Compare full conversations when follow-ups matter. A single turn cannot prove
  session memory, replay, or confirmation behavior.

## Plan before running

Show configurations, dataset size/source, evaluator versions, calibration gate,
output, and budget. For a fixed one-call app, answer calls are cases × repetitions
× configurations. Multi-turn graphs, tool loops, retries, and judges add calls;
estimate and cap them separately. Include calibration and judge calls even if
evaluator tracing is disabled.

Reuse existing experiments for analysis. Before uploading, check for equivalent
runs on the same commit, data, rubric, and settings. Never delete or overwrite
datasets or experiment history without authorization. Paid calls and uploads
need authorization covering their scope; do not request it again if supplied.
