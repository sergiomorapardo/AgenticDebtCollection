"""Run the scripted scenarios on the Studio graph and record outcome, tokens, cost and latency.

`mock` is offline. `anthropic` spends real money: it requires --confirm-spend and stops
once the --max-usd budget is reached. Tracing is always disabled.
"""

import argparse
import json
import os
import statistics
import sys
import tempfile
from collections import Counter
from datetime import datetime
from pathlib import Path
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_NOW = "2026-10-19T09:00:00-05:00"
# USD per million tokens (input, output); cache writes cost 1.25x input and reads 0.1x.
PRICES = {"claude-haiku-4-5-20251001": (1.00, 5.00)}


def usd(usage_by_model):
    total = 0.0
    for model, usage in usage_by_model.items():
        if model not in PRICES:
            raise ValueError(f"No price configured for {model}")
        price_in, price_out = PRICES[model]
        details = usage.get("input_token_details") or {}
        read = details.get("cache_read", 0)
        write = details.get("cache_creation", 0)
        fresh = usage.get("input_tokens", 0) - read - write
        total += (
            fresh * price_in
            + write * price_in * 1.25
            + read * price_in * 0.1
            + usage.get("output_tokens", 0) * price_out
        ) / 1_000_000
    return total


def check(scenario_id, replies, repository):
    """Return the failed expectations; replies are the customer-visible turn results."""
    agreements = repository.agreements()
    last = replies[-1] if replies else {}
    texts = " ".join(reply.get("text", "") for reply in replies)
    failures = []

    def expect(condition, message):
        if not condition:
            failures.append(message)

    if scenario_id == "full-payment":
        expect(len(agreements) == 1, "exactly one agreement")
        if agreements:
            installments = agreements[0]["offer"]["installments"]
            expect(
                [(p["amount_cop"], p["due_date"]) for p in installments]
                == [(600000, "2026-10-20")],
                "one COP 600,000 payment on 2026-10-20",
            )
            expect(agreements[0]["paid"] is False, "debt remains unpaid")
    elif scenario_id in {"three-installments", "duplicate-confirmation"}:
        expect(replies[3].get("stage") == "awaiting_confirmation", "offer presented first")
        expect(len(agreements) == 1, "exactly one agreement")
        if agreements:
            amounts = [p["amount_cop"] for p in agreements[0]["offer"]["installments"]]
            expect(amounts == [200000, 200000, 200000], "three COP 200,000 installments")
        if scenario_id == "duplicate-confirmation":
            expect(replies[-1] == replies[-2], "retried event returns the saved reply")
    elif scenario_id in {"outside-policy", "reported-payment", "disputed-debt"}:
        expect(last.get("stage") == "handoff", "simulated handoff")
        expect(not agreements, "no agreement")
    elif scenario_id == "unverified-identity":
        expect(last.get("finished") is True, "conversation ends")
        expect("600,000" not in texts and "Propuesta" not in texts, "no balance or offer")
    elif scenario_id == "unclear-acceptance":
        expect(not agreements, "no agreement from ambiguous or stale acceptance")
    elif scenario_id == "changed-balance":
        expect(not agreements, "no agreement after the balance changed")
    elif scenario_id == "contact-blocked":
        expect(last.get("stage") == "closed", "contact blocked")
        expect(not repository.contacts_for("demo-001"), "no contact recorded")
        expect("600,000" not in texts, "no balance disclosed")
    return failures


def run_scenario(scenario, *, provider, model):
    from langchain_core.callbacks import get_usage_metadata_callback
    from langchain_core.messages import HumanMessage

    from agents.collections.agent import build_graph
    from agents.collections.memory import SQLiteRepository
    from agents.collections.runtime import AppContext

    graph = build_graph(studio=True)
    replies, errors = [], []
    started = perf_counter()
    with tempfile.TemporaryDirectory() as directory, get_usage_metadata_callback() as usage:
        repository = SQLiteRepository(Path(directory) / "eval.sqlite3")
        ctx = AppContext(
            repository,
            datetime.fromisoformat(scenario.get("at", DEFAULT_NOW)),
            provider,
            model,
            stream_responses=provider == "anthropic",
        )
        change = scenario.get("balance_change")
        for index, turn in enumerate(scenario["turns"]):
            turn = turn if isinstance(turn, dict) else {"text": turn}
            if change and index == change["before_turn"]:
                account = repository.get_account("demo-001").model_dump(mode="json")
                account["balance_cop"] = change["balance_cop"]
                with repository.connection() as db:
                    db.execute(
                        "UPDATE accounts SET data=? WHERE id=?",
                        (json.dumps(account), "demo-001"),
                    )
            try:
                result = graph.invoke(
                    {
                        "messages": [
                            HumanMessage(
                                content=turn["text"],
                                id=f"{scenario['id']}-{turn.get('event', index)}",
                            )
                        ]
                    },
                    config={"configurable": {"thread_id": scenario["id"]}},
                    context=ctx,
                )
                replies.append(result["reply"])
            except Exception as error:  # noqa: BLE001 - record and continue the evaluation
                errors.append(f"turn {index + 1}: {type(error).__name__}")
                replies.append({})
        failures = errors + check(scenario["id"], replies, repository)
        usage_by_model = dict(usage.usage_metadata)
    first_tokens = [r["first_token_ms"] for r in replies if r.get("first_token_ms") is not None]
    return {
        "id": scenario["id"],
        "status": "fail" if failures else "pass",
        "failures": failures,
        "turns": len(replies),
        "wording_status": dict(
            sorted(Counter(r.get("wording_status", "none") for r in replies).items())
        ),
        "interpretation_errors": sum(
            r.get("interpretation_status") == "model_error" for r in replies
        ),
        "input_tokens": sum(u.get("input_tokens", 0) for u in usage_by_model.values()),
        "output_tokens": sum(u.get("output_tokens", 0) for u in usage_by_model.values()),
        "usd": round(usd(usage_by_model), 6),
        "first_token_ms_median": statistics.median(first_tokens) if first_tokens else None,
        "seconds": round(perf_counter() - started, 2),
    }


