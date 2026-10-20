"""Exercise the evaluation runner offline: checks, pricing, budget and paid-run guard."""

import importlib.util
import json
import socket
import sys
from pathlib import Path

import pytest

from agents.collections.memory import SQLiteRepository

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def runner(monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("Evaluation tests must not call paid services")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    spec = importlib.util.spec_from_file_location("eval_run", ROOT / "eval/run.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def scenarios():
    return json.loads((ROOT / "eval/scenarios.json").read_text())


def test_mock_evaluation_meets_every_scripted_expectation(runner):
    report = runner.evaluate(scenarios(), provider="mock", model=None, max_usd=1.0)
    failed = {r["id"]: r["failures"] for r in report["scenarios"] if r["status"] == "fail"}
    assert not failed
    assert report["evaluated"] == report["passed"] == 10
    assert report["usd"] == report["input_tokens"] == report["output_tokens"] == 0
    assert "| `audio-interruption` | skipped" in runner.markdown(report)


def test_checks_report_missing_outcomes(runner, tmp_path):
    repository = SQLiteRepository(tmp_path / "empty.sqlite3")
    replies = [{"stage": "ready", "text": "El saldo registrado es 600,000 COP."}] * 4
    assert runner.check("full-payment", replies, repository) == ["exactly one agreement"]
    assert "simulated handoff" in runner.check("disputed-debt", replies, repository)
    assert runner.check("unverified-identity", replies, repository) == [
        "conversation ends",
        "no balance or offer",
    ]


def test_cost_uses_haiku_prices_and_cache_multipliers(runner):
    usage = {
        "claude-haiku-4-5-20251001": {
            "input_tokens": 1_000_000,
            "output_tokens": 100_000,
            "input_token_details": {"cache_read": 400_000, "cache_creation": 100_000},
        }
    }
    assert runner.usd(usage) == pytest.approx(0.5 + 0.125 + 0.04 + 0.5)
    with pytest.raises(ValueError, match="No price"):
        runner.usd({"unknown-model": {"input_tokens": 1}})


def test_budget_stops_before_running_more_scenarios(runner):
    report = runner.evaluate(scenarios(), provider="mock", model=None, max_usd=0)
    assert {r["status"] for r in report["scenarios"]} == {"not_run", "skipped"}
    assert report["evaluated"] == 0


def test_paid_run_requires_explicit_confirmation(runner, monkeypatch, tmp_path):
    monkeypatch.setattr(
        sys, "argv", ["run.py", "--provider", "anthropic", "--output", str(tmp_path / "x.json")]
    )
    with pytest.raises(SystemExit, match="confirm-spend"):
        runner.main()
    assert not (tmp_path / "x.json").exists()
