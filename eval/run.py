"""Run the scripted scenarios on the Studio graph and record outcome, tokens, cost and latency.

`mock` is offline. `anthropic` spends real money: it requires --confirm-spend and stops
the run once --max-usd is reached (a soft cap: it may overshoot by the turn in flight).
Tracing is always disabled. The report is rewritten after every scenario.
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
# USD per million tokens (input, output). Cache writes use the 5-minute TTL price (1.25x
# input; a 1-hour TTL would be 2x) and cache reads cost 0.1x input.
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
        # LangChain's input_tokens already includes cache reads and writes.
        fresh = max(0, usage.get("input_tokens", 0) - read - write)
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
    offer = replies[3] if len(replies) > 3 else {}
    texts = " ".join(reply.get("text", "") for reply in replies)
    failures = []

    def expect(condition, message):
        if not condition:
            failures.append(message)

    if scenario_id == "full-payment":
        expect(len(agreements) == 1, "exactly one agreement")
        expect(last.get("stage") == "agreed" and last.get("finished") is True, "conversation ends")
        if agreements:
            installments = agreements[0]["offer"]["installments"]
            expect(
                [(p["amount_cop"], p["due_date"]) for p in installments]
                == [(600000, "2026-10-20")],
                "one COP 600,000 payment on 2026-10-20",
            )
            expect(agreements[0]["paid"] is False, "debt remains unpaid")
    elif scenario_id in {"three-installments", "duplicate-confirmation"}:
        expect(offer.get("stage") == "awaiting_confirmation", "offer presented first")
        # Proves contact recording works on a weekday, so contact-blocked can fail.
        expect(len(repository.contacts_for("demo-001")) == 1, "one contact recorded")
        expect(len(agreements) == 1, "exactly one agreement")
        if agreements:
            amounts = [p["amount_cop"] for p in agreements[0]["offer"]["installments"]]
            expect(amounts == [200000, 200000, 200000], "three COP 200,000 installments")
        if scenario_id == "duplicate-confirmation":
            expect(
                len(replies) > 1 and replies[-1] == replies[-2],
                "retried event returns the saved reply",
            )
    elif scenario_id in {"outside-policy", "reported-payment", "disputed-debt"}:
        expect(last.get("stage") == "handoff", "simulated handoff")
        expect(not agreements, "no agreement")
    elif scenario_id == "unverified-identity":
        expect(last.get("finished") is True, "conversation ends")
        expect("600,000" not in texts and "Propuesta" not in texts, "no balance or offer")
    elif scenario_id == "unclear-acceptance":
        expect(offer.get("stage") == "awaiting_confirmation", "offer presented first")
        unclear = replies[4] if len(replies) > 4 else {}
        expect(
            unclear.get("stage") == "ready" and not unclear.get("offer_id"),
            "ambiguous reply invalidates the offer",
        )
        expect(not last.get("offer_id"), "bare confirmation cannot revive the offer")
        expect(not agreements, "no agreement from ambiguous or stale acceptance")
    elif scenario_id == "changed-balance":
        expect(offer.get("stage") == "awaiting_confirmation", "offer presented first")
        expect(
            last.get("stage") == "ready" and not last.get("offer_id"),
            "confirmation requires a new proposal",
        )
        expect(not agreements, "no agreement after the balance changed")
    elif scenario_id == "contact-blocked":
        expect(last.get("stage") == "closed", "contact blocked")
        expect(not repository.contacts_for("demo-001"), "no contact recorded")
        expect("600,000" not in texts, "no balance disclosed")
    else:
        failures.append("no expectations defined")
    return failures


def run_scenario(scenario, *, provider, model, budget_usd=float("inf")):
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
            try:
                spent = usd(usage.usage_metadata)
            except ValueError:
                # A response reported an unpriced model: stop instead of losing the record.
                stopped = f"stopped after turn {index + 1}: unpriced model"
                break
            if spent >= budget_usd:
                stopped = f"stopped after turn {index + 1}: budget reached"
                break
        else:
            stopped = None
        # A stopped scenario reports why instead of expectations for turns never sent.
        failures = errors + ([stopped] if stopped else check(scenario["id"], replies, repository))
        usage_by_model = dict(usage.usage_metadata)
    try:
        cost = round(usd(usage_by_model), 6)
    except ValueError:
        cost = None
    first_tokens = [r["first_token_ms"] for r in replies if r.get("first_token_ms") is not None]
    result = {
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
        "usd": cost,
        "usage_by_model": usage_by_model,
        "first_token_ms_median": statistics.median(first_tokens) if first_tokens else None,
    }
    if provider != "mock":
        # Offline timings vary per machine; keep the committed mock baseline stable.
        result["seconds"] = round(perf_counter() - started, 2)
    return result


def evaluate(scenarios, *, provider, model, max_usd, on_progress=None):
    if provider == "mock" and os.getenv("ANTHROPIC_API_KEY"):
        raise RuntimeError("Unset ANTHROPIC_API_KEY for the offline mock evaluation")
    results, spent = [], 0.0
    for scenario in scenarios:
        if scenario.get("skip"):
            results.append({"id": scenario["id"], "status": "skipped", "reason": scenario["skip"]})
        elif spent >= max_usd:
            results.append({"id": scenario["id"], "status": "not_run", "reason": "budget"})
        else:
            results.append(
                run_scenario(scenario, provider=provider, model=model, budget_usd=max_usd - spent)
            )
            if results[-1]["usd"] is None:
                spent = max_usd  # Unknown cost: stop spending on later scenarios.
            else:
                spent += results[-1]["usd"]
        if on_progress:
            on_progress(summarize(results, provider=provider, model=model))
    return summarize(results, provider=provider, model=model)


def summarize(results, *, provider, model):
    ran = [r for r in results if "usd" in r]
    return {
        "provider": provider,
        "model": model if provider == "anthropic" else None,
        "clock": DEFAULT_NOW,
        "passed": sum(r["status"] == "pass" for r in results),
        "evaluated": len(ran),
        "input_tokens": sum(r["input_tokens"] for r in ran),
        "output_tokens": sum(r["output_tokens"] for r in ran),
        "usd": round(sum(r["usd"] or 0 for r in ran), 6),
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
            f"{r['input_tokens']:,}/{r['output_tokens']:,} | "
            f"{'unpriced' if r['usd'] is None else format(r['usd'], '.4f')} | "
            f"{r['first_token_ms_median'] if r['first_token_ms_median'] is not None else '—'} |"
        )
    return "\n".join(rows)


def disable_external_services(*, keep_anthropic):
    # Speech is never part of this text evaluation; tracing would spend LangSmith quota.
    for key in list(os.environ):
        if key.startswith(("LANGSMITH_", "LANGCHAIN_", "ELEVENLABS_")) or (
            not keep_anthropic and key.startswith("ANTHROPIC_")
        ):
            del os.environ[key]
    os.environ.update(LANGSMITH_TRACING="false", LANGCHAIN_TRACING_V2="false")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=["mock", "anthropic"], default="mock")
    parser.add_argument("--max-usd", type=float, default=1.0)
    parser.add_argument("--confirm-spend", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.provider == "anthropic" and not args.confirm_spend:
        sys.exit("Paid run: agree on a budget, then pass --confirm-spend and --max-usd.")
    model = None
    if args.provider == "anthropic":
        from collections_lab.config.settings import load_settings

        # .env may set tracing variables, so tracing is disabled after loading it.
        model = load_settings(ROOT / ".env").anthropic_model
        if model not in PRICES:
            sys.exit(f"No price configured for {model}; refusing a paid run without a cost record.")
        if not os.getenv("ANTHROPIC_API_KEY"):
            sys.exit("ANTHROPIC_API_KEY is not set in the environment or local .env.")
    disable_external_services(keep_anthropic=args.provider == "anthropic")
    output = args.output or ROOT / f"eval/results/{args.provider}.json"
    output.parent.mkdir(parents=True, exist_ok=True)

    def write(report):
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")

    scenarios = json.loads((ROOT / "eval/scenarios.json").read_text())
    report = evaluate(
        scenarios, provider=args.provider, model=model, max_usd=args.max_usd, on_progress=write
    )
    print(markdown(report))
    print(f"\n{report['passed']}/{report['evaluated']} passed · USD {report['usd']:.4f}")


if __name__ == "__main__":
    main()
