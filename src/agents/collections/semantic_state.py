"""Bind internal semantic results to the current event, never to public input."""

import hashlib
import re

from agents.collections.semantic import TurnResult, TurnUnderstanding


def bind_understanding(state, result: TurnResult) -> dict:
    """Create a checkpoint-safe envelope after backend input validation."""
    if not state.get("event_id"):
        raise ValueError("A semantic result requires an event identifier")
    return {
        "event_id": state["event_id"],
        "text_digest": hashlib.sha256(state["text"].encode()).hexdigest(),
        "turn": result.turn.model_dump(mode="json"),
        "status": result.status,
        "identity_candidate": result.identity_candidate,
        "verification_candidate": result.verification_candidate,
    }


def current_understanding(state) -> TurnResult | None:
    """Reject stale checkpoints; absence is supported during consumer migration."""
    envelope = state.get("semantic_result")
    if envelope is None:
        return None
    try:
        if (
            envelope["event_id"] != state["event_id"]
            or envelope["text_digest"] != hashlib.sha256(state["text"].encode()).hexdigest()
            or envelope["status"] not in {"classified", "mock", "model_error"}
        ):
            raise ValueError("Semantic result does not belong to this turn")
        turn = TurnUnderstanding.model_validate(envelope["turn"])
        tokens = re.findall(
            r"[0-9]+",
            re.sub(r"https?://\S+|www\.\S+|[\w.+-]+@[\w.-]+", "[redacted]", state["text"]),
        )
        for field, index, role in (
            ("identity_candidate", turn.identity_token, "identity"),
            ("verification_candidate", turn.code_token, "verification_code"),
        ):
            candidate = envelope.get(field)
            if candidate is not None and (
                turn.intent != role
                or index is None
                or index >= len(tokens)
                or candidate != tokens[index]
                or (role == "verification_code" and len(candidate) != 4)
            ):
                raise ValueError("Candidate is not grounded in this message")
        return TurnResult(
            turn,
            envelope["status"],
            envelope.get("identity_candidate"),
            envelope.get("verification_candidate"),
        )
    except (KeyError, TypeError, ValueError):
        return TurnResult(TurnUnderstanding(), "model_error")
