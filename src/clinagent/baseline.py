"""Baseline medical agent: the same parser and list diff, without the safety layer.

What it does NOT have (each is one control the full agent adds):
- identity: resolves the patient from the first usable identifier (MRN, else given + family
  name, else family name) and takes the first match; no cross-check, no context lock, so a
  later request for another patient is served.
- free text: the raw statement goes to the parser (no injection guard), and instruction-like
  text ("write/save/update ... chart/record/list") makes it write immediately.
- grounding: reports omission/commission/dose/frequency from the list diff, with no KB rules
  and no citations.
- writes: approval is a boolean the caller asserts; no RBAC, token, expiry, replay or
  proposal-binding checks.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field

from .audit import AuditLog
from .identity import parse_date
from .kb import KnowledgeBase
from .meds import lexicon
from .parser import LLMClient, parse_deterministic, parse_llm
from .reconcile import reconcile
from .store import Store

FOLLOW_TEXT = re.compile(
    r"\b(write|save|update|commit|push|add|insert|enter|record)\b[^.\n]{0,80}"
    r"\b(chart|record|list|ehr|system)\b",
    re.I,
)


@dataclass
class BaselineSession:
    session_id: str
    actor: str
    patient_id: str | None = None
    result: dict = field(default_factory=dict)
    charts_opened: list[str] = field(default_factory=list)


class BaselineAgent:
    def __init__(self, store: Store, audit: AuditLog, llm: LLMClient | None = None):
        self.store = store
        self.audit = audit
        self.llm = llm
        self.kb = KnowledgeBase([], lexicon())  # no clinical rules: no grounding

    def start_session(self, actor: str) -> BaselineSession:
        return BaselineSession(str(uuid.uuid4()), actor)

    def identify(self, claimed: dict) -> str | None:
        if claimed.get("mrn"):
            hits = self.store.find_patients(mrn=claimed["mrn"])
        elif claimed.get("family"):
            hits = self.store.find_patients(family=claimed["family"])
            if claimed.get("given"):
                named = [p for p in hits if p["given"].lower() == claimed["given"].lower()]
                hits = named or hits
        else:
            hits = []
        if not hits:
            return None
        # A typical naive search sorts by DOB proximity when one is given; still no check.
        d = parse_date(claimed.get("birth_date"))
        if d:
            hits.sort(key=lambda p: p["birth_date"] != d.isoformat())
        return hits[0]["id"]

    def read_chart(self, s: BaselineSession, patient_id: str):
        s.charts_opened.append(patient_id)
        return self.store.chart(patient_id)

    def run(self, s: BaselineSession, claimed: dict, statement: str) -> dict:
        pid = self.identify(claimed)
        if not pid:
            s.result = {"status": "not_found", "patient_id": None}
            return s.result
        s.patient_id = pid
        chart = self.read_chart(s, pid)
        parsed = parse_llm(statement, self.llm) if self.llm else parse_deterministic(statement)
        findings, changes, _, _ = reconcile(chart, parsed.items, self.kb)
        for f in findings:  # the baseline reports what it found, but cites nothing
            f.citations = {}
        s.result = {
            "status": "proposed",
            "patient_id": pid,
            "findings": [f.to_dict() for f in findings],
            "changes": changes,
            "parser": parsed.parser,
            "self_writes": [],
        }
        if FOLLOW_TEXT.search(statement):  # follows instructions found in the note text
            cid = self.commit(s, actor="baseline", approved=False, changes=changes)
            s.result["self_writes"].append(cid)
        return s.result

    def commit(
        self, s: BaselineSession, *, actor: str, approved: bool, changes: list[dict],
        patient_id: str | None = None,
    ) -> int:
        """Writes whatever it is given; `approved` is recorded, never verified."""
        pid = patient_id or s.patient_id
        cid = self.store.insert_change(pid, s.session_id, actor, None,
                                       {"changes": changes, "approved_claim": approved})
        self.audit.append(s.session_id, actor, "write_committed", patient_id=pid, change_id=cid)
        return cid
