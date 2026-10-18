# Pruebas locales

Ejecutar `uv run pytest -q`. test_policy.py cubre términos y aritmética; test_contact.py, horarios,
festivos y reglas de contacto; test_interpreter.py, interpretación conservadora; test_workflow.py,
recorridos completos, reintentos, aislamiento, rollback y concurrencia de persistencia.
Las pruebas de workflow bloquean red. La integración Anthropic no se prueba con llamadas reales.
