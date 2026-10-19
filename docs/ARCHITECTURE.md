# Architecture

One LangGraph workflow processes **one turn per invocation**. With `anthropic`, every new human
message receives one contextual interpretation. All consumers reuse it; backend rules authorize
actions. The `mock` provider is an explicit offline fixture. There is no parallel model fan-out.

## The shared collection graph

The CLI graph below is shared with the conversational Studio entry in [agent.py](../src/agents/collections/agent.py).
Solid arrows are fixed edges; dashed arrows are conditional routes.

```mermaid
flowchart TD
    START([START]) --> understand_message
    understand_message --> intake
    intake -.-> interpret
    intake -.-> prepare_reply
    intake -.-> close
    interpret --> dialogue_strategy
    dialogue_strategy -.-> promise
    dialogue_strategy -.-> negotiation
    dialogue_strategy -.-> confirmation
    dialogue_strategy -.-> handoff
    dialogue_strategy -.-> close
    dialogue_strategy -.-> prepare_reply
    confirmation -.-> commitment
    confirmation -.-> prepare_reply
    promise --> prepare_reply
    negotiation --> prepare_reply
    commitment --> prepare_reply
    handoff --> prepare_reply
    close --> prepare_reply
    prepare_reply -. Eligible Haiku question .-> draft_reply
    prepare_reply -. Deterministic or replay .-> response
    draft_reply -. Reviewed mode .-> review_reply
    draft_reply -. Streaming mode .-> response
    review_reply -. One retry maximum .-> draft_reply
    review_reply -. Accepted or reference fallback .-> response
    response --> END([END])
```

Generate the current graph directly from code with `uv run collections-lab graph`.
The next customer message starts a new invocation using the same persisted session.
The internal wording retry is bounded to two drafts; it never retries negotiation or writes.
Model review is a quality filter, not a proof of semantic safety. The local simulation still
requires broader adversarial and conversational evaluation before a real pilot.

## Studio conversation entry

```mermaid
flowchart TD
    START([Human message]) --> human_input
    human_input -. Saved reply or business receipt .-> assistant_message
    human_input -. New event .-> understand_message
    understand_message --> route_chat_message
    route_chat_message -. Unidentified .-> identify_customer
    route_chat_message -. Unverified .-> verify_identity
    route_chat_message -. Verified turn .-> intake[Shared collection graph]
    route_chat_message -. Recall request .-> recover_history
    route_chat_message -. Closed or failed interpretation .-> assistant_message
    identify_customer --> assistant_message
    verify_identity -. Verified .-> recover_history
    verify_identity -. Failed .-> assistant_message
    recover_history -. New business session .-> intake
    recover_history -. Existing conversation .-> assistant_message
    intake --> response
    response --> assistant_message
    assistant_message --> END([AI message])
```

Studio supplies thread and message identifiers. Backend bindings select the customer and
business session after fictional identity verification. New threads verify again before
reading memory. Returning threads resume existing sessions; terminal sessions stay terminal.
Internal summaries remain private. Customer replies use relevant account facts, never transcript excerpts.

## Interpretation and policy

```mermaid
flowchart LR
    T[Every new human message] --> P{Configured provider}
    P -->|mock| M[Offline parser]
    P -->|anthropic| H[Claude Haiku API]
    M --> I[Structured intent and proposed terms]
    H --> I
    J[Trusted Jinja instructions for backend stage] --> H
    I --> C[Validate identity evidence, consent, policy and COP]
    C --> R[Authorized reference]
    R --> W[Contextual streaming or reviewed prose]
    W --> F[Persist completed conversation]
    R --> F2[Persist and deliver financial receipt]
    classDef local fill:#e8f5e9,stroke:#2e7d32,color:#1b5e20
    classDef paid fill:#fbe9e7,stroke:#bf360c,color:#873000
    class T,P,M,I,C,R,F local
    class W paid
    class H paid
```

