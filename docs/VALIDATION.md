# Validación de la etapa por texto

- Pruebas unitarias de política, calendario de contacto e intérprete sin red.
- Pruebas de integración de workflow con SQLite, incluyendo reintentos, concurrencia,
  persistencia tras reinicio, saldo alterado, oferta ambigua, autorización y rollback.
- Importación y generación del esquema público de Studio sin exponer SQLiteRepository.
- Prueba real del servidor local `langgraph dev`: health, registro de collections_agent,
  esquemas y cuatro turnos por HTTP (inicio → verificar 1111 → tres cuotas → confirmo).
  Resultado: awaiting_identity → ready → awaiting_confirmation → agreed.
- Lint/formato, lock y construcción de wheel/sdist.

No se llamó a Anthropic, Bedrock, TTS, STT, teléfonos ni APIs de negocio. No se ha probado la UI
web de Studio mediante navegador; se validó su API local. El adaptador Haiku aún requiere una
prueba con credenciales y tokens. La voz y el despliegue AWS siguen pendientes.
