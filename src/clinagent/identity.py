"""Patient identity verification and the per-session context lock.

A session may touch a chart only after identity is verified with at least two independent
identifiers (name, date of birth, MRN, phone) that all agree with exactly one patient and with
no provided identifier contradicting it. The verified patient id is then locked to the session;
any later read or write for a different patient raises `WrongPatient`.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field

from .store import Store

MIN_IDENTIFIERS = 2


class WrongPatient(Exception):
    pass


class IdentityNotVerified(Exception):
    pass


@dataclass
class IdentityResult:
    verified: bool
    patient_id: str | None
    matched: list[str] = field(default_factory=list)
    reason: str = ""


def _norm_name(s: str | None) -> str:
    return re.sub(r"[^a-z]", "", (s or "").lower())


def _norm_phone(s: str | None) -> str:
    return re.sub(r"\D", "", s or "")[-10:]


def parse_date(s: str | dt.date | None) -> dt.date | None:
    if s is None or isinstance(s, dt.date):
        return s
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%d %b %Y", "%b %d, %Y", "%B %d, %Y"):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _compare(claimed: dict, p: dict) -> tuple[list[str], list[str]]:
    """Return (matched identifiers, contradicted identifiers)."""
    matched, bad = [], []
    if claimed.get("family") or claimed.get("given"):
        ok = _norm_name(claimed.get("family")) == _norm_name(p["family"]) and (
            not claimed.get("given") or _norm_name(claimed["given"]) == _norm_name(p["given"])
        )
        if not ok:
            bad.append("name")
        elif claimed.get("given"):  # family name alone is too weak to count as an identifier
            matched.append("name")
    if claimed.get("birth_date"):
        d = parse_date(claimed["birth_date"])
        (matched if d and d.isoformat() == p["birth_date"] else bad).append("birth_date")
    if claimed.get("mrn"):
        (matched if claimed["mrn"].strip() == p["mrn"] else bad).append("mrn")
    if claimed.get("phone"):
        ok = _norm_phone(claimed["phone"]) and _norm_phone(claimed["phone"]) == _norm_phone(
            p.get("phone")
        )
        (matched if ok else bad).append("phone")
    return matched, bad


def verify_identity(store: Store, claimed: dict) -> IdentityResult:
    """Resolve `claimed` (given, family, birth_date, mrn, phone) to exactly one patient."""
    candidates: dict[str, dict] = {}
    if claimed.get("mrn"):
        for p in store.find_patients(mrn=claimed["mrn"].strip()):
            candidates[p["id"]] = p
    if claimed.get("family"):
        for p in store.find_patients(family=_strip(claimed["family"])):
            candidates[p["id"]] = p
    if not candidates:
        return IdentityResult(False, None, reason="no_candidate")

    full = []
    for p in candidates.values():
        matched, bad = _compare(claimed, p)
        if not bad and len(matched) >= MIN_IDENTIFIERS:
            full.append((p, matched))
    if len(full) == 1:
        return IdentityResult(True, full[0][0]["id"], full[0][1], "verified")
    if len(full) > 1:
        return IdentityResult(False, None, reason="ambiguous")
    # Diagnose the most informative failure for the audit log.
    n_given = sum(1 for k in ("family", "birth_date", "mrn", "phone") if claimed.get(k))
    if n_given < MIN_IDENTIFIERS:
        return IdentityResult(False, None, reason="insufficient_identifiers")
    return IdentityResult(False, None, reason="identifier_mismatch")


def _strip(s: str) -> str:
    return s.strip()


class ContextLock:
    """Binds a session to one verified patient for its lifetime."""

    def __init__(self) -> None:
        self.patient_id: str | None = None

    def lock(self, patient_id: str) -> None:
        if self.patient_id and self.patient_id != patient_id:
            raise WrongPatient(f"session locked to {self.patient_id}, got {patient_id}")
        self.patient_id = patient_id

    def check(self, patient_id: str | None) -> None:
        if self.patient_id is None:
            raise IdentityNotVerified("no verified patient in this session")
        if patient_id != self.patient_id:
            raise WrongPatient(f"session locked to {self.patient_id}, got {patient_id}")
