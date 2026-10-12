from fastapi.testclient import TestClient

import clinagent
from clinagent import api
from clinagent.agent import ReconciliationAgent

from .conftest import ANA


def test_version():
    assert clinagent.__version__


def test_api_flow(store, audit, approvals, monkeypatch):
    monkeypatch.setattr(api, "build_agent", lambda: ReconciliationAgent(store, audit, approvals))
    nurse = {"X-Actor": "nurse.kim", "X-Role": "nurse"}
    doc = {"X-Actor": "dr.lee", "X-Role": "physician"}
    with TestClient(api.app) as c:
        assert c.get("/health").json()["status"] == "ok"
        assert c.get("/ready").status_code == 200
        sid = c.post("/sessions", headers=nurse).json()["session_id"]
        r = c.post(
            f"/sessions/{sid}/reconcile",
            headers=nurse,
            json={"identity": ANA, "statement": "Zocor 40 mg daily"},
        )
        assert r.status_code == 200 and r.json()["status"] == "proposed"
        assert c.post(f"/sessions/{sid}/approve", headers=nurse).status_code == 403
        tok = c.post(f"/sessions/{sid}/approve", headers=doc).json()["token"]
        assert (
            c.post(f"/sessions/{sid}/commit", headers=nurse, json={"token": tok}).status_code == 403
        )
        assert c.post(f"/sessions/{sid}/commit", headers=doc, json={"token": tok}).json()[
            "committed"
        ]
        assert c.get(f"/sessions/{sid}/chart/p2", headers=nurse).status_code == 403
        assert c.get(f"/audit/{sid}/replay", headers=nurse).status_code == 403
        rep = c.get(f"/audit/{sid}/replay", headers={"X-Actor": "a", "X-Role": "auditor"})
        assert rep.json()["chain_valid"] and rep.json()["replay"]["status"] == "committed"
        sid2 = c.post("/sessions", headers=nurse).json()["session_id"]
        bad = c.post(
            f"/sessions/{sid2}/reconcile",
            headers=nurse,
            json={"identity": {"family": "Lopez"}, "statement": "x"},
        )
        assert bad.status_code == 403
