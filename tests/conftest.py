import datetime as dt

import pytest

from clinagent.approvals import ApprovalService
from clinagent.audit import AuditLog
from clinagent.store import AllergyIntolerance, Lab, MedicationRequest, Patient, Store


def _med(i, pid, display, rx="0", freq=None, period=None, unit=None, prn=False):
    return MedicationRequest(
        id=f"mr-{i}",
        patient_id=pid,
        status="active",
        rxnorm=rx,
        display=display,
        authored_on=dt.datetime(2025, 1, 1),
        as_needed=prn,
        dose_quantity=1.0,
        frequency=freq,
        period=period,
        period_unit=unit,
    )


@pytest.fixture
def store(tmp_path):
    s = Store(f"sqlite:///{(tmp_path / 't.db').as_posix()}")
    with s.session() as db:
        db.add_all(
            [
                Patient(
                    id="p1",
                    mrn="MRN-1",
                    given="Ana",
                    family="Lopez",
                    birth_date=dt.date(1950, 3, 4),
                    gender="female",
                    phone="555-111-2222",
                ),
                Patient(
                    id="p2",
                    mrn="MRN-2",
                    given="Ben",
                    family="Lopez",
                    birth_date=dt.date(1961, 7, 8),
                    gender="male",
                    phone="555-333-4444",
                ),
                Patient(
                    id="p3",
                    mrn="MRN-3",
                    given="Ana",
                    family="Lopez",
                    birth_date=dt.date(1980, 1, 2),
                    gender="female",
                ),
            ]
        )
        db.add_all(
            [
                _med(1, "p1", "simvastatin 40 MG Oral Tablet", freq=1, period=1.0, unit="d"),
                _med(2, "p1", "amLODIPine 2.5 MG Oral Tablet", freq=1, period=1.0, unit="d"),
                _med(
                    3,
                    "p1",
                    "24 HR Metformin hydrochloride 500 MG Extended Release Oral Tablet",
                    freq=2,
                    period=1.0,
                    unit="d",
                ),
                _med(
                    4,
                    "p1",
                    "Acetaminophen 325 MG / Oxycodone Hydrochloride 5 MG Oral Tablet [Percocet]",
                    prn=True,
                ),
                _med(5, "p2", "lisinopril 10 MG Oral Tablet", freq=1, period=1.0, unit="d"),
            ]
        )
        db.add(
            AllergyIntolerance(
                id="al-1",
                patient_id="p1",
                code="1191",
                display="Aspirin",
                category="medication",
                clinical_status="active",
            )
        )
        db.add(
            Lab(
                id="ob-1",
                patient_id="p1",
                kind="egfr",
                loinc="33914-3",
                value=28.0,
                unit="mL/min",
                effective=dt.datetime(2025, 11, 1),
            )
        )
        db.commit()
    return s


@pytest.fixture
def audit():
    return AuditLog()


@pytest.fixture
def approvals():
    return ApprovalService(secret=b"test-secret", ttl_s=60)


ANA = {"given": "Ana", "family": "Lopez", "birth_date": "1950-03-04"}
