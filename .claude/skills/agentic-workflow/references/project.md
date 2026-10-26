# Project adapter: AgenticDebtCollection

All paths below are relative to the repository root. Read `AGENTS.md` before
editing: use an isolated worktree and a `feature/` branch, and keep each PR within
12 changed files. Follow the current task's review instructions.

## Local scope

This instruction-only edition preserves design decisions, progressive reference
loading, deterministic controls, conversation isolation, and offline-first
validation. It condenses the original wizard, concepts, and troubleshooting and
uses this repository's code as examples. The course's Python templates, notebook
corpora, hooks, Claude settings, and shared trace quota
are not installed here. Upstream live-run evidence does not validate this copy.

The canonical files live in `.claude/skills/agentic-workflow/`.
`.agents/skills/agentic-workflow` links there for Codex; edit the canonical files.
Invoke `/agentic-workflow` in Claude Code or `$agentic-workflow` in Codex.
Codex's repository discovery and symlink support are documented in
[Build skills](https://learn.chatgpt.com/docs/build-skills).

## Existing implementation

| Need | Read first (paths from repository root) |
| --- | --- |
| Graph and nodes | `src/agents/collections/agent.py`, `src/agents/collections/nodes/`, `src/agents/collections/routes/` |
| Prompts and wording | `src/agents/collections/prompt.py`, `src/agents/collections/prompts/system.jinja`, `src/agents/collections/wording.py` |
| Payment and contact rules | `src/agents/collections/policy.py`, `src/agents/collections/contact.py`, `src/agents/collections/domain.py` |
| Persistence and memory | `src/agents/collections/memory.py`, `src/agents/collections/chat_memory.py`, `src/agents/collections/runtime.py` |
| Channel boundary | `src/collections_lab/service.py`, `src/collections_lab/channels/contracts.py` |
| Offline data | `src/agents/collections/data/fixtures/`, `src/agents/collections/data/policies/demo_policy.json` |
| Checks | `tests/README.md`, `docs/VALIDATION.md`, `eval/scenarios.json` |

Extend these modules instead of generating a second app under the course's
`labs/` layout. Keep optional future runtime capabilities under
`src/agents/skills/` separate from these development skills.

## Configuration and dependencies

- Reuse `collections_lab.config.settings.load_settings` and
  `agents.collections.runtime.AppContext`. The CLI loads only the checkout's
  `.env`; never import credentials from the course or another worktree.
- Default to `COLLECTIONS_LLM_PROVIDER=mock`. For authorized Anthropic runs, keep
  the Haiku model configured by `ANTHROPIC_MODEL`; inspect settings for its value
  instead of importing the course's model, parameters, or prices. Never switch
  to Sonnet or Opus automatically.
- This is a simulation with synthetic Colombian customer fixtures. Preserve
  runnable Spanish utterances; write code identifiers and documentation in English.
- Install through uv; version `pyproject.toml` and `uv.lock` together. The project
  has LangGraph, LangChain's Anthropic adapter, Jinja, and Pydantic. Do not assume
  the course's RAG, embeddings, or evaluation packages are installed.
- Put temporary transcripts, reports, and databases under ignored `.validation/`
  or `.local/`. Do not commit credentials or generated customer transcripts.
  Keep tracing disabled for offline validation.
