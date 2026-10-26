"""Exercise the evaluation runner offline: checks, pricing, budget and paid-run guard."""

import importlib.util
import json
import os
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

    for name in ("connect", "connect_ex"):
        monkeypatch.setattr(socket.socket, name, no_network)
    monkeypatch.setattr(socket, "getaddrinfo", no_network)
    for key in list(os.environ):
        if key.startswith(("ANTHROPIC_", "LANGSMITH_", "LANGCHAIN_", "ELEVENLABS_")):
            monkeypatch.delenv(key)
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")
    spec = importlib.util.spec_from_file_location("eval_run", ROOT / "eval/run.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def scenarios():
    return json.loads((ROOT / "eval/scenarios.json").read_text())


def test_mock_evaluation_meets_every_scripted_expectation(runner):
    expected = [s["id"] for s in scenarios() if not s.get("skip")]
    report = runner.evaluate(scenarios(), provider="mock", model=None, max_usd=1.0)
    failed = {r["id"]: r["failures"] for r in report["scenarios"] if r["status"] == "fail"}
    assert not failed
    assert report["evaluated"] == report["passed"] == len(expected)
    assert report["usd"] == report["input_tokens"] == report["output_tokens"] == 0
    assert all("seconds" not in r for r in report["scenarios"])
    assert "| `audio-interruption` | skipped" in runner.markdown(report)


def test_every_scenario_has_expectations(runner, tmp_path):
    repository = SQLiteRepository(tmp_path / "empty.sqlite3")
    for scenario in scenarios():
        if not scenario.get("skip"):
            assert "no expectations defined" not in runner.check(scenario["id"], [], repository)
    assert runner.check("new-scenario", [], repository) == ["no expectations defined"]


def test_checks_report_missing_outcomes(runner, tmp_path):
    repository = SQLiteRepository(tmp_path / "empty.sqlite3")
    replies = [{"stage": "ready", "text": "El saldo registrado es 600,000 COP."}] * 4
    assert runner.check("full-payment", replies, repository) == [
        "exactly one agreement",
        "conversation ends",
    ]
    assert "simulated handoff" in runner.check("disputed-debt", replies, repository)
    assert runner.check("unverified-identity", replies, repository) == [
        "conversation ends",
        "no balance or offer",
    ]
    stale = [*replies[:3], *[{"stage": "awaiting_confirmation", "offer_id": "o"}] * 3]
    assert "ambiguous reply invalidates the offer" in runner.check(
        "unclear-acceptance", stale, repository
    )
    assert "confirmation requires a new proposal" in runner.check(
        "changed-balance", stale, repository
    )
    assert runner.check("three-installments", replies[:2], repository)[0] == (
        "offer presented first"
    )


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


def test_usage_is_captured_through_detached_internal_calls(runner):
    from langchain_core.callbacks import get_usage_metadata_callback
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage

    from agents.collections.streaming import internal_model_call

    message = AIMessage(
        content="ok",
        usage_metadata={"input_tokens": 1000, "output_tokens": 100, "total_tokens": 1100},
        response_metadata={"model_name": "claude-haiku-4-5-20251001"},
    )
    model = FakeMessagesListChatModel(responses=[message, message], callbacks=[])
    with get_usage_metadata_callback() as usage:
        internal_model_call(model, [("human", "x")])
        list(model.stream([("human", "x")]))
    assert usage.usage_metadata["claude-haiku-4-5-20251001"]["input_tokens"] == 2000
    assert runner.usd(usage.usage_metadata) == pytest.approx(0.003)


def test_budget_stops_after_the_scenario_that_reaches_it(runner, monkeypatch):
    ran = []

    def priced(scenario, **kwargs):
        ran.append((scenario["id"], kwargs["budget_usd"]))
        return {"id": scenario["id"], "status": "pass", "usd": 0.6}

    monkeypatch.setattr(runner, "run_scenario", priced)
    monkeypatch.setattr(runner, "summarize", lambda results, **kwargs: results)
    progress = []
    results = runner.evaluate(
        scenarios(), provider="mock", model=None, max_usd=1.0, on_progress=progress.append
    )
    assert [budget for _, budget in ran] == [1.0, pytest.approx(0.4)]
    runnable = [s for s in scenarios() if not s.get("skip")]
    assert [r["id"] for r in results if r["status"] == "pass"] == [s["id"] for s in runnable[:2]]
    assert sum(r["status"] == "not_run" for r in results) == len(runnable) - 2
    assert len(progress) == len(scenarios())


def test_mock_refuses_to_run_with_an_api_key(runner, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    with pytest.raises(RuntimeError, match="Unset ANTHROPIC_API_KEY"):
        runner.evaluate(scenarios(), provider="mock", model=None, max_usd=1.0)


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--provider", "anthropic"], "confirm-spend"),
        (["--provider", "anthropic", "--confirm-spend"], "ANTHROPIC_API_KEY"),
    ],
)
def test_paid_run_requires_confirmation_and_a_key(runner, monkeypatch, tmp_path, argv, message):
    monkeypatch.setattr(runner, "ROOT", tmp_path)  # No local .env can supply a key.
    monkeypatch.setattr(sys, "argv", ["run.py", *argv, "--output", str(tmp_path / "x.json")])
    with pytest.raises(SystemExit, match=message):
        runner.main()
    assert not (tmp_path / "x.json").exists()


