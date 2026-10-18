"""CLI para chat por texto, turnos reproducibles e inspección."""

import argparse
import json
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from agents.collections.config import data_directory
from agents.collections.memory import SQLiteRepository
from agents.collections.runtime import AppContext
from collections_lab.channels.contracts import TurnRequest
from collections_lab.config.settings import load_settings


def main() -> None:
    parser = argparse.ArgumentParser(description="Simulación de cobranza Colombia; sin telefonía")
    parser.add_argument("command", choices=["graph", "fixtures", "chat", "turn", "agreements"])
    parser.add_argument("--session", default="demo-session")
    parser.add_argument("--account", default="demo-001")
    parser.add_argument("--event-id", default=None)
    parser.add_argument("--text", default="")
    parser.add_argument("--database", type=Path)
    parser.add_argument("--at", help="Reloj simulado ISO 8601 con zona horaria")
    parser.add_argument("--provider", choices=["mock", "anthropic"])
    args = parser.parse_args()
    settings = load_settings(Path.cwd() / ".env")
    if args.command == "graph":
        from agents.collections.agent import agent

        print(agent.get_graph().draw_mermaid())
        return
    if args.command == "fixtures":
        from agents.collections.domain import Account
        from agents.collections.policy import load_policy

        accounts = [
            Account.model_validate(item)
            for item in json.loads((data_directory() / "fixtures/accounts.json").read_text())
        ]
        print(
            f"Fixtures válidos: {len(accounts)} cuentas ficticias; política {load_policy().policy_version}."
        )
        return
    now = datetime.fromisoformat(args.at or settings.demo_now)
    if now.tzinfo is None:
        parser.error("--at requiere zona horaria.")
    context = AppContext(
        SQLiteRepository(args.database or settings.database_path),
        now,
        args.provider or settings.llm_provider,
        settings.anthropic_model,
    )
    if args.command == "agreements":
        print(json.dumps(context.repository.agreements(), ensure_ascii=False, indent=2))
        return
    from collections_lab.service import ConversationService

    service = ConversationService(context)

    def send(text, event_id=None):
        return service.respond(
            TurnRequest(
                session_id=args.session,
                account_id=args.account,
                event_id=event_id or uuid4().hex,
                text=text,
            )
        )

    if args.command == "turn":
        print(json.dumps(send(args.text, args.event_id), ensure_ascii=False, indent=2))
        return
    print(f"SIMULACIÓN | reloj: {now.isoformat()} | proveedor: {context.provider}")
    print(
        "Código ficticio demo-001: 1111; demo-002: 2222. Usa verificar 1111. /salir sale del CLI."
    )
    if context.repository.get_session(args.session) is None:
        reply = send("")
        print("Agente:", reply["text"])
        if reply["finished"]:
            return
    else:
        print("Sesión recuperada. Puedes continuar; terminar cierra la gestión.")
    while True:
        try:
            text = input("Tú: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if text == "/salir":
            break
        if not text:
            continue
        reply = send(text)
        print("Agente:", reply["text"])
        if reply["finished"]:
            break


if __name__ == "__main__":
    main()
