# Local voice simulator

A local Gradio application wraps the existing conversational graph with ElevenLabs.
It uses manual turns and synthetic customer fixtures, without dialing telephone numbers.

```mermaid
sequenceDiagram
    actor User
    participant UI as Browser simulator
    participant STT as ElevenLabs Scribe v2
    participant Agent as Existing graph and SQLite
    participant TTS as ElevenLabs speech
    User->>UI: Start, record or upload, transcribe
    UI->>STT: Audio (maximum 10 MB)
    STT-->>UI: Editable transcript
    User->>UI: Review and send text
    UI->>Agent: Stable thread and new message ID
    Agent-->>UI: Persisted, reviewed response
    UI->>TTS: Response text
    TTS-->>UI: MP3
    User->>UI: Play audio; acknowledge full offer before next turn
```

## Run

```bash
uv sync --locked --extra voice
cp -n .env.example .env
# Fill ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID locally.
uv run --extra voice collections-voice --provider mock
```

Open http://127.0.0.1:7860. The server binds only to loopback and disables public sharing.
The API key needs Text to Speech and Speech to Text access. The voice must be accessible
through the API for your account. The default speech models are `scribe_v2` and
`eleven_multilingual_v2`. There are no automatic charged retries.

For conversational interpretation with Claude Haiku, also set `ANTHROPIC_API_KEY` and run:

```bash
uv run --extra voice collections-voice --provider anthropic
```

Haiku uses the existing bounded interpretation and reviewed wording flow. Speculative
streaming tokens are not spoken. `mock` uses no LLM, but speech still consumes ElevenLabs credits.

## Try a conversation

1. Click **Iniciar sesión**. Starting a session sends the synthetic greeting `Hola`.
2. Record with the microphone or upload a short clip, then click **Transcribir audio**.
3. Review and correct the transcript, then click **Enviar turno**. Typed turns also work.
4. Identify with `Mi cédula es 1000000001`, then `verificar 1111`.
5. Request `quiero 3 cuotas desde 2026-10-20`.
6. Play the response completely, then tick the full-offer acknowledgement and send `confirmo`.

The second fixture uses `1000000002` and code `2222`. The default demo clock remains
October 19, 2026, America/Bogota. A saved commitment never marks a debt as paid.

## Recovery and local state

- The output player supports play, pause, and volume. Playback is manual; autoplay is disabled.
- A failed speech request leaves the text and business turn saved. **Reintentar audio**
  regenerates only the latest audio, never the agreement or business turn.
- An offer with failed audio cannot be acknowledged until audio generation succeeds.
- The acknowledgement resets for every response and audio regeneration. It gates all
  subsequent offer turns, including renegotiation. **Terminar sesión** always remains available.
- Each new session has an isolated SQLite database and response MP3s in `.local/voice/<id>/`.
  Acknowledgements record the response/audio IDs in that directory. Files stay local and ignored.
- Browser session handles expire after one hour; refreshing starts a new UI session.
  Old local evidence remains on disk. There is no browser resume feature yet.
- `.env` is local to each worktree. Git only distributes `.env.example`; never commit credentials.

## Boundaries

This is a single-user local demo. A checkbox is a manual assertion that the complete offer
was heard, not automatic proof of playback or audibility. Continuous listening, barge-in,
playback cancellation tied to offer state, authentication, and production telephony are planned.
Only the audio and response text needed for speech are sent to ElevenLabs. Use fictional data.

[Roadmap](../../docs/ROADMAP.md) · [Validation](../../docs/VALIDATION.md)
