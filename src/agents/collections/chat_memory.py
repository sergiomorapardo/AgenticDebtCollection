"""Backend-owned Studio identities, replay records, and bounded customer memory."""

import json

from agents.collections.memory import ConflictError


class ChatMemory:
    def __init__(self, repository):
        self.repository = repository
        with repository.connection() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS studio_threads (id TEXT PRIMARY KEY, data TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS studio_events (thread_id TEXT NOT NULL, event_id TEXT NOT NULL, digest TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY(thread_id,event_id))"
            )

    def binding(self, thread_id):
        with self.repository.connection() as db:
            row = db.execute("SELECT data FROM studio_threads WHERE id=?", (thread_id,)).fetchone()
        return (
            json.loads(row["data"])
            if row
            else {
                "account_id": None,
                "verified": False,
                "attempts": 0,
                "closed": False,
                "session_id": None,
            }
        )

    def save_binding(self, thread_id, binding):
        with self.repository.connection() as db:
            db.execute(
                "INSERT INTO studio_threads VALUES (?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                (thread_id, json.dumps(binding)),
            )

    def replay(self, thread_id, event_id, digest):
        with self.repository.connection() as db:
            row = db.execute(
                "SELECT digest,data FROM studio_events WHERE thread_id=? AND event_id=?",
                (thread_id, event_id),
            ).fetchone()
        if row is None:
            return None
        if row["digest"] != digest:
            raise ConflictError(
                "This message ID already belongs to different content. Send a new message."
            )
        return json.loads(row["data"])

    def save_reply(self, thread_id, event_id, digest, reply):
        with self.repository.connection() as db:
            existing = self.replay(thread_id, event_id, digest)
            if existing is None:
                db.execute(
                    "INSERT INTO studio_events VALUES (?,?,?,?)",
                    (thread_id, event_id, digest, json.dumps(reply)),
                )
            return existing or reply

    def save_transition(self, state, binding, response):
        """Commit identity changes and their replay response in one transaction."""
        with self.repository.connection():
            cached = self.replay(state["chat_thread"], state["chat_event"], state["chat_digest"])
            if cached is not None:
                return cached
            self.save_binding(state["chat_thread"], binding)
            return self.save_reply(
                state["chat_thread"], state["chat_event"], state["chat_digest"], response
            )

    def customer_history(self, account_id):
        # Query by customer, never by a user-supplied thread or session identifier.
        with self.repository.connection() as db:
            count = db.execute(
                "SELECT COUNT(*) FROM sessions WHERE json_extract(data,'$.account_id')=? AND json_extract(data,'$.identity_verified')=1",
                (account_id,),
            ).fetchone()[0]
            rows = db.execute(
                "SELECT id,data FROM sessions WHERE json_extract(data,'$.account_id')=? AND json_extract(data,'$.identity_verified')=1 ORDER BY rowid DESC LIMIT 3",
                (account_id,),
            ).fetchall()
            agreements = db.execute(
                "SELECT id,data FROM agreements WHERE json_extract(data,'$.offer.account_id')=? ORDER BY rowid DESC LIMIT 3",
                (account_id,),
            ).fetchall()
        return {
            "count": count,
            "sessions": [{"session_id": r["id"], **json.loads(r["data"])} for r in rows],
            "agreements": [{"agreement_id": r["id"], **json.loads(r["data"])} for r in agreements],
        }


def summarize_history(history):
    """Extract facts and short excerpts; original records remain authoritative."""
    if not history["count"]:
        return "No encontré conversaciones anteriores verificadas sobre tu cuenta."
    lines = [
        f"Recuperé {history['count']} conversación(es) anterior(es). Resumen de las últimas tres:"
    ]
    stages = {
        "ready": "lista para conversar",
        "agreed": "compromiso registrado",
        "closed": "finalizada",
        "handoff": "revisión humana",
        "awaiting_confirmation": "propuesta pendiente de confirmación",
        "awaiting_terms": "faltan términos",
    }
    for session in history["sessions"]:
        lines.append(f"- Estado: {stages.get(session['stage'], session['stage'])}.")
        # Skip onboarding turns, which can contain synthetic verification codes.
        for turn in session.get("history", [])[2:][-2:]:
            lines.append(f"  Cliente: {turn['user'][:180]} | Agente: {turn['assistant'][:240]}")
    for agreement in history["agreements"]:
        terms = "; ".join(
            f"{p['amount_cop']:,} COP el {p['due_date']}"
            for p in agreement["offer"]["installments"]
        )
        lines.append(
            f"- Compromiso {agreement['agreement_id']}: {terms}. Promesa registrada; pago no verificado."
        )
    return "\n".join(lines)


def customer_history_reply(history, repository):
    """Expose authoritative account facts, never internal summaries or transcript excerpts."""
    from agents.collections.conversation import terminal_response

    session = history["sessions"][0]
    commitments = []
    for agreement in history["agreements"]:
        terms = "; ".join(
            f"{p['amount_cop']:,} COP el {p['due_date']}"
            for p in agreement["offer"]["installments"]
        )
        commitments.append(
            f"Compromiso registrado con referencia {agreement['agreement_id']}: {terms}. "
            "Es una promesa de pago; pago no verificado. El saldo continúa pendiente."
        )
    prefix = "\n".join(commitments)
    if session["stage"] in {"agreed", "closed", "handoff"}:
        if session["stage"] == "agreed" and commitments:
            return prefix
        return "\n".join(filter(None, [prefix, terminal_response(session, "")]))
    if session.get("pending_offer_id") and session["stage"] == "awaiting_confirmation":
        offer = repository.get_offer(session["pending_offer_id"], session["session_id"])
        terms = "; ".join(f"{p.amount_cop:,} COP el {p.due_date}" for p in offer.installments)
        proposal = f"Propuesta pendiente: {terms}. ¿Confirmas estas fechas e importes?"
        return "\n".join(filter(None, [prefix, proposal]))
    if commitments:
        return prefix + "\n¿Qué duda tienes sobre estos compromisos?"
    account = repository.get_account(session["account_id"])
    return (
        f"El saldo registrado es {account.balance_cop:,} COP. "
        "¿Prefieres revisar el pago total o un plan de cuotas?"
    )
