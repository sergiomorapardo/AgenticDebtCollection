# Roadmap

Solid green = implemented locally. Dashed paths = future stages.

```mermaid
flowchart TD
    A[1. Text: CLI + Studio + SQLite] -.-> B[2. Local simulated voice call]
    B -.-> C[3. Authenticated web simulator on staging]
    C -.-> D[4. Test telephony with explicitly authorized numbers]
    D -.-> E[5. Controlled pilot with reviewed policies]
    classDef ready fill:#e8f5e9,stroke:#2e7d32,color:#1b5e20
    classDef planned fill:#f3f4f6,stroke:#6b7280,color:#111827,stroke-dasharray:5 5
    class A ready
    class B,C,D,E planned
```

| Stage | Exit evidence |
| --- | --- |
| 1. Text | Offline workflow tests and local Studio API passed; live Haiku check still pending |
| 2. Simulated voice | Microphone → transcript → workflow → audible response, with no phone dialing |
| 3. Staging | Authentication, isolation, external persistence, concurrency, recovery, and observability tested |
| 4. Test telephony | Channel timing, interruptions, and handoff validated with authorized numbers |
| 5. Controlled pilot | Real integrations, policy review, supervision, and stop criteria approved |

## Next: a browser call without a phone number

**Planned, not implemented.** Start with manual turns; then evaluate streaming and interruptions.

```mermaid
flowchart LR
    M[Microphone] -.-> S[Speech to text]
    S -.-> G[Existing text workflow]
    G -.-> T[Text to speech]
    T -.-> B[Browser speaker]
    G -.-> P[Transcript, offer, and saved commitment panel]
```

The voice channel must track which offer was actually heard before accepting confirmation.
Generating audio alone does not establish that the customer heard the terms.

No Gradio app, voice provider, AWS resources, or real calls are included yet.
See [voice simulation](../apps/voice_simulator/README.md) and [deployment](../infra/README.md).
