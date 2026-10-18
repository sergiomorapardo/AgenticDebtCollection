# AWS staging design

**Stage 3: planned.** This directory contains no provisioned AWS resources or infrastructure code.

```mermaid
flowchart TD
    B[Authenticated browser simulator] -. Text turn .-> A[API Gateway]
    A -.-> L[Lambda: execute one LangGraph turn]
    L -. Read and write through a new adapter .-> D[(DynamoDB)]
    L -. Optional intent extraction .-> H[Claude Haiku API]
    H -. Structured intent .-> L
    L -. Validated reply .-> A
    A -.-> B
    classDef planned fill:#f3f4f6,stroke:#6b7280,color:#111827,stroke-dasharray:5 5
    classDef paid fill:#fbe9e7,stroke:#bf360c,color:#873000
    class B,A,L,D planned
    class H paid
```

Dashed paths describe a proposed deployment. Lambda will process one turn; Studio remains a
development tool. The full development Agent Server will not be packaged inside Lambda.

| Before staging | Required work |
| --- | --- |
| Storage | Implement a DynamoDB adapter; test atomic confirmation, retries, and session concurrency |
| Access | Authentication, authorization, and customer/session isolation |
| Operations | Secrets, observability, recovery, and an explicit budget |
| Voice | Validate the local simulator first; speech adapters remain outside the financial workflow |

Current persistence is SQLite. [Roadmap](../docs/ROADMAP.md) · [Architecture](../docs/ARCHITECTURE.md)