def test_unpriced_usage_stops_the_scenario_and_keeps_raw_usage(runner, monkeypatch):
    unknown = {"unknown-model": {"input_tokens": 5, "output_tokens": 1, "total_tokens": 6}}

    class Usage:
        usage_metadata = unknown

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr("langchain_core.callbacks.get_usage_metadata_callback", Usage)
    full_payment = next(s for s in scenarios() if s["id"] == "full-payment")
    result = runner.run_scenario(full_payment, provider="mock", model=None, budget_usd=1.0)
    assert result["failures"] == ["stopped after turn 1: unpriced model"]
    assert result["usd"] is None and result["usage_by_model"] == unknown
    assert result["turns"] == 1


@pytest.mark.parametrize("keep_anthropic", [False, True])
def test_external_services_are_disabled_before_a_run(runner, monkeypatch, keep_anthropic):
    for key in (
        "LANGCHAIN_API_KEY",
        "LANGSMITH_PROJECT",
        "ELEVENLABS_API_KEY",
        "ANTHROPIC_API_KEY",
    ):
        monkeypatch.setenv(key, "test-value")
    runner.disable_external_services(keep_anthropic=keep_anthropic)
    remaining = {k for k in os.environ if k.startswith(("LANGCHAIN_", "LANGSMITH_", "ELEVENLABS_"))}
    assert remaining == {"LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2"}
    assert os.environ["LANGSMITH_TRACING"] == os.environ["LANGCHAIN_TRACING_V2"] == "false"
    assert ("ANTHROPIC_API_KEY" in os.environ) is keep_anthropic


def test_budget_crossed_on_the_last_turn_still_checks_the_outcome(runner, monkeypatch):
    full_payment = next(s for s in scenarios() if s["id"] == "full-payment")
    reads = []

    class Usage:
        @property
        def usage_metadata(self):
            reads.append(1)
            # One read per non-final turn, then the final snapshot: only that one is expensive.
            tokens = 10_000_000 if len(reads) >= len(full_payment["turns"]) else 0
            return {"claude-haiku-4-5-20251001": {"input_tokens": tokens, "output_tokens": 0}}

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr("langchain_core.callbacks.get_usage_metadata_callback", Usage)
    result = runner.run_scenario(full_payment, provider="mock", model=None, budget_usd=1.0)
    assert result["status"] == "pass" and result["turns"] == len(full_payment["turns"])
    assert result["usd"] == pytest.approx(10.0)
