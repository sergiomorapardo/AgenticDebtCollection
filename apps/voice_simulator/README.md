# Local voice simulator

**Stage 2: planned.** A Gradio interface will simulate a conversation in the browser without dialing a phone number.

```mermaid
sequenceDiagram
    actor User
    participant UI as Browser simulator
    participant STT as Speech to text
    participant Agent as Existing text workflow
    participant TTS as Text to speech
    User->>UI: Start a fictional customer session
    User->>UI: Record one utterance
    UI->>STT: Transcribe audio
    STT-->>UI: Customer text
    UI->>Agent: Send turn with session and event IDs
    Agent-->>UI: Validated reply and current stage
    UI->>TTS: Synthesize reply
    TTS-->>UI: Response audio
    UI->>User: Play audio and display transcript
    Note over UI,Agent: Future interruption handling must track which offer was heard
```

| First version | Later iteration |
| --- | --- |
| Select fixture, start, record, mute, end | Streaming and pause detection |
| Manual turns and transcript panel | Cancel interrupted audio |
| Show offer, commitment, and latency | Validate offer playback before confirmation |

Speech adapters stay outside the financial workflow. Keep the business `session_id` stable;
if using Agent Server, map it to a development thread. No Gradio, STT, or TTS integration is installed yet.

[Delivery stages](../../docs/ROADMAP.md) · [Current architecture](../../docs/ARCHITECTURE.md)
