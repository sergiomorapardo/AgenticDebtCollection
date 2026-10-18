"""Render trusted system instructions; customer content belongs in JSON messages."""

from typing import Literal, get_args

from jinja2 import Environment, PackageLoader, StrictUndefined

PromptTask = Literal[
    "continuation",
    "onboarding_classify",
    "onboarding_draft",
    "onboarding_review",
    "reply_draft",
    "reply_review",
    "interpret",
]
PromptStage = Literal[
    "awaiting_customer",
    "awaiting_identity",
    "ready",
    "awaiting_terms",
    "awaiting_confirmation",
    "agreed",
    "closed",
    "handoff",
]

_ENVIRONMENT = Environment(
    loader=PackageLoader("agents.collections.prompts", "."),
    undefined=StrictUndefined,
    autoescape=False,  # Plain-text system instructions, not HTML.
    keep_trailing_newline=False,
)
_TEMPLATE = _ENVIRONMENT.get_template("system.jinja")


def render_prompt(task: PromptTask, *, stage: PromptStage | None = None) -> str:
    """Render only packaged templates with bounded backend context.

    Never pass utterances, histories, template source or arbitrary context here.
    Callers serialize untrusted customer data into the separate human message.
    """
    if task not in get_args(PromptTask):
        raise ValueError(f"Unknown prompt task: {task!r}")
    if stage is not None and stage not in get_args(PromptStage):
        raise ValueError(f"Unknown conversation stage: {stage!r}")
    return _TEMPLATE.render(task=task, stage=stage).strip()
