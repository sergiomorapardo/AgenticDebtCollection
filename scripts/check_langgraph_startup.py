"""Start Agent Server and verify Studio-style turns without credentials or model calls."""

import argparse
import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]


def check_startup(config_path: Path, timeout: float) -> None:
    config = json.loads(config_path.read_text())
    expected = set(config["graphs"])
    if not expected:
        raise RuntimeError("No graphs are registered in the configuration.")

    # Keep the real registrations, resolving their paths before using a temporary cwd.
    for name, spec in config["graphs"].items():
        path, variable = spec.rsplit(":", 1)
        if path.endswith(".py"):
            config["graphs"][name] = f"{(ROOT / path).resolve()}:{variable}"
    config["dependencies"] = [
        str((ROOT / dependency).resolve()) if dependency.startswith(".") else dependency
        for dependency in config.get("dependencies", [])
    ]

    # Do not read developer .env files or send traces using inherited credentials.
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(
            ("LANGSMITH_", "LANGCHAIN_", "LANGGRAPH_", "ANTHROPIC_", "OPENAI_", "COLLECTIONS_")
        )
    }
    config["env"] = {
        "COLLECTIONS_LLM_PROVIDER": "mock",
        "COLLECTIONS_MODE": "simulation",
        "COLLECTIONS_DEMO_NOW": "2026-10-19T09:00:00-05:00",
        "LANGSMITH_TRACING": "false",
        "LANGCHAIN_TRACING_V2": "false",
        "LANGGRAPH_CLI_NO_ANALYTICS": "1",
        "PYTHON_DOTENV_DISABLED": "1",
    }
    env.update(config["env"])

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    base_url = f"http://127.0.0.1:{port}"
    client = build_opener(ProxyHandler({}))

    with tempfile.TemporaryDirectory(prefix="langgraph-startup-") as directory:
        temporary = Path(directory)
        config["env"]["COLLECTIONS_DATABASE_PATH"] = str(temporary / "collections.sqlite3")
        temporary_config = temporary / "langgraph.json"
        temporary_config.write_text(json.dumps(config))
        log_path = temporary / "server.log"
        with log_path.open("w") as log:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "langgraph_cli",
                    "dev",
                    "--config",
                    str(temporary_config),
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "--no-browser",
                    "--no-reload",
                ],
                cwd=temporary,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError(f"LangGraph exited with code {process.returncode}.")
                    try:
                        with client.open(f"{base_url}/ok", timeout=1) as response:
                            if response.status == 200:
                                break
                    except (URLError, TimeoutError):
                        pass
                    time.sleep(0.5)
                else:
                    raise RuntimeError(f"LangGraph was not ready within {timeout:g} seconds.")

                request = Request(
                    f"{base_url}/assistants/search",
                    data=json.dumps({"limit": 1000}).encode(),
                    headers={"Content-Type": "application/json"},
                )
                with client.open(request, timeout=10) as response:
                    assistants = json.load(response)
                    registered = {assistant["graph_id"] for assistant in assistants}
                missing = expected - registered
                if missing:
                    raise RuntimeError(f"Graphs missing from the server: {sorted(missing)}")
                print(f"LangGraph startup OK: {', '.join(sorted(expected))}")
                assistant_id = next(
                    a["assistant_id"] for a in assistants if a["graph_id"] == "collections_agent"
                )
                check_conversation(
                    client, base_url, temporary / "collections.sqlite3", assistant_id
                )
            except Exception:
                print(log_path.read_text(), file=sys.stderr)
                raise
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()


