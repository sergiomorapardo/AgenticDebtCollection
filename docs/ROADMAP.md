# Roadmap

Solid green = implemented locally. Dashed paths = future stages.

```mermaid
flowchart TD
    A[1. Text: CLI + Studio + SQLite] --> B[2. Push-to-talk local voice simulation]
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

**Implemented locally:** a standalone HTML call interface over a local FastAPI API, with push-to-talk turns with automatic transcription, sending, and audio
playback. Browser playback completion enables the next reply. Continuous listening and
interruption-aware playback tracking remain planned.

```mermaid
flowchart LR
    M[Browser microphone] --> S[Speech to text]
    S --> G[Existing text workflow]
    G --> T[Text to speech]
    T --> B[Automatic browser playback]
    G --> P[Collapsible conversation transcript]
```

The browser's audio-ended event is bound to the current response audio before enabling
another reply. Offer confirmation is also gated on the server; events for older audio
cannot unlock a new offer. An ended event does not prove audibility or uninterrupted
listening. Stronger interruption-aware playback tracking remains a later iteration.

### TODO: streaming voice

- [ ] Stream response audio only after the complete response text passes the existing
  business checks; preserve the exact approved amounts and dates.
- [ ] Validate the streaming endpoint with the configured voice and Free account,
  recording credits before and after the test, latency, and playback completion.
- [ ] Add realtime transcription, interruption handling, and reliable full-offer
  playback tracking in a later iteration. Keep push-to-talk turns as the current baseline.

No AWS resources or real telephone calls are included.
See [voice simulation](../apps/voice_simulator/README.md) and [deployment](../infra/README.md).
