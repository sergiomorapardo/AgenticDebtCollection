# Roadmap

Solid green = implemented locally. Dashed paths = future stages.

```mermaid
flowchart TD
    A[1. Text: CLI + Studio + SQLite] --> B[2. Manual local voice simulation]
    B -.-> C[3. Authenticated web simulator on staging]
    C -.-> D[4. Test telephony with explicitly authorized numbers]
    D -.-> E[5. Controlled pilot with reviewed policies]
    classDef ready fill:#e8f5e9,stroke:#2e7d32,color:#1b5e20
    classDef planned fill:#f3f4f6,stroke:#6b7280,color:#111827,stroke-dasharray:5 5
    class A,B ready
    class C,D,E planned
```

| Stage | Exit evidence |
| --- | --- |
| 1. Text | Offline workflow tests and local Studio API passed; live Haiku check still pending |
| 2. Simulated voice | Microphone → transcript → workflow → audible response, with no phone dialing |
| 3. Staging | Authentication, isolation, external persistence, concurrency, recovery, and observability tested |
| 4. Test telephony | Channel timing, interruptions, and handoff validated with authorized numbers |
| 5. Controlled pilot | Real integrations, policy review, supervision, and stop criteria approved |

## Local voice simulation

**Implemented locally:** manual Gradio turns with ElevenLabs transcription and speech.
Continuous listening, automatic playback tracking, and interruption handling remain planned.

```mermaid
flowchart LR
    M[Microphone or uploaded audio] --> S[Speech to text]
    S --> R[Review transcript] --> G[Existing text workflow]
    G --> T[Text to speech]
    T --> B[Browser speaker]
    G --> P[Transcript, offer, and saved commitment panel]
```

The first version requires manual acknowledgement of the current complete offer after
audio is generated, before sending the next turn. An acknowledgement is recorded against
the response and audio; it is not automatic proof of playback or audibility.
Automatic interruption-aware playback tracking remains a later iteration.

### TODO: streaming voice

- [ ] Stream response audio only after the complete response text passes the existing
  business checks; preserve the exact approved amounts and dates.
- [ ] Validate the streaming endpoint with the configured voice and Free account,
  recording credits before and after the test, latency, and playback completion.
- [ ] Add realtime transcription, interruption handling, and reliable full-offer
  playback tracking in a later iteration. Keep manual turns as the current baseline.

No AWS resources or real telephone calls are included.
See [voice simulation](../apps/voice_simulator/README.md) and [deployment](../infra/README.md).
