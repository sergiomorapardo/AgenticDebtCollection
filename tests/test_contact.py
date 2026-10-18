"""Límites horarios y calendario colombiano para la simulación de contacto."""

from datetime import datetime

import pytest

from agents.collections.contact import check_contact


def check(timestamp, prior_contacts=None, authorized=True):
    return check_contact(
        now=datetime.fromisoformat(timestamp),
        authorized=authorized,
        prior_contacts=prior_contacts or [],
    )[0]


@pytest.mark.parametrize(
    "timestamp, allowed",
    [
        ("2026-10-20T06:59:59-05:00", False),
        ("2026-10-20T07:00:00-05:00", True),
        ("2026-10-20T18:59:59-05:00", True),
        ("2026-10-20T19:00:00-05:00", False),
        ("2026-10-24T07:59:59-05:00", False),
        ("2026-10-24T08:00:00-05:00", True),
        ("2026-10-24T14:59:59-05:00", True),
        ("2026-10-24T15:00:00-05:00", False),
        ("2026-10-25T12:00:00-05:00", False),
        ("2026-10-20T12:00:00+00:00", True),
        ("2026-10-21T00:00:00+00:00", False),
    ],
)
def test_hours_and_timezone(timestamp, allowed):
    assert check(timestamp) is allowed


@pytest.mark.parametrize(
    "holiday",
    [
        "2026-01-01",
        "2026-03-23",
        "2026-04-02",
        "2026-04-03",
        "2026-05-18",
        "2026-06-08",
        "2026-06-15",
        "2026-07-20",
        "2026-11-16",
        "2026-12-25",
    ],
)
def test_fixed_movable_and_monday_observed_holidays(holiday):
    assert not check(f"{holiday}T10:00:00-05:00")


def test_no_authorization_or_naive_clock_rejected():
    assert not check("2026-10-20T10:00:00-05:00", authorized=False)
    assert not check("2026-10-20T10:00:00")


def test_same_day_direct_contact_prevents_retry():
    history = [
        {
            "timestamp": "2026-10-20T08:00:00-05:00",
            "channel": "phone_simulation",
            "established": True,
        }
    ]
    assert not check("2026-10-20T10:00:00-05:00", history)
    history[0]["established"] = False
    assert check("2026-10-20T10:00:00-05:00", history)


def test_other_channel_same_week_prevents_contact():
    history = [{"timestamp": "2026-10-19T08:00:00-05:00", "channel": "email", "established": True}]
    assert not check("2026-10-20T10:00:00-05:00", history)
    history[0]["channel"] = "phone_simulation"
    assert check("2026-10-20T10:00:00-05:00", history)


def test_new_week_allows_other_channel():
    history = [{"timestamp": "2026-10-23T08:00:00-05:00", "channel": "email", "established": True}]
    assert check("2026-10-26T10:00:00-05:00", history)


@pytest.mark.parametrize(
    "history",
    [
        [{"established": True}],
        [{"established": "true"}],
        [{"timestamp": "2026-10-20T08:00:00", "channel": "email", "established": True}],
        [{"timestamp": "2026-10-20T11:00:00-05:00", "channel": "email", "established": True}],
        [{"timestamp": "2026-10-20T08:00:00-05:00", "established": True}],
    ],
)
def test_invalid_history_fails_closed(history):
    assert not check("2026-10-20T10:00:00-05:00", history)
