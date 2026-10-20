"""Keep the README demo aligned with the offline Studio happy path."""

import importlib.util
import os
import socket
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_readme_demo_matches_a_fresh_offline_render(monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("The demo renderer must not use the network")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    for key in list(os.environ):
        if key.startswith(("ANTHROPIC_", "LANGSMITH_", "LANGCHAIN_", "ELEVENLABS_")):
            monkeypatch.delenv(key)
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    spec = importlib.util.spec_from_file_location("render_demo", ROOT / "scripts/render_demo.py")
    render_demo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(render_demo)
    expected = render_demo.render(render_demo.run_happy_path())
    assert (ROOT / "docs/images/demo.svg").read_text() == expected, (
        "Regenerate with: uv run python scripts/render_demo.py"
    )
