"""The only path that writes to the medication list.

Checks, in order: the session's context lock (right patient), the writer's role (RBAC), and a
clinician approval token bound to this session, patient and exact proposal. Every attempt,
allowed or blocked, is audited.
"""

from __future__ import annotations

from dataclasses import dataclass

from .approvals import ApprovalError, ApprovalService
from .audit import AuditLog
from .identity import ContextLock, IdentityNotVerified, WrongPatient
from .rbac import AccessDenied, Perm, require
from .store import Store


@dataclass
class WriteResult:
    committed: bool
    reason: str
    change_id: int | None = None


def commit_change(
    *,
    store: Store,
    audit: AuditLog,
    approvals: ApprovalService,
    lock: ContextLock,
    session_id: str,
    actor: str,
    role: str,
    patient_id: str,
    changes: list[dict],
    token: str | None,
) -> WriteResult:
    try:
        lock.check(patient_id)
        require(role, Perm.WRITE_MED_LIST)
        approval = approvals.validate(
            token, session_id=session_id, patient_id=patient_id, changes=changes
        )
    except (WrongPatient, IdentityNotVerified) as e:
        audit.append(session_id, actor, "write_blocked", reason="wrong_patient", detail=str(e),
                     patient_id=patient_id)
        return WriteResult(False, "wrong_patient")
    except AccessDenied as e:
        audit.append(session_id, actor, "write_blocked", reason="rbac", detail=str(e),
                     patient_id=patient_id)
        return WriteResult(False, "rbac")
    except ApprovalError as e:
        audit.append(session_id, actor, "write_blocked", reason=e.reason, patient_id=patient_id)
        return WriteResult(False, e.reason)

    change_id = store.insert_change(
        patient_id, session_id, actor, approval.approval_id, {"changes": changes}
    )
    audit.append(
        session_id, actor, "write_committed", patient_id=patient_id, change_id=change_id,
        approval_id=approval.approval_id, approver=approval.approver, n_changes=len(changes),
    )
    return WriteResult(True, "committed", change_id)
