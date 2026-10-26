"""Bounded Spanish number normalization; meaning/roles still belong to the model."""

import re
import unicodedata
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


def normalize_words(text: str) -> str:
    return "".join(
        char
        for char in unicodedata.normalize("NFD", text.lower())
        if unicodedata.category(char) != "Mn"
    )


SMALL = dict(
    zip(
        [
            "cero",
            "uno",
            "dos",
            "tres",
            "cuatro",
            "cinco",
            "seis",
            "siete",
            "ocho",
            "nueve",
            "diez",
            "once",
            "doce",
            "trece",
            "catorce",
            "quince",
            "dieciseis",
            "diecisiete",
            "dieciocho",
            "diecinueve",
            "veinte",
            "veintiuno",
            "veintidos",
            "veintitres",
            "veinticuatro",
            "veinticinco",
            "veintiseis",
            "veintisiete",
            "veintiocho",
            "veintinueve",
        ],
        range(30),
    )
)
SMALL.update(un=1, una=1, veintiun=21)
TENS = dict(
    zip(
        ["treinta", "cuarenta", "cincuenta", "sesenta", "setenta", "ochenta", "noventa"],
        range(30, 100, 10),
    )
)
HUNDREDS = dict(
    zip(
        [
            "cien",
            "doscientos",
            "trescientos",
            "cuatrocientos",
            "quinientos",
            "seiscientos",
            "setecientos",
            "ochocientos",
            "novecientos",
        ],
        range(100, 1000, 100),
    )
)
HUNDREDS["ciento"] = 100


def _under_thousand(words):
    if not words:
        return 0
    total = 0
    if words[0] in HUNDREDS:
        total = HUNDREDS[words[0]]
        words = words[1:]
    if not words:
        return total
    if len(words) == 1 and words[0] in SMALL | TENS:
        return total + (SMALL | TENS)[words[0]]
    if (
        len(words) == 3
        and words[0] in TENS
        and words[1] == "y"
        and 0 < SMALL.get(words[2], 99) < 10
    ):
        return total + TENS[words[0]] + SMALL[words[2]]
    raise ValueError("Unsupported or ambiguous number")


def number_value(quote: str) -> int:
    """Normalize a whole number expression, never a substring or arbitrary prose."""
    text = normalize_words(quote).strip()
    match = re.fullmatch(r"(\d[\d.,]*)(?:\s+(mil|millon|millones))?", text)
    if match:
        raw, scale = match.groups()
        if re.fullmatch(r"\d+", raw):
            value = Decimal(raw)
        elif re.fullmatch(r"\d{1,3}(?:[.]\d{3})+|\d{1,3}(?:,\d{3})+", raw):
            value = Decimal(raw.replace(".", "").replace(",", ""))
        elif scale and re.fullmatch(r"\d+[.,]\d{1,2}", raw):
            value = Decimal(raw.replace(",", "."))
        else:
            raise ValueError("Ambiguous separators or fractional COP")
        value *= {None: 1, "mil": 1000, "millon": 1000000, "millones": 1000000}[scale]
        if value != value.to_integral_value():
            raise ValueError("Fractional values are not supported")
        return int(value)
    words = text.split()
    total = 0
    for scale, labels in ((1000000, {"millon", "millones"}), (1000, {"mil"})):
        positions = [i for i, word in enumerate(words) if word in labels]
        if len(positions) > 1:
            raise ValueError("Repeated scale")
        if positions:
            index = positions[0]
            total += (_under_thousand(words[:index]) if index else 1) * scale
            words = words[index + 1 :]
    if not text:
        raise ValueError("Missing number")
    return total + _under_thousand(words)


