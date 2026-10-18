# Cobranza Agent Lab

Agente de cobranza para una fintech ficticia en Colombia, con LangGraph y políticas deterministas.
**Implementado: primera etapa por texto, ejecutable localmente y en LangGraph Studio.**
La llamada simulada con micrófono/voz, AWS y la telefonía real pertenecen a etapas posteriores.

## Qué hace

- Verifica una identidad ficticia antes de revelar el saldo.
- Comprueba autorización de canal, horario, festivos de Colombia e historial de contacto.
- Recuerda la obligación y propone pago completo o hasta tres cuotas mensuales.
- Solicita confirmación explícita de importe y fechas; revalida saldo y política antes de guardar.
- Persiste sesiones, ofertas, acuerdos y eventos en SQLite. Los reintentos no duplican acuerdos.
- Deriva de forma simulada disputas, pagos reportados y solicitudes de asesor.
- Mantiene la distinción entre compromiso registrado y pago verificado.

El modo **mock** es un parser limitado y reproducible, sin llamadas de red. **anthropic** utiliza
Claude Haiku para extraer intención y términos mediante API. Los importes y las respuestas de
negocio se calculan/forman en código en ambos modos. No hay fallback automático de proveedor.
La integración de Haiku está implementada, pero no se ha ejecutado con credenciales reales.

## Inicio rápido

Requiere Python 3.12 y uv. Desde la raíz del checkout/worktree:

```bash
uv sync --locked
cp -n .env.example .env
uv run collections-lab chat --session prueba-001
```

El CLI muestra el **reloj ficticio fijo: 2026-10-19 09:00, America/Bogota**. Puedes cambiarlo
con `--at 2026-10-20T10:00:00-05:00`. No representa la fecha real del equipo.

Prueba esta conversación:

```text
verificar 1111
quiero 3 cuotas desde 2026-10-20
confirmo
```

El escenario demo-001 tiene saldo de 600.000 COP. Se proponen tres cuotas de 200.000;
el acuerdo se guarda después de confirmar. demo-002 tiene saldo de 450.000 y código ficticio 2222.
**Los códigos son datos de prueba, no un método de autenticación para clientes reales.**

También puedes decir `pago 600000 mañana`, `ya pagué`, `no reconozco la deuda`, `quiero un asesor`
o `terminar`. `/salir` sale del CLI conservando la sesión. Las fechas aisladas pueden completar
una propuesta pendiente; la interpretación offline no cubre lenguaje libre general.

```bash
uv run collections-lab agreements
uv run collections-lab fixtures
uv run collections-lab graph
```

La base se guarda en `.local/collections.sqlite3`, ignorada por Git. Al repetir la misma sesión,
se recupera su estado. Las sesiones finalizadas no se reabren. Una sesión nueva con la misma
cuenta el mismo día queda bloqueada por el historial. Para un escenario de prueba independiente,
usa otro archivo explícito, por ejemplo `--database .local/otro-escenario.sqlite3`.

## Turnos reproducibles

```bash
uv run collections-lab turn --database .local/turnos.sqlite3 --session ejemplo --event-id 1 --text ''
uv run collections-lab turn --database .local/turnos.sqlite3 --session ejemplo --event-id 2 --text 'verificar 1111'
uv run collections-lab turn --database .local/turnos.sqlite3 --session ejemplo --event-id 3 --text 'pago 600000 mañana'
uv run collections-lab turn --database .local/turnos.sqlite3 --session ejemplo --event-id 4 --text 'confirmo'
```

El mismo event_id con igual contenido devuelve el resultado guardado; con otro contenido falla.
La sesión queda vinculada a una cuenta. Una oferta ambigua o rechazada se invalida: un `sí`
posterior a una aclaración no confirma la oferta anterior; hay que presentar términos de nuevo.

## LangGraph Studio

```bash
uv run langgraph dev
```

Selecciona `collections_agent`. Usa el mismo session_id y account_id durante la conversación,
y un event_id nuevo en cada ejecución. Crea un thread de Studio por sesión de prueba. Ejemplo:

```json
{"session_id":"studio-001","account_id":"demo-002","event_id":"1","text":""}
```

Después cambia event_id a `2` y text a `verificar 2222`; continúa con la propuesta y confirmación.
Los controles se ejecutan en los nodos. Studio guarda sus checkpoints; SQLite es la fuente de
verdad de sesiones y acuerdos. No edites el estado interno como si fuera entrada del usuario.
Studio es una herramienta de desarrollo, no una interfaz pública de clientes.

## Haiku opcional

Configura tu propia ANTHROPIC_API_KEY en el .env local y ejecuta:

```bash
uv run collections-lab chat --database .local/haiku.sqlite3 --session haiku-001 --provider anthropic
```

Esto consume tokens. Modelo por defecto: `claude-haiku-4-5-20251001`. No se copian claves de otros
repositorios. LANGSMITH_TRACING=false por defecto; no se han generado trazas pagadas en validación.

## Organización

```text
src/agents/collections/
  agent.py, state.py, config.py, memory.py, runtime.py
  domain.py, policy.py, contact.py, interpreter.py
  nodes/<name>/      node.py, prompt.py, tools.py
  routes/<name>/     agent.py, state.py
  data/             fixtures/ y policies/
src/collections_lab/
  config/           configuración común
  channels/         entrada pública validada
  service.py        servicio local por turno
apps/voice_simulator/   etapa 2 pendiente
infra/                 etapa de despliegue pendiente
eval/                  escenarios y costos
```

El AGENTS.md inicial se copió literalmente de
AdvancedGenAI y fue el único archivo del primer commit. El desarrollo se realiza en worktrees.

## Validación

```bash
uv run pytest -q
uv run ruff check src tests
uv run ruff format --check src tests
uv lock --check
uv build
```

Las pruebas cubren reglas, interpretación, workflow, recuperación, transacciones, concurrencia
y reintentos sin llamar a APIs externas. Ver [arquitectura](docs/ARCHITECTURE.md) y
[etapas](docs/ROADMAP.md). Este entorno usa datos sintéticos y no acredita cumplimiento legal
completo ni está diseñado para exponerse a Internet. Staging requiere autenticación y aislamiento.
