"""Keep internal redaction markers out of customer text, including split stream chunks."""

import re

_MARKER = re.compile(r"\[(?:number|redacted)\]|<number_[0-9]+>", re.IGNORECASE)
_PREFIXES = ("[number]", "[redacted]", "<number_")
_REPLACEMENT = "ese dato"


def _possible_marker(value: str) -> bool:
    lower = value.lower()
    return any(marker.startswith(lower) for marker in _PREFIXES) or bool(
        re.fullmatch(r"<number_[0-9]*", lower)
    )


class CustomerTextFilter:
    """Hold only an unfinished marker prefix; pass ordinary prose through immediately."""

    def __init__(self):
        self._pending = ""

    def feed(self, text: str) -> str:
        self._pending += text
        visible = []
        while self._pending:
            match = _MARKER.match(self._pending)
            if match:
                visible.append(_REPLACEMENT)
                self._pending = self._pending[match.end() :]
            elif _possible_marker(self._pending):
                break
            else:
                visible.append(self._pending[0])
                self._pending = self._pending[1:]
        return "".join(visible)

    def finish(self) -> str:
        """Replace an interrupted marker prefix instead of leaking its raw fragment."""
        remaining = _REPLACEMENT if self._pending else ""
        self._pending = ""
        return remaining


def sanitize_customer_text(text: str) -> str:
    filtered = CustomerTextFilter()
    return filtered.feed(text) + filtered.finish()


def contains_internal_marker(text: str) -> bool:
    """Include unfinished trailing markers that would be hidden by the stream filter."""
    return sanitize_customer_text(text) != text
