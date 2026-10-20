"""Conversation boundaries and narrowly scoped recovery of legacy false closures."""

import re
import unicodedata

LEGACY_CLOSE = (
    "Finalizo la simulación. No se ha registrado un nuevo compromiso ni realizado un pago."
)
# Closures that only stop collection; the customer may later ask to review options again.
RESUMABLE_CLOSE_REASONS = frozenset({"customer_request", "repeated_refusal"})


def normalize_text(text):
    return " ".join(
        "".join(
            c for c in unicodedata.normalize("NFD", text.lower()) if not unicodedata.combining(c)
        ).split()
    ).strip(" .,;:!¡¿?")


def requests_end(text):
    # Ignore quoted examples, but retain directives outside a question or quotation.
    text = re.sub(r""""[^"]*"|“[^”]*”|'[^']*'""", "", text)
    clauses = re.split(r"[,;.!?¿]+|\s+y\s+", normalize_text(text))
    directive = (
        r"(?:por favor\s+)?(?:terminar|salir|adios|hasta luego|cuelga|"
        r"(?:quiero|deseo) terminar(?: (?:la|esta) (?:conversacion|llamada|simulacion))?|"
        r"no (?:quiero|deseo) (?:continuar|seguir|hablar)(?: (?:con esta|en esta) (?:conversacion|llamada|simulacion))?|"
        r"no me (?:llames|llamen|contactes)(?: mas| de nuevo| otra vez)?|dejame en paz|"
        r"(?:termina|terminemos|finaliza|finalicemos|cierra) (?:esta |la )?(?:conversacion|llamada|simulacion)|"
        r"(?:puedes |podrias )?dejar de (?:llamarme|contactarme))"
        r"(?: por favor)?"
    )
    return any(re.fullmatch(directive, clause.strip()) for clause in clauses)


def explicit_refusal(text):
    return bool(
        re.search(
            r"\b(no quiero pagar|no voy a pagar|no pagare nada|me niego a pagar|no pienso pagar)\b",
            normalize_text(text),
        )
    )


def social_intent(text):
    """Recognize unambiguous demo examples; mixed financial requests go to interpretation."""
    text = normalize_text(text)
    if re.fullmatch(r"(?:que )?mal servicio|pesimo servicio|estoy molesto|esto no funciona", text):
        return "complaint"
    if re.fullmatch(
        r"que paso|por que (?:cerraste|terminaste)|por que cerraste la sesion|no entiendo|que puedes hacer",
        text,
    ):
        return "clarify_process"
    if re.fullmatch(
        r"(?:ayudame (?:a |con )?|quiero |puedes ayudarme a )?(?:programar(?: en)?|(?:aprender )?python|escribir codigo)(?: en)?(?: python)?",
        text,
    ):
        return "off_topic"
    return None


def recoverable_misclosure(session):
    """Only recognize the evidenced old bug; never reopen legitimate terminal outcomes."""
    if (
        session["stage"] != "closed"
        or not session.get("identity_verified")
        or session.get("agreement_id")
        or session.get("close_reason")
    ):
        return False
    history = session.get("history", [])
    for index, turn in enumerate(history):
        if turn["assistant"] == LEGACY_CLOSE and social_intent(turn["user"]) == "off_topic":
            return not any(
                requests_end(later["user"]) or explicit_refusal(later["user"])
                for later in history[index + 1 :]
            )
    return False


def continuation_available(session, intent=None):
    """Whether a channel should keep listening for a customer-led continuation request.

    This reopens nothing: intake still requires the customer's newly understood request.
    Agreements, handoffs, failed verification and other closures stay terminal.
    """
    if intent in {"end", "decline", "refuse"}:
        return False
    return (
        session["stage"] == "closed"
        and bool(session.get("identity_verified"))
        and not session.get("agreement_id")
        and (
            session.get("close_reason") in RESUMABLE_CLOSE_REASONS
            or recoverable_misclosure(session)
        )
    )


def terminal_response(session, text, *, intent=None):
    if recoverable_misclosure(session):
        if intent == "end":
            return "Entendido. Mantengo cerrada esta conversación; no retomaré la negociación."
        return (
            "Lamento la atención anterior. Puedo ayudarte a revisar las opciones de pago. "
            "¿Quieres que sigamos revisando las opciones de pago?"
        )
    complaint = intent == "complaint"
    prefix = "Lamento la mala experiencia. " if complaint else ""
    if not session.get("identity_verified"):
        return (
            prefix
            + "La verificación de esta sesión terminó; no puedo consultar información de la cuenta."
        )
    if session["stage"] == "agreed":
        return (
            prefix
            + "El compromiso ya quedó registrado. Eso no verifica un pago ni cambia el saldo; este mensaje no crea otro acuerdo."
        )
    if session["stage"] == "handoff":
        return (
            prefix
            + "Tu solicitud quedó registrada para revisión. No puedo transferirte a un asesor desde aquí; no se registró un pago ni continuaré negociando en esta conversación."
        )
    if session.get("close_reason") in {"customer_request", "repeated_refusal"}:
        if intent in {"end", "decline", "refuse"}:
            return "Entendido. No insistiré con el cobro ni registraré un nuevo acuerdo."
        if intent == "off_topic":
            return "Puedo ayudarte con dudas sobre tu cuenta; esa petición está fuera de mi alcance. No retomaré el cobro por cambiar de tema."
        return (
            prefix + "Dejé de insistir con el cobro porque me indicaste que no querías seguir. "
            "Podemos aclarar dudas o registrar que no estás de acuerdo con el saldo. "
            "Si decides revisar opciones de pago, podemos retomarlas aquí; todavía no hay un nuevo acuerdo."
        )
    return (
        prefix + "Esta gestión está cerrada y no puedo reiniciar la negociación aquí. "
        "Puedo aclarar el estado o registrar una solicitud de revisión, sin modificar el saldo ni crear otro compromiso."
    )
