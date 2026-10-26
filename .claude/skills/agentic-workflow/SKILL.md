---
name: agentic-workflow
description: Design or extend a LangChain or LangGraph app with explicit state, prompts, tools, memory, and deterministic controls. Use for agent architecture, workflow implementation, or choosing between a chain, tool loop, and graph. For dataset benchmarks or model judges on an existing app, use evaluate-app instead.
---

# Design and build an agentic workflow

Decide the shape, prompt, data access, state, and controls before implementing the
smallest change that satisfies the request. Preserve the existing application's
architecture and business guarantees.

1. Read [project.md](references/project.md) for paths, configuration, dependencies,
   and the origin of this adaptation.
2. Use the decision table below to resolve missing design decisions.
   Reuse decisions and authorization already supplied. Ask one material question
   at a time through the host's available question tool, or plain text when
   unavailable; do not depend on a Claude-only tool or field.
3. Present the intended flow, affected files, and observable success criteria.
   Continue when the request already authorizes implementation.
4. Read [build.md](references/build.md) for prompts, tools, memory, and controls.
5. Run the applicable offline checks in [validation.md](references/validation.md).
6. Before a live run, establish its model, call budget, tracing destination, and
   existing authorization. Request approval for paid calls or uploads outside
   that authorization. Never silently upgrade the model or retry indefinitely.
7. Deliver paths, commands, evidence, and unverified behavior. A mock run proves
   control flow, not live model quality.

Read references only when their phase is relevant. For a comparison of prompts
or models, use the sibling `evaluate-app` skill. These are development
instructions, not capabilities loaded by the customer-facing collections agent.

## Resolve the design

Skip decisions the request or code already answers. Offer a recommended option
when a missing decision changes scope, behavior, cost, or an external action.

| Decision | Useful choices |
| --- | --- |
| Goal | Observable outcome and the failure to prevent |
| Shape | Fixed chain; model-selected tools; existing graph with explicit state |
| Prompt | Static messages; versioned Jinja for optional fields and variable lists |
| Output | Pydantic fields for code; free text when only a person reads it |
| Facts | Authoritative structured lookup; cited document retrieval if needed |
| Conversation | Independent request; session history; persisted business state |
| Controls | Identity, policy, confirmation, schemas, and replay handling |
| Evidence | Offline checks; authorized live model and tracing run |

Use a chain when steps are fixed and a tool loop when the model must select
facts to fetch. Keep the existing graph when business state and gates interact.
Before implementing, show the intended flow, modules, data/state ownership,
success criteria, and estimated calls and traces. Do not replace the collections
graph with a generic loop to match a course example. New retrieval features are
optional and require their own implementation and validation.
