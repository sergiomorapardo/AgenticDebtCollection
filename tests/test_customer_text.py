"""Incremental filtering must be invariant to provider token boundaries."""

import pytest

from agents.collections.customer_text import (
    CustomerTextFilter,
    contains_internal_marker,
    sanitize_customer_text,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        (
            "Saldo [number], contacto [redacted], código <number_42>.",
            "Saldo ese dato, contacto ese dato, código ese dato.",
        ),
        ("[NUMBER][Redacted]<NUMBER_0>", "ese datoese datoese dato"),
        (
            "Opcional [sí/no], [numbering], <nota>, 1 < 2.",
            "Opcional [sí/no], [numbering], <nota>, 1 < 2.",
        ),
        ("Dato [num", "Dato ese dato"),
        ("Dato <number_123", "Dato ese dato"),
        ("No hay marcadores.", "No hay marcadores."),
    ],
)
def test_filter_is_independent_of_every_possible_two_chunk_boundary(raw, expected):
    assert sanitize_customer_text(raw) == expected
    for index in range(len(raw) + 1):
        filtered = CustomerTextFilter()
        actual = filtered.feed(raw[:index]) + filtered.feed(raw[index:]) + filtered.finish()
        assert actual == expected
    filtered = CustomerTextFilter()
    assert "".join(filtered.feed(character) for character in raw) + filtered.finish() == expected
    assert contains_internal_marker(raw) == (raw != expected)


def test_only_potential_marker_prefix_is_delayed():
    filtered = CustomerTextFilter()
    assert filtered.feed("Te escucho. [num") == "Te escucho. "
    assert filtered.feed("ber] [opcional]") == "ese dato [opcional]"
    assert filtered.finish() == ""
    assert filtered.finish() == ""
