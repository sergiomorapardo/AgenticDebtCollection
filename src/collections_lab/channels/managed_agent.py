"""Text channel over a Claude Managed Agents session; the local backend answers every tool."""

import argparse
import hmac
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from agents.collections.config import data_directory
from agents.collections.domain import Offer
from agents.collections.memory import ConflictError, SQLiteRepository, event_hash
from agents.collections.policy import build_offer, load_policy
from collections_lab.config.settings import load_settings

LOGGER = logging.getLogger(__name__)
MAX_IDENTITY_ATTEMPTS = 3
DEFAULT_BUDGET_CENTS = "100"


@dataclass
class ManagedConversation:
    """Business state of one session. The model sends values; this class decides."""

    repository: SQLiteRepository
    now: datetime
    session_id: str
    reviews_path: Path
    account_id: str | None = None
    failed_attempts: int = 0
    closed: bool = False
    turn: int = 0
    last_customer_text: str = ""
    offer: Offer | None = None
    offer_turn: int = -1
    revision: int = 0
    session_data: dict = field(default_factory=dict)

    @property
    def today(self) -> date:
        return self.now.date()

    def customer_message(self, text: str) -> None:
        self.turn += 1
        self.last_customer_text = text

    def handle(self, name: str, tool_input: dict, event_id: str) -> dict:
        handlers = {
            "collections_verify_identity": self._verify_identity,
            "collections_get_account_summary": self._account_summary,
            "collections_quote_plan": self._quote_plan,
            "collections_record_commitment": self._record_commitment,
            "collections_request_human_review": self._request_review,
        }
        if name not in handlers:
            return {"error": f"Unknown tool {name}."}
        if self.closed:
            return {"error": "This conversation is closed. Tell the customer you cannot continue."}
        try:
            return handlers[name](tool_input, event_id)
        except (ValueError, ConflictError) as exc:
            return {"error": str(exc)}
        except Exception:
            # The session waits for a result to every call. Tool input holds the document
            # and code, so only the tool name is logged.
            LOGGER.exception("Tool %s failed", name)
            return {"error": "Internal backend error. Tell the customer to try again later."}

    def _persist(self, event_id: str, draft_offer: Offer | None = None) -> None:
        state = {
            "session_id": self.session_id,
            "event_id": event_id,
            "input_hash": event_hash(self.account_id or "", event_id),
            "session": {**self.session_data, "revision": self.revision},
        }
        if draft_offer is not None:
            state["draft_offer"] = draft_offer.model_dump(mode="json")
        self.repository.save_turn(state, {"channel": "managed_agent"})
        self.revision += 1

    def _verify_identity(self, tool_input: dict, event_id: str) -> dict:
        if self.account_id:
            return {"error": "Identity is already verified. Do not verify again."}
        fixtures = data_directory() / "fixtures"
        customers = json.loads((fixtures / "customers.json").read_text())
        codes = json.loads((fixtures / "identity.json").read_text())
        account_id = customers.get(str(tool_input.get("document_id", "")).strip())
        expected = codes.get(account_id, "") if account_id else ""
        given = str(tool_input.get("code", "")).strip()
        # Same answer for an unknown document and a wrong code: no account enumeration.
        if not expected or not hmac.compare_digest(expected.encode(), given.encode()):
            self.failed_attempts += 1
            if self.failed_attempts >= MAX_IDENTITY_ATTEMPTS:
                self.closed = True
                return {"verified": False, "closed": True}
            return {
                "verified": False,
                "attempts_left": MAX_IDENTITY_ATTEMPTS - self.failed_attempts,
            }
        self.account_id = account_id
        self.session_data = {
            "account_id": account_id,
            "identity_verified": True,
            "stage": "verified",
            "contact_started_at": self.now.isoformat(),
        }
        try:
            self._persist(event_id)
        except ConflictError as exc:
            self.account_id, self.session_data, self.closed = None, {}, True
            return {"verified": False, "closed": True, "reason": str(exc)}
        account = self.repository.get_account(account_id)
        return {"verified": True, "customer_label": account.customer_label}

    def _require_identity(self) -> str:
        if not self.account_id:
            raise ValueError("Identity is not verified. Verify identity first.")
        return self.account_id

    def _account_summary(self, tool_input: dict, event_id: str) -> dict:
        account = self.repository.get_account(self._require_identity())
        policy = load_policy()
        saved = [
            {"agreement_id": item["agreement_id"], "installments": item["offer"]["installments"]}
            for item in self.repository.agreements()
            if item["offer"]["account_id"] == account.account_id
        ]
        return {
            "balance_cop": account.balance_cop,
            "original_due_date": account.due_date.isoformat(),
            "today": self.today.isoformat(),
            "policy": {
                "max_installments": policy.max_installments,
                "latest_first_payment_date": (
                    self.today + timedelta(days=policy.first_payment_within_days)
                ).isoformat(),
                "installment_frequency": policy.installment_frequency,
                "discounts_allowed": policy.discounts_allowed,
            },
            "saved_agreements": saved,
        }

    def _quote_plan(self, tool_input: dict, event_id: str) -> dict:
        account = self.repository.get_account(self._require_identity())
        policy = load_policy()
        try:
            first = date.fromisoformat(str(tool_input.get("first_payment_date", "")))
        except ValueError:
            raise ValueError("first_payment_date must be YYYY-MM-DD.") from None
        try:
            offer = build_offer(
                account,
                policy,
                today=self.today,
                first_payment_date=first,
                installments=tool_input.get("installments"),
            )
        except ValueError:
            latest = self.today + timedelta(days=policy.first_payment_within_days)
            raise ValueError(
                f"Outside policy. Allowed: 1 to {policy.max_installments} installments, "
                f"first payment from {self.today.isoformat()} to {latest.isoformat()}."
            ) from None
        self.session_data |= {"pending_offer_id": offer.offer_id, "stage": "awaiting_confirmation"}
        self._persist(event_id, draft_offer=offer)
        self.offer, self.offer_turn = offer, self.turn
        return {
            "offer_id": offer.offer_id,
            "installments": [item.model_dump(mode="json") for item in offer.installments],
        }

    def _record_commitment(self, tool_input: dict, event_id: str) -> dict:
        self._require_identity()
        if self.offer is None or tool_input.get("offer_id") != self.offer.offer_id:
            raise ValueError("offer_id is not the most recent offer. Quote the plan again.")
        if self.offer_turn >= self.turn:
            raise ValueError(
                "The offer was quoted in this turn. Present it and wait for the customer's answer."
            )
        quote = str(tool_input.get("confirmation_quote", "")).strip()
        if quote != self.last_customer_text.strip():
            raise ValueError(
                "confirmation_quote is not the customer's latest message. Copy it exactly."
            )
        agreement_id = self.repository.save_confirmed_offer(
            session_id=self.session_id,
            offer_id=self.offer.offer_id,
            event_id=event_id,
            today=self.today,
        )
        return {
            "agreement_id": agreement_id,
            "installments": [item.model_dump(mode="json") for item in self.offer.installments],
            "paid": False,
        }

    def _request_review(self, tool_input: dict, event_id: str) -> dict:
        review_id = "review-" + uuid4().hex[:12]
        record = {
            "review_id": review_id,
            "session_id": self.session_id,
            "account_id": self.account_id,
            "reason": tool_input.get("reason"),
            "summary": re.sub(r"\d{4,}", "[redacted]", str(tool_input.get("summary", "")))[:1000],
            "created_at": self.now.isoformat(),
        }
        self.reviews_path.parent.mkdir(parents=True, exist_ok=True)
        with self.reviews_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return {"review_id": review_id}


