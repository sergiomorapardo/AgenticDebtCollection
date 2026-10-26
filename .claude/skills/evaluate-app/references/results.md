# Read results before deciding

Keep raw rows and a report under the ignored path in [project.md](project.md).
Include case ID, configuration, input, expected outcome, actual response/state,
scores, comments, errors, latency, and observed usage. Use synthetic or anonymized
data and exclude credentials even from local evidence.

For authorized LangSmith experiments, read actual runs and feedback. Flush
pending uploads and wait within a bounded deadline for expected scores. Report
missing feedback as incomplete; never wait forever or substitute zeros. Reading
existing results needs no new model calls but still needs service access.

| Report section | Evidence |
| --- | --- |
| Run identity | Commit, configuration, provider/model, data/rubric versions, clock |
| Coverage | Planned, attempted, completed, failed, and skipped cases with reasons |
| Quality | Metric and scored denominator; missing/not-applicable counts separately |
| Paired comparison | Same IDs scored by both versions; better, worse, and equal |
| Latency/cost | Answer latency, tokens, judge/calibration overhead, retries |
| Cases to inspect | Failures/disagreements and expected/actual state and responses |
| Limitations | Mock vs live, sample size, missing feedback, calibration weaknesses |

Average valid scores only and show the count. A boolean returned as `1.0` or
`0.0` remains valid: test for `None`, not truthiness. Report failed app runs
separately from evaluator failures; do not hide them by removing their rows.

For monetary costs, cite the applicable current price and distinguish measured
from estimated cost. Include untraced judge calls. Without verified pricing,
report tokens and mark monetary cost unknown; do not reuse course prices.

## Interpretation

Read failed and differing cases before averages. Separate interpretation, policy,
state/persistence, and wording failures. In retrieval work, distinguish missing
evidence from generation that ignored available evidence. Fix the component
supported by the observations.

- **Supported observation:** what happened on tested cases, with counts and
  conditions. General claims need representative coverage and uncertainty analysis.
- **Hypothesis:** a direction suggested by limited cases, plus what would test it.
- **Not established:** claims outside the sample, offline behavior, or calibrated
  criteria. Say when neither version has demonstrated an advantage.

A paired sign test can summarize wins/losses, but ties, exclusions, sample size,
and dependent conversations matter. A p-value alone does not establish product
readiness. Additional cases or repetitions must fit the authorized budget.

Deliver a concise comparison, representative cases, report path, existing
experiment links if applicable, and next evidence needed. Do not claim browser
voice quality from text tests or live language quality from mocks.
