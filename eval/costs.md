# Cost and latency measurements

No paid model, speech, or telephony calls have been used for validation yet. The harness and
an offline `mock` baseline exist; the Haiku run is pending an agreed budget.

## Scenario harness

`eval/run.py` replays each scripted scenario in `scenarios.json` on the Studio graph with a
fresh SQLite file, then checks business outcomes (agreements, stage, disclosure, contacts).
It records per scenario the result, `wording_status` distribution (`none` means a backend
reply that skips wording), interpretation errors, turns, input/output tokens, USD and
median time to first streamed token. Tracing is always disabled.

```bash
uv run python eval/run.py                      # offline baseline -> eval/results/mock.json
uv run python eval/run.py --provider anthropic --confirm-spend --max-usd 1.00
```

The paid run refuses to start without `--confirm-spend`, refuses an unpriced model, and
checks the budget after every turn. `--max-usd` is a soft cap: the turn in flight can
overshoot it slightly. The report is rewritten after every scenario, so spend is recorded
even if a later scenario fails. Prices: Haiku 4.5 at USD 1 / 5 per million input/output
tokens; cache writes use the 5-minute TTL price (1.25× input) and reads 0.1× input.

Scenarios pass or fail on business outcomes only. `wording_status` and interpretation
`model_error` counts are reported for analysis but do not fail a scenario.

| Run | Passed | Tokens in/out | USD | Notes |
| --- | --- | --- | --- | --- |
| `mock` | 10/10 (1 skipped) | 0/0 | 0.00 | Limited offline parser; no streaming, so no TTFT |
| Haiku 4.5 | Pending | — | — | Estimated ≈ 47 turns × ~4.5k/300 tokens ≈ USD 0.30 |

`audio-interruption` is skipped: it needs interruption-aware voice playback.

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
