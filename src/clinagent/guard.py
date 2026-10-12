"""Prompt-injection guard for free text (patient statements, notes).

Free text is data, never instructions: the agent has no tool the text could invoke, and every
write needs a clinician approval token. The guard is defense in depth. It removes sentences
that try to instruct the system (role tags, "ignore previous instructions", approval bypass,
"you must write ...") before any text reaches the LLM parser, so injected content cannot become
a medication in the proposal. Flags are written to the audit log.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_PATTERNS: list[tuple[str, str]] = [
    (
        "ignore_instructions",
        r"\b(ignore|disregard|forget|override)\b[^.\n]{0,40}\b(instruction|rule|polic|guideline|prompt|previous|above|prior)",
    ),
    ("role_tag", r"(^|[\s\[<{(])(system|assistant|admin|developer|tool)\s*[:\]>]"),
    (
        "bypass_approval",
        r"\b(without|no|skip|bypass|don'?t need|not need|no need for)\b[^.\n]{0,30}\b(approv|review|sign[- ]?off|verif|confirm|pharmacist|clinician|authori[sz])",
    ),
    (
        "claimed_authority",
        r"\b(pre-?approved|already approved|has approved|is authori[sz]ed|authori[sz]ation (code|token)|approval (code|token)|admin(istrator)? (mode|override)|emergency override)\b",
    ),
    (
        "addressed_to_ai",
        r"\b(you are (now )?(an?|the)|as an ai|dear (ai|assistant|agent|bot)|(ai|assistant|agent|bot|model),? (you )?(must|should|will|need))\b",
    ),
    (
        "write_command",
        r"\b(write|commit|push|save|apply|submit|execute|run)\b[^.\n]{0,40}\b(to|into|in) (the |my |his |her )?(chart|record|ehr|database|med(ication)? list|system)\b[^.\n]{0,40}\b(now|immediately|right away|directly)\b",
    ),
    (
        "tool_markup",
        r"(\{\s*\"(tool|function|action)\"\s*:|<\s*/?\s*(tool|function_call|instructions?)\s*>)",
    ),
]
_COMPILED = [(name, re.compile(p, re.I)) for name, p in _PATTERNS]
_SENT = re.compile(r"[^.!?\n]+[.!?]?|\n", re.M)


@dataclass
class GuardResult:
    clean_text: str
    flags: list[dict] = field(default_factory=list)

    @property
    def injected(self) -> bool:
        return bool(self.flags)


def scan(text: str) -> GuardResult:
    """Blank out instruction-like sentences (keeps character offsets stable for citations)."""
    out = list(text)
    flags = []
    for m in _SENT.finditer(text):
        sent = m.group(0)
        hit = next((name for name, rx in _COMPILED if rx.search(sent)), None)
        if hit:
            flags.append({"pattern": hit, "start": m.start(), "end": m.end(), "text": sent.strip()})
            for i in range(m.start(), m.end()):
                if out[i] != "\n":
                    out[i] = " "
    return GuardResult("".join(out), flags)
