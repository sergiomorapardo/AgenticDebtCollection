"""Keep the README demo aligned with the offline Studio happy path."""

import importlib.util
import os
import socket
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_readme_demo_matches_a_fresh_offline_render(monkeypatch):
    spec = importlib.util.spec_from_file_location("render_demo", ROOT / "scripts/render_demo.py")
    render_demo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(render_demo)
    # Apply the renderer's own offline setup through monkeypatch so nothing leaks.
    for name in ("connect", "connect_ex"):
        monkeypatch.setattr(socket.socket, name, render_demo.no_network)
    monkeypatch.setattr(socket, "getaddrinfo", render_demo.no_network)
    for key in list(os.environ):
        if key.startswith(render_demo.CREDENTIAL_PREFIXES):
            monkeypatch.delenv(key)
    for key, value in render_demo.PINNED_ENV.items():
        monkeypatch.setenv(key, value)
    expected = render_demo.render(render_demo.run_happy_path())
    with open(ROOT / "docs/images/demo.svg", newline="") as asset:
        assert asset.read() == expected, "Regenerate with: uv run python scripts/render_demo.py"
