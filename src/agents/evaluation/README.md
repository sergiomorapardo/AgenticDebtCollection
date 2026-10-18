# Agent evaluation

Reserved for future evaluation utilities. Current checks live in [tests](../../../tests/README.md);
[eval/scenarios.json](../../../eval/scenarios.json) is a scenario catalog, not an evaluation runner.

```mermaid
flowchart LR
    S[Scenario catalog] --> C{Required capability}
    C -->|Text available now| T[Offline workflow assertions]
    C -->|Voice planned| V[Future playback and interruption checks]
    T --> R[Policy, state, and recovery evidence]
    V -. Stage 2 .-> R
```

Evaluate exact amounts, policy adherence, recovery, and conversational clarity.
A recorded commitment must remain distinct from a verified payment.
