# Adaptive negotiation

Each human message starts one graph run. The conversation loops **between turns**, with
business state in SQLite. No autonomous loop pressures the customer while waiting for input.

```mermaid
flowchart TD
    U[New human message] --> I[Interpret intent and explicit terms]
    I --> D[Dialogue strategy and persisted objections]
    D -->|First refusal| Q[Ask why; allow exit]
    D -->|Repeated refusal or stop| X[Close without agreement]
    D -->|Hardship or rejected plan| B[Ask affordable monthly capacity]
    D -->|Counteroffer| P[Compute policy-compliant alternative]
    P -->|Missing information| Q
    P -->|Rejected schedule| Q
    P -->|Eligible terms and date| O[Present exact schedule]
    O --> U
    Q --> U
    B --> U
    D -->|Explicit acceptance| C[Revalidate presented offer]
    C --> S[Record commitment atomically]
    D -->|Dispute, exception, or no viable plan| H[Simulated human review]
```

These arrows include **customer turns**, not internal LangGraph back edges. Studio exposes
`interpret → dialogue_strategy → negotiation / confirmation / handoff / response` per turn.

| Try after verification | Expected outcome |
| --- | --- |
| `no quiero pagar nada` | Ask about the objection; no date-format demand |
| Repeat `no quiero pagar ¿qué vas a hacer?` | Respect refusal and close without threats |
| `perdí mi trabajo` → `250 mil` → `mañana` → `confirmo` | Ask capacity; propose 3 × COP 200,000; record only after confirmation |
| `solo puedo pagar 250 mil` → `cada mes` | Clarify frequency before inferring a monthly plan |
| `no puedo pagar` → `nada` | Simulated review without an unaffordable offer |
| `solo puedo pagar 100 mil al mes` | Policy exception needs simulated human review |
| Reject a presented plan, then request the same schedule | Remember the rejection and ask what must change |
| `acepto si me descuentan` | No acceptance; discount exception goes to simulated review |
| `no quiero pagar, no quiero continuar` | Stop immediately |

Use separate SQLite databases for independent scenarios. A finished conversation stays closed,
including when recalled in a new Studio thread. The mock parser supports these examples but
is not general language understanding. Haiku interprets broader language when explicitly enabled.

Rules remain: at most three monthly installments, first payment within seven days, no discounts,
no extra charges, explicit confirmation, unchanged debt balance until a payment is verified.
Monthly capacity is a ceiling, not an exact installment amount. A one-off amount is not silently
converted into a monthly promise. At most three objection turns or three presented proposals
precede simulated review. Rejected schedules (last three), dialogue stage, and history survive
process restarts. The LLM never writes agreements or changes the payment policy.

## Contextual wording and the internal loop

```mermaid
flowchart LR
    S[Backend strategy and reference] --> E{Eligible non-financial question?}
    E -->|No| P[Persist deterministic reply]
    E -->|Haiku enabled| D[Draft wording from reference only]
    D --> R[Review meaning and tone]
    R -->|Accepted| P
    R -->|Rejected once| D
    R -->|Second rejection or provider error| F[Use reference; expose status]
    F --> P
```

The interpreter uses the current message and up to four recent turns; current terms take
precedence over earlier objections. The writer and reviewer receive only the authorized
reference. This minimizes additional data exposure and prevents mismatched grounding.
No model rewrites an offer schedule, confirmed commitment, terminal outcome, or replay.
At most one interpretation call and four wording/review calls occur on an eligible turn;
exact confirmations and simple dates do not require interpretation calls. Wording errors
fall back visibly through `reply.wording_status`, without switching provider/model.

Offline evidence: `test_screenshot_refusals_are_understood_without_repeating_or_pressuring`,
`test_hardship_capacity_counteroffer_and_confirmation`,
`test_capacity_survives_installment_changes_and_rejection`, and
`test_studio_retry_loop_is_bounded_and_next_turn_resets`.
