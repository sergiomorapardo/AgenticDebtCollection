"""Map event-bound semantic meaning; only the backend authorizes continuation."""

from agents.collections.semantic_state import current_understanding


def interpret_continuation(state, session, ctx):
    """Never reclassify a turn or infer continuation from its literal wording."""
    understanding = current_understanding(state)
    if understanding is None or understanding.status == "model_error":
        return "unclear", "model_error"
    intent = {
        "continue": "resume",
        "options": "resume",
        "end": "stop",
        "decline": "decline",
    }.get(understanding.turn.intent, "unclear")
    return intent, understanding.status