def evaluate(scenarios, *, provider, model, max_usd):
    results, spent = [], 0.0
    for scenario in scenarios:
        if scenario.get("skip"):
            results.append({"id": scenario["id"], "status": "skipped", "reason": scenario["skip"]})
        elif spent >= max_usd:
            results.append({"id": scenario["id"], "status": "not_run", "reason": "budget"})
        else:
            results.append(run_scenario(scenario, provider=provider, model=model))
            spent += results[-1]["usd"]
    ran = [r for r in results if "usd" in r]
    return {
        "provider": provider,
        "model": model if provider == "anthropic" else None,
        "clock": DEFAULT_NOW,
        "passed": sum(r["status"] == "pass" for r in results),
        "evaluated": len(ran),
        "input_tokens": sum(r["input_tokens"] for r in ran),
        "output_tokens": sum(r["output_tokens"] for r in ran),
        "usd": round(sum(r["usd"] for r in ran), 6),
        "scenarios": results,
    }


def markdown(report):
    rows = [
        "| Scenario | Result | Turns | Wording status | Tokens in/out | USD | Median TTFT ms |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in report["scenarios"]:
        if "usd" not in r:
            rows.append(f"| `{r['id']}` | {r['status']}: {r['reason']} | | | | | |")
            continue
        wording = ", ".join(f"{k} {v}" for k, v in r["wording_status"].items())
        result = r["status"] + (f" ({'; '.join(r['failures'])})" if r["failures"] else "")
        rows.append(
            f"| `{r['id']}` | {result} | {r['turns']} | {wording} | "
            f"{r['input_tokens']:,}/{r['output_tokens']:,} | {r['usd']:.4f} | "
            f"{r['first_token_ms_median'] if r['first_token_ms_median'] is not None else '—'} |"
        )
    return "\n".join(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=["mock", "anthropic"], default="mock")
    parser.add_argument("--max-usd", type=float, default=1.0)
    parser.add_argument("--confirm-spend", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.provider == "anthropic" and not args.confirm_spend:
        sys.exit("Paid run: agree on a budget, then pass --confirm-spend and --max-usd.")
    for key in list(os.environ):
        if key.startswith(("LANGSMITH_", "LANGCHAIN_", "ELEVENLABS_")) or (
            args.provider == "mock" and key.startswith("ANTHROPIC_")
        ):
            del os.environ[key]
    os.environ.update(LANGSMITH_TRACING="false", LANGCHAIN_TRACING_V2="false")
    model = "claude-haiku-4-5-20251001"
    if args.provider == "anthropic":
        from collections_lab.config.settings import load_settings

        model = load_settings(ROOT / ".env").anthropic_model
        if not os.getenv("ANTHROPIC_API_KEY"):
            sys.exit("ANTHROPIC_API_KEY is not set in the environment or local .env.")
        for key in list(os.environ):
            if key.startswith(("LANGSMITH_", "LANGCHAIN_")):
                del os.environ[key]
        os.environ.update(LANGSMITH_TRACING="false", LANGCHAIN_TRACING_V2="false")
    scenarios = json.loads((ROOT / "eval/scenarios.json").read_text())
    report = evaluate(scenarios, provider=args.provider, model=model, max_usd=args.max_usd)
    output = args.output or ROOT / f"eval/results/{args.provider}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(markdown(report))
    print(f"\n{report['passed']}/{report['evaluated']} passed · USD {report['usd']:.4f}")


if __name__ == "__main__":
    main()
