"""Identidad exclusivamente sintética, sin atribuir autenticación real."""

import hmac
import json
import re

from agents.collections.config import data_directory


def verify_demo_code(account_id: str, text: str) -> bool:
    match = re.fullmatch(r"verificar\s+(\d{4})", text.strip(), re.IGNORECASE)
    if not match:
        return False
    codes = json.loads((data_directory() / "fixtures/identity.json").read_text())
    expected = codes.get(account_id, "")
    return bool(expected) and hmac.compare_digest(match.group(1), expected)