class NumberEvidence(BaseModel):
    """A quoted current-message expression, validated independently of model arithmetic."""

    model_config = ConfigDict(extra="forbid")
    quote: str = Field(
        min_length=1,
        max_length=160,
        description="Exact quote from CURRENT message containing one complete numeric expression, including mil/millones. Examples: uno, una cuota, 250 mil, doscientos mil pesos, 1,5 millones. Never quote prior dialogue.",
    )

    def verify(self, text: str, value: int):
        text = normalize_words(unicodedata.normalize("NFC", text))
        quote = normalize_words(unicodedata.normalize("NFC", self.quote))
        matches = list(re.finditer(re.escape(quote), text))
        spans = number_spans(quote)
        if len(spans) != 1:
            raise ValueError("Evidence must contain exactly one number")
        start, end = spans[0]
        complete_spans = number_spans(text)
        if not any(
            (match.start() + start, match.start() + end) in complete_spans for match in matches
        ):
            raise ValueError("Number evidence is absent from this message")
        if number_value(quote[start:end]) != value:
            raise ValueError("Number disagrees with its source expression")


def identity_spans(text: str):
    """Yield exact spans plus normalized digits; keep leading zeroes and separate alternatives."""
    digits = "|".join(
        word for word, value in SMALL.items() if value < 10 and word not in {"un", "una"}
    )
    pattern = rf"(?<!\w)(?:\d{{1,3}}(?:[.,]\d{{3}})+|\d(?:[ ]+\d)+|\d+|(?:{digits})(?:[ ,]+(?:{digits}))*)(?!\w)"
    for match in re.finditer(pattern, text, re.IGNORECASE):
        raw = match.group()
        if raw[0].isdigit():
            value = re.sub(r"\D", "", raw)
        else:
            value = "".join(str(SMALL[word]) for word in re.split(r"[ ,]+", raw.lower()))
        yield match.start(), match.end(), value


def number_spans(text: str):
    """Find complete expressions so a model cannot select just 'mil' from '250 mil'."""
    words = set(SMALL) | set(TENS) | set(HUNDREDS) | {"mil", "millon", "millones"}
    pattern = (
        r"(?<!\w)(?:[-+]?\d+(?:[.,]\d+)*|"
        + "|".join(sorted(words, key=len, reverse=True))
        + r")(?!\w)"
    )
    normalized = normalize_words(text)
    matches = list(re.finditer(pattern, normalized))
    groups = []
    for match in matches:
        if groups and re.fullmatch(
            r"(?:\s+(?:y\s+)?|\s*[-–]\s*)", normalized[groups[-1][1] : match.start()]
        ):
            groups[-1] = (groups[-1][0], match.end())
        else:
            groups.append((match.start(), match.end()))
    return groups


def normalize_transcript_numbers(text: str, *, identifiers=False) -> str:
    """Render supported spoken cardinals as digits without rewriting unrelated words."""
    text = unicodedata.normalize("NFC", text)
    normalized = normalize_words(text)
    for start, end in reversed(number_spans(text)):
        phrase = normalized[start:end]
        if phrase in {"mil", "millon", "millones"} and re.match(
            r"\s+(?:de\s+)?(?:gracias|disculpas|perdones)\b", normalized[end:]
        ):
            continue
        if re.fullmatch(r"\d+", phrase):
            # A transcription may already contain an identifier with leading zeroes.
            continue
        # Articles are not numbers unless followed by a numeric unit.
        if phrase in {"un", "una"} and not re.match(
            r"\s+(?:cuota|pago|peso|vez)\b", normalized[end:]
        ):
            continue
        try:
            replacement = str(number_value(phrase))
        except ValueError:
            parts = phrase.split()
            if all(part in SMALL and SMALL[part] < 10 for part in parts):
                replacement = " ".join(str(SMALL[part]) for part in parts)
            else:
                continue
        text = text[:start] + replacement + text[end:]
    if identifiers:
        # STT often punctuates digit dictation as a list. Join only complete lists of
        # at least three single digits; never drop an extra "10" to match a known ID.
        def join_digits(match):
            parts = re.split(r"\s*,\s*", match[0])
            return "".join(parts) if all(len(part) == 1 for part in parts) else match[0]

        text = re.sub(
            r"(?<![\w.,])\d+(?:\s*,\s*\d+){2,}(?!\w)",
            join_digits,
            text,
        )
        for start, end, value in reversed(list(identity_spans(text))):
            text = text[:start] + value + text[end:]
    return text
