"""Persistencia SQLite para la demo local; sin efectos externos ni pagos reales."""

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from threading import RLock, local
from uuid import uuid4

from agents.collections.config import data_directory


class ConflictError(ValueError):
    """Un reintento no corresponde al mismo evento o la sesión ha cambiado."""


class SQLiteRepository:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._local = local()
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS accounts (id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, revision INTEGER NOT NULL, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS offers (id TEXT NOT NULL, session_id TEXT NOT NULL, data TEXT NOT NULL, presented INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(id,session_id));
                CREATE TABLE IF NOT EXISTS agreements (id TEXT PRIMARY KEY, session_id TEXT NOT NULL UNIQUE, offer_id TEXT NOT NULL, event_id TEXT NOT NULL, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (session_id TEXT NOT NULL, event_id TEXT NOT NULL, input_hash TEXT NOT NULL, reply TEXT NOT NULL, PRIMARY KEY(session_id,event_id));
                CREATE TABLE IF NOT EXISTS contacts (session_id TEXT PRIMARY KEY, account_id TEXT NOT NULL, timestamp TEXT NOT NULL, channel TEXT NOT NULL, established INTEGER NOT NULL);
            """)
            accounts = json.loads((data_directory() / "fixtures/accounts.json").read_text())
            for account in accounts:
                db.execute(
                    "INSERT OR IGNORE INTO accounts VALUES (?,?)",
                    (account["account_id"], json.dumps(account)),
                )

    @contextmanager
    def connection(self):
        current = getattr(self._local, "connection", None)
        if current is not None:
            yield current
            return
        with self._lock:
            db = sqlite3.connect(self.path, timeout=30)
            db.row_factory = sqlite3.Row
            try:
                db.execute("BEGIN IMMEDIATE")
                self._local.connection = db
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise
            finally:
                self._local.connection = None
                db.close()

    def is_contact_authorized(self, account_id: str) -> bool:
        permissions = json.loads(
            (data_directory() / "fixtures/contact_permissions.json").read_text()
        )
        return "phone_simulation" in permissions.get(account_id, [])

    def get_account(self, account_id: str):
        from agents.collections.domain import Account

        with self.connection() as db:
            row = db.execute("SELECT data FROM accounts WHERE id=?", (account_id,)).fetchone()
        if row is None:
            raise ValueError("Cuenta ficticia desconocida.")
        return Account.model_validate_json(row["data"])

    def get_session(self, session_id: str) -> dict | None:
        with self.connection() as db:
            row = db.execute(
                "SELECT revision,data FROM sessions WHERE id=?", (session_id,)
            ).fetchone()
        if row is None:
            return None
        return {**json.loads(row["data"]), "revision": row["revision"]}

    def get_event(self, session_id: str, event_id: str, input_hash: str) -> dict | None:
        with self.connection() as db:
            row = db.execute(
                "SELECT input_hash,reply FROM events WHERE session_id=? AND event_id=?",
                (session_id, event_id),
            ).fetchone()
        if row is None:
            return None
        if row["input_hash"] != input_hash:
            raise ConflictError("El event_id ya existe con otro contenido.")
        return json.loads(row["reply"])

    def contacts_for(self, account_id: str) -> list[dict]:
        with self.connection() as db:
            rows = db.execute(
                "SELECT timestamp,channel,established FROM contacts WHERE account_id=?",
                (account_id,),
            ).fetchall()
        return [{**dict(row), "established": bool(row["established"])} for row in rows]

    def mark_contact(self, session_id: str, account_id: str, now: datetime):
        with self.connection() as db:
            db.execute(
                "INSERT OR IGNORE INTO contacts VALUES (?,?,?,?,?)",
                (session_id, account_id, now.isoformat(), "phone_simulation", 1),
            )

    def get_offer(self, offer_id: str, session_id: str):
        from agents.collections.domain import Offer

        with self.connection() as db:
            row = db.execute(
                "SELECT data,presented FROM offers WHERE id=? AND session_id=?",
                (offer_id, session_id),
            ).fetchone()
        if row is None or not row["presented"]:
            raise ValueError("No existe una oferta presentada para esta sesión.")
        return Offer.model_validate_json(row["data"])

    def save_turn(self, state: dict, reply: dict):
        session = dict(state["session"])
        revision = session.pop("revision", 0)
        with self.connection() as db:
            row = db.execute(
                "SELECT revision FROM sessions WHERE id=?", (state["session_id"],)
            ).fetchone()
            actual = row["revision"] if row else 0
            if revision != actual:
                raise ConflictError(
                    "La sesión cambió; vuelve a enviar el turno con un event_id nuevo."
                )
            if revision == 0 and session.get("contact_started_at"):
                from agents.collections.contact import check_contact

                now = datetime.fromisoformat(session["contact_started_at"])
                allowed, reason = check_contact(
                    now=now,
                    authorized=self.is_contact_authorized(session["account_id"]),
                    prior_contacts=self.contacts_for(session["account_id"]),
                )
                if not allowed:
                    raise ConflictError(f"El contacto ya no está permitido: {reason}")
                self.mark_contact(state["session_id"], session["account_id"], now)
            offer = state.get("draft_offer")
            if offer:
                db.execute(
                    "INSERT INTO offers VALUES (?,?,?,1) ON CONFLICT(id,session_id) DO UPDATE SET presented=1",
                    (offer["offer_id"], state["session_id"], json.dumps(offer)),
                )
            db.execute(
                "INSERT INTO sessions VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,data=excluded.data",
                (state["session_id"], revision + 1, json.dumps(session)),
            )
            db.execute(
                "INSERT INTO events VALUES (?,?,?,?)",
                (state["session_id"], state["event_id"], state["input_hash"], json.dumps(reply)),
            )

    def save_confirmed_offer(self, *, session_id: str, offer_id: str, event_id: str, today) -> str:
        from agents.collections.policy import load_policy, validate_offer

        with self.connection() as db:
            existing = db.execute(
                "SELECT id,offer_id FROM agreements WHERE session_id=?", (session_id,)
            ).fetchone()
            if existing:
                if existing["offer_id"] != offer_id:
                    raise ConflictError("Esta sesión ya tiene otro acuerdo.")
                return existing["id"]
            session = self.get_session(session_id)
            if (
                not session
                or not session.get("identity_verified")
                or session.get("pending_offer_id") != offer_id
                or session.get("stage") != "awaiting_confirmation"
            ):
                raise ConflictError(
                    "No hay una oferta vigente pendiente de aceptación en esta sesión."
                )
            offer = self.get_offer(offer_id, session_id)
            account = self.get_account(session["account_id"])
            validate_offer(offer, account, load_policy(), today=today)
            agreement_id = "agreement-" + uuid4().hex[:16]
            record = {
                "offer": offer.model_dump(mode="json"),
                "confirmation_event_id": event_id,
                "status": "promised",
                "paid": False,
            }
            db.execute(
                "INSERT INTO agreements VALUES (?,?,?,?,?)",
                (agreement_id, session_id, offer_id, event_id, json.dumps(record)),
            )
            return agreement_id

    def agreements(self) -> list[dict]:
        with self.connection() as db:
            rows = db.execute("SELECT id,session_id,data FROM agreements ORDER BY rowid").fetchall()
        return [
            {"agreement_id": row["id"], "session_id": row["session_id"], **json.loads(row["data"])}
            for row in rows
        ]


def event_hash(account_id: str, text: str) -> str:
    return hashlib.sha256(json.dumps([account_id, text], ensure_ascii=False).encode()).hexdigest()
