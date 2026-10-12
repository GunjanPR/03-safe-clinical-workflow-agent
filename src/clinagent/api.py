"""FastAPI service.

Demo authentication: the caller's identity and role come from the `X-Actor` and `X-Role`
headers. A real deployment would take them from the hospital's identity provider (SMART on
FHIR / OIDC); RBAC, approval tokens and the context lock work the same either way.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

from . import __version__
from .agent import ReconciliationAgent, replay_session
from .approvals import ApprovalError, ApprovalService
from .audit import AuditLog
from .config import get_settings
from .identity import IdentityNotVerified, WrongPatient
from .kb import knowledge_base
from .parser import LLMClient
from .rbac import AccessDenied, Perm, require
from .store import get_store
from .telemetry import setup_tracing, tracer


class Caller(BaseModel):
    actor: str
    role: str


def caller(x_actor: str = Header(...), x_role: str = Header(...)) -> Caller:
    return Caller(actor=x_actor, role=x_role)


class Identity(BaseModel):
    given: str | None = None
    family: str | None = None
    birth_date: str | None = None
    mrn: str | None = None
    phone: str | None = None


class ReconcileRequest(BaseModel):
    identity: Identity
    statement: str = Field(..., description="Patient-reported medication list (free text)")


class CommitRequest(BaseModel):
    token: str | None = None


def build_agent() -> ReconciliationAgent:
    s = get_settings()
    llm = (
        LLMClient(s.llm_base_url, s.llm_model, s.llm_api_key, s.llm_timeout_s)
        if s.llm_base_url
        else None
    )
    secret = s.approval_secret.encode() if s.approval_secret else None
    store = get_store(s.database_url)
    if s.fhir_base_url:  # read charts from HAPI FHIR; writes stay in the SQL store
        from .fhir_store import FhirStore

        store = FhirStore(s.fhir_base_url, store)
    return ReconciliationAgent(
        store,
        AuditLog(s.audit_path),
        ApprovalService(secret=secret, ttl_s=s.approval_ttl_s),
        llm=llm,
        min_confidence=s.min_confidence,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_tracing(get_settings().otel_exporter)
    app.state.agent = build_agent()
    yield


app = FastAPI(title="Safe Clinical Workflow Agent", version=__version__, lifespan=lifespan)


@app.middleware("http")
async def trace_requests(request: Request, call_next):
    with tracer.start_as_current_span(f"{request.method} {request.url.path}") as span:
        t0 = time.perf_counter()
        response = await call_next(request)
        span.set_attribute("http.status_code", response.status_code)
        response.headers["x-latency-ms"] = f"{(time.perf_counter() - t0) * 1000:.1f}"
        return response


def _agent(request: Request) -> ReconciliationAgent:
    return request.app.state.agent


def _session(agent: ReconciliationAgent, session_id: str):
    s = agent.sessions.get(session_id)
    if not s:
        raise HTTPException(404, "unknown session")
    return s


@app.get("/health")
def health():
    return {"status": "ok", "version": __version__}


@app.get("/ready")
def ready(request: Request):
    agent = _agent(request)
    try:
        agent.store.find_patients(mrn="__probe__")
    except Exception as e:  # pragma: no cover - depends on the database
        raise HTTPException(503, f"store unavailable: {e}") from e
    return {"status": "ready", "llm": bool(agent.llm), "kb_entries": len(agent.kb.entries)}


@app.post("/sessions")
def start_session(request: Request, who: Caller = Depends(caller)):
    try:
        require(who.role, Perm.RUN_RECONCILIATION)
    except AccessDenied as e:
        raise HTTPException(403, str(e)) from e
    s = _agent(request).start_session(who.actor, who.role)
    return {"session_id": s.session_id}


@app.post("/sessions/{session_id}/reconcile")
def reconcile(
    session_id: str, body: ReconcileRequest, request: Request, who: Caller = Depends(caller)
):
    agent = _agent(request)
    s = _session(agent, session_id)
    if who.actor != s.actor:
        raise HTTPException(403, "session belongs to another user")
    res = agent.run(s, body.identity.model_dump(exclude_none=True), body.statement)
    if res.get("status") == "blocked":
        raise HTTPException(403, {"status": "blocked", "reason": res.get("reason")})
    return res


@app.get("/sessions/{session_id}/chart/{patient_id}")
def chart(session_id: str, patient_id: str, request: Request, who: Caller = Depends(caller)):
    agent = _agent(request)
    s = _session(agent, session_id)
    try:
        c = agent.read_chart(s, patient_id)
    except (WrongPatient, IdentityNotVerified) as e:
        raise HTTPException(403, f"wrong patient: {e}") from e
    except AccessDenied as e:
        raise HTTPException(403, str(e)) from e
    return c.__dict__


@app.post("/sessions/{session_id}/approve")
def approve(session_id: str, request: Request, who: Caller = Depends(caller)):
    """A clinician approves the session's current proposal (exactly as proposed)."""
    agent = _agent(request)
    s = _session(agent, session_id)
    if s.result.get("status") not in ("proposed", "pharmacist_review"):
        raise HTTPException(409, "nothing to approve")
    try:
        token = agent.approvals.issue(
            session_id=s.session_id,
            patient_id=s.result["patient_id"],
            changes=s.result["changes"],
            approver=who.actor,
            role=who.role,
        )
    except ApprovalError as e:
        agent.audit.append(s.session_id, who.actor, "approval_denied", reason=e.reason)
        raise HTTPException(403, e.reason) from e
    agent.audit.append(
        s.session_id, who.actor, "approved", proposal_hash=s.result["proposal_hash"], role=who.role
    )
    return {"token": token, "proposal_hash": s.result["proposal_hash"]}


@app.post("/sessions/{session_id}/commit")
def commit(session_id: str, body: CommitRequest, request: Request, who: Caller = Depends(caller)):
    agent = _agent(request)
    s = _session(agent, session_id)
    w = agent.commit(s, actor=who.actor, role=who.role, token=body.token)
    if not w.committed:
        raise HTTPException(403, {"committed": False, "reason": w.reason})
    return {"committed": True, "change_id": w.change_id}


@app.get("/audit/{session_id}/replay")
def replay(session_id: str, request: Request, who: Caller = Depends(caller)):
    try:
        require(who.role, Perm.READ_AUDIT)
    except AccessDenied as e:
        raise HTTPException(403, str(e)) from e
    agent = _agent(request)
    ok, bad = agent.audit.verify()
    return {
        "chain_valid": ok,
        "first_bad_event": bad,
        "replay": replay_session(agent.audit, session_id),
        "events": agent.audit.replay(session_id),
    }


@app.get("/kb/search")
def kb_search(q: str, k: int = 5):
    kb = knowledge_base()
    return [{"score": round(s, 3), **kb.citation(eid)} for eid, s in kb.search(q, k=k)]
