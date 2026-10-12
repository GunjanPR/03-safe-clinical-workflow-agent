"""Role-based access control. Roles map to permission sets; checks are explicit."""

from __future__ import annotations

from enum import Enum


class Perm(str, Enum):
    READ_CHART = "read_chart"
    RUN_RECONCILIATION = "run_reconciliation"
    APPROVE_CHANGE = "approve_change"
    WRITE_MED_LIST = "write_med_list"
    READ_AUDIT = "read_audit"


ROLES: dict[str, set[Perm]] = {
    "physician": {Perm.READ_CHART, Perm.RUN_RECONCILIATION, Perm.APPROVE_CHANGE, Perm.WRITE_MED_LIST},
    "pharmacist": {Perm.READ_CHART, Perm.RUN_RECONCILIATION, Perm.APPROVE_CHANGE, Perm.WRITE_MED_LIST},
    "nurse": {Perm.READ_CHART, Perm.RUN_RECONCILIATION},
    "auditor": {Perm.READ_AUDIT},
    # The agent itself can read and propose, never approve or write on its own authority.
    "agent": {Perm.READ_CHART, Perm.RUN_RECONCILIATION},
}


class AccessDenied(Exception):
    pass


def has_perm(role: str, perm: Perm) -> bool:
    return perm in ROLES.get(role, set())


def require(role: str, perm: Perm) -> None:
    if not has_perm(role, perm):
        raise AccessDenied(f"role '{role}' lacks permission '{perm.value}'")
