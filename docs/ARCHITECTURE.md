# Architecture

One LangGraph workflow processes **one turn per invocation**. Code routes the turn; an optional
LLM extracts intent. There is no multi-agent supervisor or parallel model fan-out.

## The implemented graph

Node names and edges match [agent.py](../src/agents/collections/agent.py).
Solid arrows are fixed edges; dashed arrows are conditional routes.

```mermaid
flowchart TD
    START([START]) --> intake
    intake -.-> interpret
    intake -.-> response
    intake -.-> close
    interpret -.-> promise
    interpret -.-> negotiation
    interpret -.-> confirmation
    interpret -.-> handoff
    interpret -.-> close
    interpret -.-> response
    confirmation -.-> commitment
    confirmation -.-> response
    promise --> response
    negotiation --> response
    commitment --> response
    handoff --> response
    close --> response
    response --> END([END])
```

Generate the current graph directly from code with `uv run collections-lab graph`.
The next customer message starts a new invocation using the same persisted session.

## Interpretation and policy

```mermaid
flowchart LR
    T[Verified customer text] --> P{Configured provider}
    P -->|mock| M[Offline parser]
    P -->|anthropic| H[Claude Haiku API]
    M --> I[Structured intent and proposed terms]
    H --> I
    I --> C[Code validates policy and computes dates / COP]
    C --> R[Deterministic reply template]
    classDef local fill:#e8f5e9,stroke:#2e7d32,color:#1b5e20
    classDef paid fill:#fbe9e7,stroke:#bf360c,color:#873000
    class T,P,M,I,C,R local
    class H paid
```

| Code owns | Demo rule |
| --- | --- |
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

SQLite stores accounts, sessions, offers, agreements, events, and contacts.
**Agreements are promises: the account balance stays unchanged.**

## Retries and trusted state

```mermaid
flowchart TD
    T[Incoming turn] --> K{"Existing session_id + event_id?"}
    K -->|No| G[Process against persisted session and account]
    K -->|Yes| C{Same request content?}
    C -->|Yes| R[Return cached reply; no duplicate agreement]
    C -->|No| E[Reject conflicting event]
    G --> W[Persist turn atomically]
```

Only `session_id`, `event_id`, `account_id`, and `text` are public input. The session is bound to
its account; identity, balance, and agreement IDs come from trusted storage. Session IDs alone
are not authentication. Closed, handed-off, and agreed sessions cannot reopen.

| Situation | Outcome |
| --- | --- |
| Unclear or rejected offer | Invalidate it; require newly presented terms before confirmation |
| Changed balance or policy | Invalidate old terms; require a new proposal and confirmation |
| Reported payment or dispute | Simulated handoff; no payment posting |
| Provider error | Neutral reply; no automatic provider/model switch |

## Channel boundaries

```mermaid
flowchart LR
    C[CLI today] --> T[Turn input]
    S[Studio today] --> T
    V[Voice simulator: planned] -. Transcribed text .-> T
    T --> G[Same collections workflow]
    G --> R[Validated reply]
    R -. Planned TTS .-> A[Browser speaker]
```

Voice, authentication, external persistence, and AWS are future work. See the [roadmap](ROADMAP.md).