| Code owns | Demo rule |
| --- | --- |
| Meaning | One stage-aware interpretation; no current-message phrase shortcuts in production |
| Acceptance | Unconditional consent to the current valid offer, without changed terms |
| Initial contact | Authorized `phone_simulation` channel; Colombia calendar; contact history |
| Contact window | Mon–Fri 07:00–19:00; Sat 08:00–15:00; end excluded; Sundays/holidays blocked |
| Contact frequency | At most one established contact per day; no alternate channels in the same week |
| Identity | Fixture code before disclosing the balance; three failed attempts close the session |
| Payment terms | `demo-co-v1`: 1–3 monthly installments; first date today through today + 7 days |
| Money | Integer COP; preserve the total; adjust month-end dates; no discounts or added charges |
| Turn limits | Bounded clarifications/proposals; handoff after 20 persisted turns |

These are implemented demo controls, not a certification of legal compliance. The model cannot
authorize an exception, change the balance, or record a payment.

## Confirmation and persistence

```mermaid
sequenceDiagram
    participant Customer
    participant Graph as LangGraph nodes
    participant Store as SQLite
    Customer->>Graph: Confirm the previously presented offer
    Graph->>Graph: confirmation revalidates the presented offer
    alt Offer already stale or invalid
        Graph->>Store: response persists invalidation and turn, no agreement
        Graph-->>Customer: New proposal and confirmation required
    else Offer valid at confirmation
        Graph->>Graph: commitment requests a write
        Note over Graph,Store: response finalizes in one transaction
        Graph->>Store: Revalidate persisted offer, identity, balance, and policy
        alt Still valid and storage succeeds
            Graph->>Store: Save agreement, session, and event result atomically
            Store-->>Graph: Commit succeeds
            Graph-->>Customer: Commitment recorded
        else Late revalidation or persistence failure
            Store-->>Graph: Roll back writes, propagate error
            Note over Customer,Graph: No successful registration reply
        end
    end
```

`ConversationService` serializes the entire local turn in a transaction. Direct Studio execution
uses optimistic session revisions and a transaction at finalization. Staging needs concurrency
validation against its external storage adapter.

SQLite stores accounts, sessions, offers, agreements, events, contacts, Studio identity bindings,
and chat replay responses. Onboarding transitions and their responses commit atomically.
**Agreements are promises: the account balance stays unchanged.**

## Retries and trusted state

```mermaid
flowchart TD
    T[Incoming turn] --> K{"Existing session_id + event_id?"}
    K -->|No| G[Process against persisted session and account]
    K -->|Yes| C{Same request content?}
    C -->|Yes| R[Return saved receipt; no model call or duplicate agreement]
    C -->|No| E[Reject conflicting event]
    G --> W[Persist turn atomically]
    W --> D{Final chat delivery succeeds?}
    D -->|No| R
```

The CLI accepts `session_id`, `event_id`, `account_id`, and `text`. Studio accepts only `messages`;
its adapter derives event IDs and retrieves the session/account binding internally. The session is bound to
its account; identity, balance, and agreement IDs come from trusted storage. Session IDs alone
are not authentication. Closed, handed-off, and agreed sessions cannot reopen.

| Situation | Outcome |
| --- | --- |
| Unclear or rejected offer | Invalidate it; require newly presented terms before confirmation |
| Changed balance or policy | Invalidate old terms; require a new proposal and confirmation |
| Reported payment or dispute | Simulated handoff; no payment posting |
| Provider error | No identity attempt, offer or agreement; no provider/model switch |
| Failed chat delivery after commit | Recover the original business receipt before classification |
| Stop during interrupted verification | Close the new thread before reading previous offers |

## Channel boundaries

```mermaid
flowchart LR
    C[CLI today] --> T[Turn input]
    S[Studio Chat] --> M[Human messages and verified customer memory]
    M --> T
    V[Voice simulator: planned] -. Transcribed text .-> T
    T --> G[Same collections workflow]
    G --> R[Validated reply]
    R -. Planned TTS .-> A[Browser speaker]
```

Voice, production authentication, external persistence, and AWS are future work. See the [roadmap](ROADMAP.md).
