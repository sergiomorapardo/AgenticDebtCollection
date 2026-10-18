# Arquitectura de la etapa por texto

## Orquestación

Un StateGraph organiza intake → interpret → rama de negocio → response → END.
Intake recupera estado desde SQLite, limita el contacto inicial y verifica identidad ficticia.
Intent enruta a promise, negotiation, confirmation, handoff, close o response.
Confirmation admite commitment solo con una oferta presentada y vigente.
Cada invocación procesa un turno y termina; nunca espera dentro del proceso a que hable el usuario.

El modelo extrae intención y términos, sin decidir elegibilidad ni calcular calendarios.
El proveedor mock permite pruebas sin red; Anthropic usa Haiku y salida estructurada.
Las respuestas financieras son plantillas deterministas para mantener cifras/fechas verificables.
No se utiliza un supervisor multiagente ni un evaluador LLM en el camino de cada turno.

## Persistencia

SQLite conserva cuentas, sesiones, ofertas, contactos, acuerdos y eventos. Los saldos provienen
siempre del repositorio, no del historial. Cada oferta identifica términos, versiones y política.
Los acuerdos son promesas: no reducen saldo ni acreditan pagos. No hay pasarela de pagos.

El nodo commitment solicita la escritura. Response revalida y guarda acuerdo, estado y resultado
del evento en una sola transacción antes de entregar la respuesta, también al invocar desde Studio.
ConversationService serializa el turno completo para simplificar la demo; Studio usa versiones
optimistas de sesión y transacciones al finalizar. Staging necesitará concurrencia por sesión.
La clave (session_id,event_id) identifica reintentos; cambia el contenido con esa clave y falla.

Un cambio de saldo/política invalida la oferta. Una respuesta ambigua después de una oferta la
invalida y exige una nueva presentación. Se limitan turnos, intentos de identidad y propuestas.
Una caída de persistencia revierte escrituras y no entrega un mensaje de registro exitoso.
Un error del proveedor devuelve una respuesta neutral sin cambiar a otro proveedor/modelo.

La entrada validada TurnRequest admite session_id, event_id, account_id y text. No permite
identity_verified, saldo ni agreement_id. Las sesiones/identidades siguen siendo de desarrollo:
conocer un session_id no constituye autenticación. Nunca exponer esta interfaz a clientes reales.

## Colombia y política ficticia

Canal sintético phone_simulation autorizado mediante fixture. Reloj explícito America/Bogota.
Controles implementados: lunes-viernes 07:00–19:00 y sábado 08:00–15:00 (fin excluido), domingos
 y festivos bloqueados; contacto establecido máximo una vez por día, y sin varios canales en la
misma semana. No se pregunta por el motivo del incumplimiento; se consultan alternativas.
No se implementan excepciones horarias: cualquier caso fuera de la regla general se bloquea.
Referencia: https://gestornormativo.creg.gov.co/gestor/entorno/docs/ley_2300_2023.htm
Estos controles parciales no sustituyen revisión normativa/de datos previa a un piloto real.

Política demo-co-v1: saldo fijo, primera fecha entre hoy y siete días, hasta tres cuotas mensuales,
sin descuentos/cargos. Se usan COP enteros, se distribuyen restos y se ajusta fin de mes. Los
importes, fechas y condición de simulación siempre se presentan antes de pedir confirmación.

## Evolución del canal

Gradio consumirá el mismo workflow y session_id. Transcripción y TTS quedarán fuera del motor
financiero. Primero conversación por turnos, después streaming, detección de pausas e interrupciones.
Generar una oferta no equivaldrá a haberla reproducido; el canal reportará qué audio se escuchó.
Esta funcionalidad de voz y los proveedores STT/TTS aún no están implementados.

El siguiente despliegue será staging con simulador web antes de conectar Amazon Connect/Lex.
No hay AWS provisionado ni llamadas, mensajes o contactos a terceros en este repositorio.
