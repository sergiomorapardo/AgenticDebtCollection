# Build evaluators and validate offline

Write one evaluator per criterion a person can state in one sentence. A useful
result has `key`, `score`, and `comment`; use stable English `snake_case` keys.
Scores are booleans or a documented numeric scale. Use `None` with a reason for
not-applicable, missing evidence, or judge failure, keeping those reasons
separate. Never replace a missing score with zero.

For LangSmith, verify the installed API and use its evaluator contract
(`inputs`, `outputs`, `reference_outputs`) as appropriate. Expose evidence through
an evaluation adapter rather than changing public customer output just for scoring.

## Code first

| Criterion | Evidence |
| --- | --- |
| No disclosure before verification | Identity state and balance/offer fields |
| Terms satisfy policy | Policy result and exact amounts/dates |
| Confirmation creates one agreement | State transition and stored count |
| Duplicate event is idempotent | Cached response and unchanged agreements |
| Reported payment is not verified payment | Business status after the claim |
| Customers stay isolated | State/replies from distinct customer fixtures |
| Citation validity, if retrieval exists | Cited file/page belongs to retrieved evidence |
| Tone or paraphrased meaning | Human review or a calibrated judge with a rubric |

Document the rule, return useful failures, and test pass, fail, and not-applicable
when meaningful. Use authoritative policy and human-reviewed expectations, never
the answer under test as its own reference. Financial checks must follow the
domain's representation and locale instead of approximate substring matching.

## Judges

Add a judge only where deterministic checks cannot measure the criterion.
OpenEvals prompts may be a starting point if intentionally introduced; neither
OpenEvals nor a `make_judge` helper is bundled with this skill.

1. Define concrete pass/fail cases and what the judge must ignore.
2. Ground the rubric in the answer language and domain. Separate tone,
   correctness, groundedness, and compliance rather than one vague score.
3. Validate prompt fields before calling the model. Delimit customer text and
   candidate answers as data, including adversarial instructions within them.
4. Request a concise evidence-based explanation and structured score. Parse or
   provider failures are evaluator errors, not incorrect app answers.
5. Version the rubric and use it consistently. Keep configured Haiku unless
   another judge is explicitly selected.

## Calibration

Use fixed human-labeled responses including paraphrases and representative
failures: invented figures, missing confirmation, or another customer's data.
Eight balanced examples are a starting point, not proof of reliability. Set the
acceptance gate before running and inspect every disagreement. Stop experiments
if it fails. Improve the rubric without dropping hard cases; record its version.

## Offline validation

- Validate case/reference fields, stable IDs, and source metadata if relevant.
- Run the app with mock or fake models and isolated state.
- Format rubrics without invoking judges. Fake malformed output and provider
  failure; confirm both stay `None` with evaluator-error reasons.
- Feed reports a failed app run, missing score, and not-applicable score. None
  may disappear or silently count as a valid zero.
- Disable tracing and remove API keys from the child process environment, not
  `.env`. Mock remote clients so dry runs cannot create datasets or experiments.
- Run relevant existing tests and lint. Documentation-only changes need
  metadata/link checks, not paid calibration.