def check_conversation(client, base_url: str, database: Path, assistant_id: str) -> None:
    def request(path: str, payload=None):
        message = Request(
            base_url + path,
            data=None if payload is None else json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with client.open(message, timeout=30) as response:
            return json.load(response)

    schemas = request(f"/assistants/{assistant_id}/schemas")
    assert set(schemas["input_schema"]["properties"]) == {"messages"}, (
        "Studio must accept messages only"
    )
    assert "messages" in schemas["output_schema"]["properties"]

    def conversation(turns):
        thread = request("/threads", {})["thread_id"]
        configurable = {"thread_id": thread}
        results = []
        for index, (text, stage) in enumerate(turns, start=1):
            payload = {
                "assistant_id": assistant_id,
                "input": {
                    "messages": [
                        {
                            "type": "human",
                            "content": [{"type": "text", "text": text}],
                            "id": f"human-{index}",
                        }
                    ]
                },
                "config": {"configurable": dict(configurable)},
            }
            result = request(f"/threads/{thread}/runs/wait", payload)
            assert "__error__" not in result, result.get("__error__")
            if "987654321" in text:
                assert "No pude encontrar una cuenta" in result["reply"]["text"]
            if text == "1111" and stage == "awaiting_customer":
                assert "primero necesito tu número de identificación" in result["reply"]["text"]
            assert result["reply"]["stage"] == stage, f"Unexpected stage on turn {index}"
            assert result["messages"][-1]["type"] == "ai"
            assert result["messages"][-1]["content"] == result["reply"]["text"]
            assert not any(
                label in result["reply"]["text"]
                for label in ("Recuperé", "Resumen de las", "Estado:", "Cliente:", "Agente:")
            ), "Internal history was exposed in a customer message"
            results.append(result)
            print(f"Studio human turn {index}: {stage}")
            checkpoint = request(f"/threads/{thread}/state")["checkpoint"]
            configurable["thread_ts"] = checkpoint["checkpoint_id"]
        replay = request(f"/threads/{thread}/runs/wait", payload)
        assert replay["reply"] == result["reply"], "Replay changed the saved reply"
        assert replay["messages"] == result["messages"], "Replay duplicated messages"
        return results

    onboarding = conversation(
        [
            ("que hay", "awaiting_customer"),
            ("no quiero hacer ni mierda", "awaiting_customer"),
            ("que no quiero hacer nada", "awaiting_customer"),
            ("les voy a opagar", "awaiting_customer"),
            ("1000000001", "awaiting_identity"),
            ("por qué necesitan verificar", "awaiting_identity"),
            ("No me llames más, ¿entendido?", "closed"),
        ]
    )
    assert len({item["reply"]["text"] for item in onboarding[:4]}) == 4
    assert "No insistiré" in onboarding[1]["reply"]["text"]
    assert "pausa" in onboarding[2]["reply"]["text"]
    assert all("600,000" not in item["reply"]["text"] for item in onboarding)

    completed = conversation(
        [
            ("Hola", "awaiting_customer"),
            ("987654321", "awaiting_customer"),
            ("1111", "awaiting_customer"),
            ("ya te lo pase, era 987654321", "awaiting_customer"),
            ("Mi cédula es 123456789", "awaiting_identity"),
            ("verificar 1111", "ready"),
            ("quiero 3 cuotas desde 2026-10-20", "awaiting_confirmation"),
            ("confirmo", "agreed"),
        ]
    )[-1]
    agreement_id = completed["reply"]["agreement_id"]
    recalled = conversation(
        [
            ("Hola", "awaiting_customer"),
            ("1000000001", "awaiting_identity"),
            ("verificar 0000", "awaiting_identity"),
            ("verificar 1111", "agreed"),
        ]
    )
    assert all(agreement_id not in str(turn) for turn in recalled[:-1])
    assert agreement_id in recalled[-1]["messages"][-1]["content"]
    assert "2026-12-20" in recalled[-1]["messages"][-1]["content"]
    other = conversation(
        [
            ("1000000002", "awaiting_identity"),
            ("verificar 2222", "ready"),
        ]
    )
    assert agreement_id not in str(other)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM agreements").fetchone()[0] == 1
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM contacts WHERE account_id='demo-001'"
            ).fetchone()[0]
            == 1
        )
    # Reproduce the historical closure on the second synthetic account, then resume
    # through a fresh verified Studio thread with natural language instead of a command.
    from agents.collections.conversation import LEGACY_CLOSE

    with sqlite3.connect(database) as connection:
        session_id, raw = connection.execute(
            "SELECT id,data FROM sessions WHERE json_extract(data,'$.account_id')='demo-002' ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        session = json.loads(raw)
        session.update(stage="closed", pending_offer_id=None)
        session["history"].append(
            {
                "event_id": "legacy-smoke",
                "user": "ayudame a programar en python",
                "assistant": LEGACY_CLOSE,
            }
        )
        connection.execute(
            "UPDATE sessions SET data=? WHERE id=?", (json.dumps(session), session_id)
        )
    resumed = conversation(
        [
            ("1000000002", "awaiting_identity"),
            ("2222", "closed"),
            ("sigue", "ready"),
        ]
    )
    assert "dime continuemos" not in resumed[1]["reply"]["text"]
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM agreements").fetchone()[0] == 1
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM contacts WHERE account_id='demo-002'"
            ).fetchone()[0]
            == 1
        )
    print("Studio chat, replay, verified recall, customer isolation, and natural continuation OK")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "langgraph.json")
    parser.add_argument("--timeout", type=float, default=90)
    args = parser.parse_args()
    check_startup(args.config, args.timeout)
