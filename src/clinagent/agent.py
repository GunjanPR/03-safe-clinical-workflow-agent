"""The medication-reconciliation agent as a LangGraph state machine.

    verify_identity ──fail──► blocked
          │
    gather_chart ─► guard_text ─► parse ─► reconcile ─► route ─┬─► pharmacist_review
                                                               └─► proposed (awaits approval)

Every node writes an audit event and an OpenTelemetry span. The agent role can read and propose
but never write: `Session.commit` goes through writer.commit_change, which needs a clinician
approval token for the exact proposal.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from . import guard
from .approvals import ApprovalService, proposal_hash
from .audit import AuditLog
from .identity import ContextLock, IdentityNotVerified, WrongPatient, verify_identity
from .kb import KnowledgeBase, knowledge_base
from .parser import LLMClient, ParseResult, parse_deterministic, parse_llm
from .rbac import AccessDenied, Perm, require
from .reconcile import reconcile
from .store import Chart, Store
from .telemetry import tracer
from .writer import WriteResult, commit_change


class State(TypedDict, total=False):
    session_id: str
    actor: str
    role: str
    claimed: dict
    statement: str
    patient_id: str | None
    identity: dict
    chart: Chart
    guard_flags: list[dict]
    clean_statement: str
    parse: dict
    findings: list[dict]
    changes: list[dict]
    proposal_hash: str
    confidence: float
    uncertainty_reasons: list[str]
    status: str  # blocked | pharmacist_review | proposed | error
    reason: str


@dataclass
class Session:
    session_id: str
    actor: str
    role: str
    lock: ContextLock = field(default_factory=ContextLock)
    result: dict[str, Any] = field(default_factory=dict)


class ReconciliationAgent:
    def __init__(
        self,
        store: Store,
        audit: AuditLog,
        approvals: ApprovalService,
        *,
        kb: KnowledgeBase | None = None,
        llm: LLMClient | None = None,
        min_confidence: float = 0.55,
        as_of: dt.date = dt.date(2026, 1, 1),
    ):
        self.store = store
        self.audit = audit
        self.approvals = approvals
        self.kb = kb or knowledge_base()
        self.llm = llm
        self.min_confidence = min_confidence
        self.as_of = as_of
        self.sessions: dict[str, Session] = {}
        self.graph = self._build()

    # ------------------------------------------------------------------ sessions
    def start_session(self, actor: str, role: str) -> Session:
        s = Session(str(uuid.uuid4()), actor, role)
        self.sessions[s.session_id] = s
        self.audit.append(s.session_id, actor, "session_start", role=role)
        return s

    def run(self, session: Session, claimed: dict, statement: str) -> dict:
        with tracer.start_as_current_span("reconcile_session") as span:
            span.set_attribute("session.id", session.session_id)
            state: State = {
                "session_id": session.session_id,
                "actor": session.actor,
                "role": session.role,
                "claimed": claimed,
                "statement": statement,
            }
            self._session = session  # nodes need the lock; one run per agent call
            try:
                out = self.graph.invoke(state)
            except Exception as e:  # never leave a session without a terminal audit event
                self.audit.append(
                    session.session_id, session.actor, "error", error=f"{type(e).__name__}: {e}"
                )
                out = {**state, "status": "error", "reason": f"{type(e).__name__}: {e}"}
            span.set_attribute("status", out.get("status", ""))
            result = {k: v for k, v in out.items() if k not in ("chart", "clean_statement")}
            session.result = result
            return result

    def read_chart(self, session: Session, patient_id: str) -> Chart:
        """Chart access outside the graph (e.g., a follow-up request in the same session)."""
        try:
            require(session.role, Perm.READ_CHART)
            session.lock.check(patient_id)
        except (WrongPatient, IdentityNotVerified, AccessDenied) as e:
            self.audit.append(
                session.session_id,
                session.actor,
                "access_blocked",
                patient_id=patient_id,
                reason=type(e).__name__,
            )
            raise
        self.audit.append(session.session_id, session.actor, "chart_read", patient_id=patient_id)
        return self.store.chart(patient_id)

    def commit(
        self,
        session: Session,
        *,
        actor: str,
        role: str,
        token: str | None,
        changes: list[dict] | None = None,
        patient_id: str | None = None,
    ) -> WriteResult:
        with tracer.start_as_current_span("commit_write"):
            return commit_change(
                store=self.store,
                audit=self.audit,
                approvals=self.approvals,
                lock=session.lock,
                session_id=session.session_id,
                actor=actor,
                role=role,
                patient_id=patient_id or session.result.get("patient_id") or "",
                changes=changes if changes is not None else session.result.get("changes", []),
                token=token,
            )

    # ------------------------------------------------------------------ graph nodes
    def _log(self, state: State, action: str, **data: Any) -> None:
        self.audit.append(state["session_id"], "agent", action, **data)

    def _verify_identity(self, state: State) -> State:
        with tracer.start_as_current_span("verify_identity"):
            try:
                require(state["role"], Perm.RUN_RECONCILIATION)
            except AccessDenied as e:
                self._log(state, "identity_blocked", reason="rbac", detail=str(e))
                return {"status": "blocked", "reason": "rbac", "patient_id": None}
            res = verify_identity(self.store, state["claimed"])
            ident = {"verified": res.verified, "matched": res.matched, "reason": res.reason}
            if not res.verified:
                self._log(state, "identity_blocked", **ident)
                return {
                    "status": "blocked",
                    "reason": res.reason,
                    "identity": ident,
                    "patient_id": None,
                }
            try:
                self._session.lock.lock(res.patient_id)
            except WrongPatient as e:
                self._log(state, "identity_blocked", reason="context_switch", detail=str(e))
                return {
                    "status": "blocked",
                    "reason": "context_switch",
                    "identity": ident,
                    "patient_id": None,
                }
            self._log(state, "identity_verified", patient_id=res.patient_id, matched=res.matched)
            return {"identity": ident, "patient_id": res.patient_id}

    def _gather_chart(self, state: State) -> State:
        with tracer.start_as_current_span("gather_chart"):
            chart = self.read_chart(self._session, state["patient_id"])
            self._log(
                state,
                "chart_snapshot",
                patient_id=state["patient_id"],
                medications=[m["id"] for m in chart.medications],
                allergies=[a["id"] for a in chart.allergies],
                labs={k: v["id"] for k, v in chart.labs.items()},
            )
            return {"chart": chart}

    def _guard(self, state: State) -> State:
        with tracer.start_as_current_span("guard_text") as span:
            g = guard.scan(state["statement"])
            span.set_attribute("guard.flags", len(g.flags))
            if g.flags:
                self._log(state, "injection_flagged", flags=g.flags)
            return {"guard_flags": g.flags, "clean_statement": g.clean_text}

    def _parse(self, state: State) -> State:
        with tracer.start_as_current_span("parse_statement") as span:
            text = state["clean_statement"]
            det = parse_deterministic(text)
            agreement: dict[str, float] = {}
            if self.llm:
                res: ParseResult = parse_llm(text, self.llm)
                if res.parser == "llm":
                    det_keys = {i.key for i in det.items}
                    llm_keys = {i.key for i in res.items}
                    for k in det_keys | llm_keys:
                        agreement[k] = 1.0 if (k in det_keys and k in llm_keys) else 0.6
                    # Items only the deterministic parser found are kept, at lower confidence.
                    res.items += [i for i in det.items if i.key not in llm_keys]
            else:
                res = det
            span.set_attribute("parser", res.parser)
            parsed = res.to_dict()
            parsed["agreement"] = agreement
            self._log(
                state,
                "statement_parsed",
                parser=res.parser,
                fallback_reason=res.fallback_reason,
                items=parsed["items"],
                unidentified=res.unidentified,
                latency_ms=round(res.latency_ms, 1),
                tokens=res.tokens,
            )
            return {"parse": {"result": res, "agreement": agreement, "dict": parsed}}

    def _reconcile(self, state: State) -> State:
        with tracer.start_as_current_span("reconcile"):
            res: ParseResult = state["parse"]["result"]
            findings, changes, conf, reasons = reconcile(
                state["chart"],
                res.items,
                self.kb,
                unidentified=res.unidentified,
                agreement=state["parse"]["agreement"],
                as_of=self.as_of,
            )
            fd = [f.to_dict() for f in findings]
            ph = proposal_hash(changes)
            self._log(
                state,
                "findings",
                findings=fd,
                changes=changes,
                proposal_hash=ph,
                confidence=conf,
                uncertainty_reasons=reasons,
            )
            return {
                "findings": fd,
                "changes": changes,
                "proposal_hash": ph,
                "confidence": conf,
                "uncertainty_reasons": reasons,
                "parse": state["parse"]["dict"],
            }

    def _route(self, state: State) -> State:
        if state["confidence"] < self.min_confidence:
            self._log(
                state,
                "routed_to_pharmacist",
                confidence=state["confidence"],
                reasons=state["uncertainty_reasons"],
            )
            return {"status": "pharmacist_review", "reason": "low_confidence"}
        self._log(
            state,
            "proposal_ready",
            proposal_hash=state["proposal_hash"],
            n_changes=len(state["changes"]),
        )
        return {"status": "proposed", "reason": "awaiting_clinician_approval"}

    def _build(self):
        g = StateGraph(State)
        g.add_node("verify_identity", self._verify_identity)
        g.add_node("gather_chart", self._gather_chart)
        g.add_node("guard_text", self._guard)
        g.add_node("parse", self._parse)
        g.add_node("reconcile", self._reconcile)
        g.add_node("route", self._route)
        g.set_entry_point("verify_identity")
        g.add_conditional_edges(
            "verify_identity",
            lambda s: "end" if s.get("status") == "blocked" else "next",
            {"end": END, "next": "gather_chart"},
        )
        g.add_edge("gather_chart", "guard_text")
        g.add_edge("guard_text", "parse")
        g.add_edge("parse", "reconcile")
        g.add_edge("reconcile", "route")
        g.add_edge("route", END)
        return g.compile()


def replay_session(audit: AuditLog, session_id: str) -> dict:
    """Rebuild a session's outcome from the audit log alone."""
    out: dict[str, Any] = {"session_id": session_id, "events": 0, "writes": [], "blocked": []}
    for ev in audit.replay(session_id):
        out["events"] += 1
        a, d = ev["action"], ev["data"]
        if a == "identity_verified":
            out["patient_id"] = d["patient_id"]
        elif a == "identity_blocked":
            if "status" in out:  # a later blocked attempt in an already-completed session
                out["blocked"].append({"action": a, **d})
            else:
                out["status"] = "blocked"
                out["reason"] = d.get("reason")
        elif a == "statement_parsed":
            out["parser"] = d["parser"]
            out["n_items"] = len(d["items"])
        elif a == "findings":
            out["findings"] = d["findings"]
            out["changes"] = d["changes"]
            out["proposal_hash"] = d["proposal_hash"]
            out["confidence"] = d["confidence"]
        elif a == "routed_to_pharmacist":
            out["status"] = "pharmacist_review"
        elif a == "proposal_ready":
            out["status"] = "proposed"
        elif a == "write_committed":
            out["writes"].append(d)
            out["status"] = "committed"
        elif a in ("write_blocked", "access_blocked"):
            out["blocked"].append({"action": a, **d})
        elif a == "error":
            out["status"] = "error"
    return out
