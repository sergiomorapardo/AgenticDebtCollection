# Local persistence and DynamoDB development

## What runs today

```mermaid
flowchart TD
    C[Studio Chat] --> G[LangGraph]
    G --> K[Studio development checkpoints]
    G --> S[SQLite business repository]
    S --> A[Accounts, sessions, history]
    S --> O[Offers, commitments, contact records]
    S --> I[Verified thread bindings and replay responses]
    F[JSON fixtures] --> S
    S -. Planned adapter .-> D[DynamoDB Local]
    D -. Staging validation .-> AWS[Amazon DynamoDB]
```

| Data | Owner / location | Behavior |
| --- | --- | --- |
| Business records | `SQLiteRepository`, default `.local/collections.sqlite3` | Accounts, session history, offers, agreements, events, contacts |
| Studio identity and replay | `ChatMemory`, same SQLite file | Verified customer binding per thread; cached message results |
| Graph messages/checkpoints | LangGraph development runtime; local `.langgraph_api/` artifacts | Separate from business truth; not a production durable-checkpoint guarantee |
| Seed customers, accounts, codes | Versioned JSON under `src/agents/collections/data/fixtures/` | Synthetic inputs; existing accounts use insert-if-absent initialization |
| Customer summaries | Derived on demand from SQLite | Up to three verified sessions and three commitments; no LLM or destructive compaction |

`COLLECTIONS_DATABASE_PATH` selects the database. Relative paths resolve from the process
working directory, so each worktree normally has independent data. All paths above are
ignored by Git except the versioned fixtures. Sharing an absolute database path intentionally
shares business history and contact limits. Changing the path selects another database;
it does not migrate records.

The primary default is `~/Developer/AgenticDebtCollection/.local/collections.sqlite3`.
Business persistence survives process restarts while the file is retained. SQLite transactions,
unique event keys, and session revisions protect the current local workflow.

## Recommended next storage target

**Use DynamoDB Local in Docker when implementing the DynamoDB repository adapter.** It is
AWS's official local development option and does not require calling the cloud service.
Use a persistent volume plus `-dbPath` for retained demo data; `-inMemory` is for disposable
runs. Configure an explicit local SDK endpoint and dummy local credentials.
[AWS setup](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/DynamoDBLocal.html),
[AWS options and differences](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/DynamoDBLocal.UsageNotes.html).

| Option | Best fit for this project | Boundary |
| --- | --- | --- |
| SQLite today | Fast offline behavior and regression tests | Does not emulate DynamoDB semantics |
| DynamoDB Local | DynamoDB data model and repository integration | Does not reproduce managed throughput, availability, or recovery behavior |
| LocalStack | Later integration of several AWS services in one local environment | Check required service/API coverage and current limitations |

LocalStack also supports DynamoDB and documents its limitations:
[LocalStack DynamoDB](https://docs.localstack.cloud/aws/services/dynamodb/).
The preference for DynamoDB Local now is a project recommendation based on the narrower scope.

**No DynamoDB adapter, Docker service, or migration is implemented yet.** Changing the
SQLite path or starting an emulator alone cannot switch the agent to DynamoDB.

```mermaid
flowchart LR
    A[Define repository contracts] --> B[Model customer and session access patterns]
    B --> C[Implement DynamoDB conditional writes and transactions]
    C --> D[Run shared behavioral cases against DynamoDB Local]
    D --> E[Validate AWS-specific behavior on staging]
```

Refactor direct SQL in `ChatMemory` behind repository operations before adding an adapter.
Preserve atomic agreement/session/event writes, atomic identity/replay transitions, account
isolation, pending-offer recovery, and bounded history retrieval. Test pagination explicitly.
The business repository and graph checkpointer are separate migration decisions.

## Branches, worktrees, and local data

| After a squash merge | Current policy |
| --- | --- |
| Remote PR source branch | Deleted automatically |
| Local branch | Retained |
| Local worktree directory | Retained |
| Ignored local database, `.env`, `.venv`, checkpoints | Remain in that checkout; never merged into `main` |
| Commit history | Squash commit on `main`; original commits remain inspectable through the PR |

This follows [AGENTS.md](../AGENTS.md): automatic remote cleanup does not authorize removing
local branches or worktrees. Before any requested local cleanup, inspect uncommitted changes
and preserve needed ignored data. Deleting a worktree can remove its local database;
merging the branch does not copy that database into the primary checkout.

See [use cases](USE_CASES.md) for persistence, replay, concurrency, and migration acceptance cases.