def _lock_ids(lock_path: Path, agent_dir: str) -> tuple[str, str]:
    resources = json.loads(lock_path.read_text())["resources"]
    return (
        resources[f"./agents/{agent_dir}/agent.md"]["id"],
        resources[f"./agents/{agent_dir}/environment.yaml"]["id"],
    )


def run_chat(args: argparse.Namespace) -> int:
    import anthropic

    settings = load_settings(Path(".env") if Path(".env").exists() else None)
    agent_id, environment_id = _lock_ids(Path(args.lock), args.agent)
    database = Path(args.database or settings.database_path)
    client = anthropic.Anthropic()
    session = client.beta.sessions.create(
        agent=agent_id,
        environment_id=environment_id,
        title=f"chat {args.agent}",
        budget={"type": "limit", "max_list_cost": {"amount": args.budget_cents, "currency": "USD"}},
    )
    conversation = ManagedConversation(
        repository=SQLiteRepository(database),
        now=datetime.fromisoformat(args.at or settings.demo_now),
        session_id=session.id,
        reviews_path=database.with_suffix(".reviews.jsonl"),
    )
    print(
        f"Session {session.id}  https://platform.claude.com/workspaces/default/sessions/{session.id}"
    )
    print("Escriba como cliente. /salir termina.\n")
    with client.beta.sessions.events.stream(session_id=session.id) as stream:
        events = iter(stream)
        while True:
            try:
                text = input("cliente> ").strip()
            except EOFError:
                return 0
            if text == "/salir":
                return 0
            if not text:
                continue
            conversation.customer_message(text)
            client.beta.sessions.events.send(
                session_id=session.id,
                events=[{"type": "user.message", "content": [{"type": "text", "text": text}]}],
            )
            for event in events:
                if event.type == "agent.message":
                    print("agente> " + "".join(b.text for b in event.content if b.type == "text"))
                elif event.type == "agent.custom_tool_use":
                    result = conversation.handle(event.name, event.input or {}, event.id)
                    if args.verbose:
                        print(f"  [{event.name}] {json.dumps(result, ensure_ascii=False)}")
                    client.beta.sessions.events.send(
                        session_id=session.id,
                        events=[
                            {
                                "type": "user.custom_tool_result",
                                "custom_tool_use_id": event.id,
                                "content": [
                                    {"type": "text", "text": json.dumps(result, ensure_ascii=False)}
                                ],
                            }
                        ],
                    )
                elif event.type == "session.status_terminated":
                    return 1
                elif event.type == "session.status_idle":
                    reason = event.stop_reason.type
                    if reason == "requires_action":
                        continue
                    if reason != "end_turn":
                        print(f"[sesión detenida: {reason}]")
                        return 1
                    break
            if conversation.closed:
                print("[conversación cerrada por el backend]")
                return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", default="negociador-cobranza")
    parser.add_argument("--lock", default="claude-lock.json")
    parser.add_argument("--database", help="SQLite file; defaults to COLLECTIONS_DATABASE_PATH")
    parser.add_argument("--at", help="Demo clock, ISO 8601 with zone")
    parser.add_argument("--budget-cents", default=DEFAULT_BUDGET_CENTS)
    parser.add_argument("--verbose", action="store_true", help="Print every tool result")
    return run_chat(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
