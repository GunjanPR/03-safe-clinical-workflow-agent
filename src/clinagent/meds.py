"""Medication normalization: lexicon lookup, product parsing, strengths and frequencies.

A *product* is identified by its sorted tuple of canonical ingredients ("product key"), e.g.
('acetaminophen', 'oxycodone') for Percocet. Chart MedicationRequests and patient-reported
items are both reduced to products, so they can be compared regardless of brand, salt form,
spelling or packaging.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import resources

import yaml

UNIT_TO_MG = {"mg": 1.0, "mcg": 0.001, "g": 1000.0}


@dataclass(frozen=True)
class Product:
    ingredients: tuple[str, ...]
    # ingredient -> (value, unit) as written on the product; unit lowercase ("mg", "mg/ml", ...)
    strengths: tuple[tuple[str, float, str], ...] = ()

    @property
    def key(self) -> str:
        return "+".join(self.ingredients)

    def strength_mg(self, ingredient: str | None = None) -> float | None:
        """Strength of one ingredient (default: the only/first one) in mg, if expressible."""
        for ing, value, unit in self.strengths:
            if (ingredient is None or ing == ingredient) and unit in UNIT_TO_MG:
                return value * UNIT_TO_MG[unit]
        return None

    def strength_signature(self) -> tuple[float, ...] | None:
        """Sorted mg strengths of all ingredients, or None if any is missing or non-mg."""
        if not self.strengths or len(self.strengths) != len(self.ingredients):
            return None
        out = []
        for _, value, unit in self.strengths:
            if unit not in UNIT_TO_MG:
                return None
            out.append(round(value * UNIT_TO_MG[unit], 4))
        return tuple(sorted(out))


@dataclass
class Match:
    ingredients: tuple[str, ...]
    surface: str
    start: int
    end: int
    score: float = 1.0  # 1.0 exact; < 1.0 fuzzy


@dataclass
class Lexicon:
    ingredients: dict[str, dict]
    products: dict[str, list[str]]
    salt_words: list[str]
    surfaces: dict[str, tuple[str, ...]] = field(default_factory=dict)  # lowercase form -> ings
    _pattern: re.Pattern | None = None

    def __post_init__(self) -> None:
        for name, info in self.ingredients.items():
            for form in [name, *(info.get("brands") or []), *(info.get("synonyms") or [])]:
                self.surfaces[_norm(form)] = (name,)
        for brand, ings in self.products.items():
            self.surfaces[_norm(brand)] = tuple(sorted(ings))
        forms = sorted(self.surfaces, key=len, reverse=True)
        self._pattern = re.compile(
            r"(?<![a-z0-9])(" + "|".join(re.escape(f) for f in forms) + r")(?![a-z0-9])"
        )

    def classes(self, ingredient: str) -> list[str]:
        return list(self.ingredients.get(ingredient, {}).get("classes") or [])

    def find_all(self, text: str) -> list[Match]:
        """Exact (case/punctuation-insensitive) matches, longest first, non-overlapping."""
        norm, mapping = _norm_with_map(text)
        out = []
        for m in self._pattern.finditer(norm):
            out.append(
                Match(
                    self.surfaces[m.group(1)],
                    m.group(1),
                    mapping[m.start()],
                    mapping[m.end() - 1] + 1,
                )
            )
        return out

    def fuzzy(self, word: str, cutoff: float = 0.84) -> Match | None:
        """Best fuzzy match of one word or short phrase against the lexicon surfaces."""
        w = _norm(word)
        if len(w) < 5:
            return None
        cands = difflib.get_close_matches(w, self.surfaces.keys(), n=1, cutoff=cutoff)
        if not cands:
            return None
        score = difflib.SequenceMatcher(None, w, cands[0]).ratio()
        return Match(self.surfaces[cands[0]], cands[0], 0, 0, score)


def _norm(s: str) -> str:
    s = s.lower().replace("'", "").replace(".", "").replace(",", " ")
    return re.sub(r"\s+", " ", s).strip()


def _norm_with_map(text: str) -> tuple[str, list[int]]:
    """Normalize like _norm but keep a map from normalized index -> original index."""
    out, mapping = [], []
    prev_space = True
    for i, ch in enumerate(text):
        c = ch.lower()
        if c in "'.":
            continue
        if c == "," or c.isspace():
            if prev_space:
                continue
            c = " "
            prev_space = True
        else:
            prev_space = False
        out.append(c)
        mapping.append(i)
    return "".join(out), mapping


@lru_cache
def lexicon() -> Lexicon:
    data = yaml.safe_load(
        resources.files("clinagent").joinpath("knowledge/drug_lexicon.yaml").read_text("utf-8")
    )
    return Lexicon(data["ingredients"], data.get("products") or {}, data.get("salt_words") or [])


# --------------------------------------------------------------------------- chart products

_PREFIX = re.compile(
    r"^(?:(?:abuse-deterrent|breath-actuated|nda\d+|\d+(?:\.\d+)?\s*(?:hr|actuat|ml|day))\s+)+",
    re.I,
)
_STRENGTH = re.compile(r"(\d+(?:\.\d+)?)\s*(MG|MCG|UNT|ML|G)(/(?:ML|HR|ACTUAT|MG))?(?![a-z])", re.I)


@lru_cache(maxsize=4096)
def parse_display(display: str) -> Product:
    """Parse an RxNorm display string ("Acetaminophen 325 MG / oxyCODONE ... [Percocet]")."""
    lex = lexicon()
    text = re.sub(r"\[[^\]]*\]", "", display)  # brand in brackets is redundant with the generic
    if text.lstrip().startswith("{"):  # contraceptive packs: ingredients only
        ings = sorted({i for m in lex.find_all(text) for i in m.ingredients})
        return Product(tuple(ings))
    text = _PREFIX.sub("", text.strip())
    ings: list[str] = []
    strengths: list[tuple[str, float, str]] = []
    for comp in text.split(" / "):
        found = lex.find_all(comp)
        if not found:
            continue
        m = found[0]
        for ing in m.ingredients:
            if ing not in ings:
                ings.append(ing)
        st = _STRENGTH.search(comp, m.end)
        if st and len(m.ingredients) == 1:
            unit = (st.group(2) + (st.group(3) or "")).lower()
            strengths.append((m.ingredients[0], float(st.group(1)), unit))
    if not ings:  # fall back to brand in brackets or anything recognizable
        ings = sorted({i for m in lex.find_all(display) for i in m.ingredients})
    order = sorted(ings)
    strengths.sort(key=lambda s: order.index(s[0]))
    return Product(tuple(order), tuple(strengths))


# --------------------------------------------------------------------------- frequencies


def chart_per_day(frequency: int | None, period: float | None, unit: str | None) -> float | None:
    if not frequency or not period or not unit:
        return None
    per_unit = {"h": 24.0, "d": 1.0, "wk": 1 / 7, "mo": 1 / 30, "a": 1 / 365}.get(unit)
    if per_unit is None:
        return None
    return round(frequency / period * per_unit, 4)


FREQ_PATTERNS: list[tuple[str, float]] = [
    (r"every other day|alternate days|qod", 0.5),
    (r"once (?:a|per) week|weekly|every week|1x/?wk", 1 / 7),
    (r"every (?:3|three) days|every 72 ?h(?:ou)?rs?", 1 / 3),
    (r"four times|4 times|\bqid\b|q\.i\.d|4x(?:/| a | per )?day|every 6 ?h(?:ou)?rs?|q6h", 4.0),
    (r"three times|3 times|\btid\b|t\.i\.d|3x(?:/| a | per )?day|every 8 ?h(?:ou)?rs?|q8h", 3.0),
    (
        r"twice|two times|2 times|\bbid\b|b\.i\.d|2x(?:/| a | per )?day|every 12 ?h(?:ou)?rs?|"
        r"q12h|morning and (?:night|evening)",
        2.0,
    ),
    (r"every 4 ?h(?:ou)?rs?|q4h", 6.0),
    (
        r"once (?:a|per) day|once daily|\bdaily\b|every day|\bqd\b|q\.d|each day|every morning|"
        r"every night|every evening|at bedtime|nightly|\bqhs\b|in the morning|at night|"
        r"1x(?:/| a | per )?day|once a night|one a day|once in the morning|before bed",
        1.0,
    ),
]
PRN_PATTERN = re.compile(
    r"as needed|\bprn\b|when (?:i|I) need|when needed|if needed|as necessary|when it acts up|"
    r"only when",
    re.I,
)


def text_per_day(text: str) -> float | None:
    t = text.lower()
    for pat, val in FREQ_PATTERNS:
        if re.search(pat, t):
            return val
    return None


_TEXT_STRENGTH = re.compile(
    r"(\d+(?:\.\d+)?)(?:\s*/\s*(\d+(?:\.\d+)?))?\s*(mg|mcg|milligrams?|micrograms?|g|units?|ml)?\b",
    re.I,
)


def text_strengths(text: str) -> list[tuple[float, str]]:
    """Strengths written after a drug name: '20 mg', '20mg', '75 mcg', '5/325', '500'."""
    m = _TEXT_STRENGTH.search(text)
    if not m:
        return []
    unit = (m.group(3) or "mg").lower()
    unit = {
        "milligram": "mg",
        "milligrams": "mg",
        "microgram": "mcg",
        "micrograms": "mcg",
        "unit": "units",
    }.get(unit, unit)
    vals = [float(m.group(1))] + ([float(m.group(2))] if m.group(2) else [])
    return [(v, unit) for v in vals]
