# Cost and latency measurements

No paid model, speech, or telephony calls have been used for validation. No cost or latency
benchmark is available yet; agree on a budget before running paid evaluations.

| Measure per session | Unit |
| --- | --- |
| Conversation length | Turns |
| Model usage | Input/output tokens, provider, and model version |
| Speech synthesis | Characters and provider/version |
| Speech recognition | Audio seconds and provider/version |
| Responsiveness | Time to first text and first audible response |
| Reliability | Errors and retries by service |
| Spend | Cost by service and session |

Tracing is disabled by default. Voice measurements depend on [stage 2](../docs/ROADMAP.md).
