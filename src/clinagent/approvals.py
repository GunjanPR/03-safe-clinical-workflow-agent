"""Clinician approval tokens.

A token is an HMAC-signed claim that a named clinician with an approving role reviewed one
exact proposal (by hash) for one patient in one session. Tokens expire and are single use.
`writer.commit_change` refuses any write without a token that validates against the proposal
being written.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
import uuid
from dataclasses import dataclass

from .rbac import Perm, has_perm


class ApprovalError(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def proposal_hash(changes: list[dict]) -> str:
    return hashlib.sha256(
        json.dumps(changes, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


@dataclass
class Approval:
    approval_id: str
    session_id: str
    patient_id: str
    proposal_hash: str
    approver: str
    role: str
    exp: float


class ApprovalService:
    def __init__(self, secret: bytes | None = None, ttl_s: int = 900):
        self._secret = secret or secrets.token_bytes(32)
        self.ttl_s = ttl_s
        self._used: set[str] = set()
        self._lock = threading.Lock()

    def _sign(self, payload: bytes) -> str:
        return hmac.new(self._secret, payload, hashlib.sha256).hexdigest()

    def issue(
        self,
        *,
        session_id: str,
        patient_id: str,
        changes: list[dict],
        approver: str,
        role: str,
        now: float | None = None,
    ) -> str:
        if not has_perm(role, Perm.APPROVE_CHANGE):
            raise ApprovalError(f"role '{role}' cannot approve changes")
        now = time.time() if now is None else now
        claims = {
            "approval_id": str(uuid.uuid4()),
            "session_id": session_id,
            "patient_id": patient_id,
            "proposal_hash": proposal_hash(changes),
            "approver": approver,
            "role": role,
            "exp": now + self.ttl_s,
        }
        payload = json.dumps(claims, sort_keys=True).encode()
        return base64.urlsafe_b64encode(payload).decode() + "." + self._sign(payload)

    def validate(
        self,
        token: str | None,
        *,
        session_id: str,
        patient_id: str,
        changes: list[dict],
        consume: bool = True,
        now: float | None = None,
    ) -> Approval:
        if not token:
            raise ApprovalError("missing_token")
        try:
            b64, sig = token.rsplit(".", 1)
            payload = base64.urlsafe_b64decode(b64.encode())
            claims = json.loads(payload)
        except Exception as e:
            raise ApprovalError("malformed_token") from e
        if not hmac.compare_digest(self._sign(payload), sig):
            raise ApprovalError("bad_signature")
        now = time.time() if now is None else now
        if claims["exp"] < now:
            raise ApprovalError("expired")
        if claims["session_id"] != session_id:
            raise ApprovalError("wrong_session")
        if claims["patient_id"] != patient_id:
            raise ApprovalError("wrong_patient")
        if claims["proposal_hash"] != proposal_hash(changes):
            raise ApprovalError("proposal_mismatch")
        if not has_perm(claims["role"], Perm.APPROVE_CHANGE):
            raise ApprovalError("approver_lacks_permission")
        with self._lock:
            if claims["approval_id"] in self._used:
                raise ApprovalError("replayed")
            if consume:
                self._used.add(claims["approval_id"])
        return Approval(**claims)
