"""El servidor de Studio expone entradas simples, no dependencias de infraestructura."""

from agents.collections.studio import agent


def test_studio_schemas_do_not_expose_repository_or_trusted_state():
    schema = agent.get_input_jsonschema()
    assert set(schema["properties"]) == {"session_id", "event_id", "account_id", "text"}
    assert agent.get_context_jsonschema() is None
