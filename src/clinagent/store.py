"""Compact clinical store built from Synthea FHIR R4 bulk output.

SQLite by default, PostgreSQL via CLINAGENT_DATABASE_URL. Every row keeps its source FHIR
resource id, so recommendations can cite `MedicationRequest/<id>` etc. and an independent
verifier can check those citations against the store.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from functools import lru_cache

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
    func,
    select,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

# LOINC codes for the labs the reconciliation rules use.
LAB_CODES = {
    "33914-3": "egfr",  # GFR/1.73 sq M predicted (MDRD), what Synthea emits
    "62238-1": "egfr",  # GFR/1.73 sq M predicted (CKD-EPI)
    "6298-4": "potassium",
    "2823-3": "potassium",
    "6301-6": "inr",
    "34714-6": "inr",
    "2160-0": "creatinine",
    "38483-4": "creatinine",
}


class Base(DeclarativeBase):
    pass


class Patient(Base):
    __tablename__ = "patient"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    mrn: Mapped[str] = mapped_column(String(64), index=True)
    given: Mapped[str] = mapped_column(String(128))
    family: Mapped[str] = mapped_column(String(128), index=True)
    birth_date: Mapped[dt.date] = mapped_column(Date)
    gender: Mapped[str] = mapped_column(String(16))
    postal_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)


class MedicationRequest(Base):
    __tablename__ = "medication_request"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    patient_id: Mapped[str] = mapped_column(ForeignKey("patient.id"), index=True)
    status: Mapped[str] = mapped_column(String(32))
    rxnorm: Mapped[str] = mapped_column(String(32))
    display: Mapped[str] = mapped_column(Text)
    authored_on: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    as_needed: Mapped[bool] = mapped_column(Boolean, default=False)
    dose_quantity: Mapped[float | None] = mapped_column(Float, nullable=True)
    frequency: Mapped[int | None] = mapped_column(Integer, nullable=True)
    period: Mapped[float | None] = mapped_column(Float, nullable=True)
    period_unit: Mapped[str | None] = mapped_column(String(8), nullable=True)
    dosage_text: Mapped[str | None] = mapped_column(Text, nullable=True)


class AllergyIntolerance(Base):
    __tablename__ = "allergy_intolerance"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    patient_id: Mapped[str] = mapped_column(ForeignKey("patient.id"), index=True)
    code: Mapped[str] = mapped_column(String(32))
    display: Mapped[str] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(String(32), nullable=True)
    criticality: Mapped[str | None] = mapped_column(String(16), nullable=True)
    clinical_status: Mapped[str] = mapped_column(String(16))


class Condition(Base):
    __tablename__ = "condition"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    patient_id: Mapped[str] = mapped_column(ForeignKey("patient.id"), index=True)
    code: Mapped[str] = mapped_column(String(32))
    display: Mapped[str] = mapped_column(Text)
    clinical_status: Mapped[str] = mapped_column(String(16))
    onset: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)


class Lab(Base):
    __tablename__ = "lab"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    patient_id: Mapped[str] = mapped_column(ForeignKey("patient.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16), index=True)  # egfr | potassium | inr | creatinine
    loinc: Mapped[str] = mapped_column(String(16))
    value: Mapped[float] = mapped_column(Float)
    unit: Mapped[str | None] = mapped_column(String(32), nullable=True)
    effective: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)


class MedListChange(Base):
    """Committed medication-list changes. Only `writer.commit_change` inserts here."""

    __tablename__ = "med_list_change"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    patient_id: Mapped[str] = mapped_column(String(64), index=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    actor: Mapped[str] = mapped_column(String(64))
    approval_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    change: Mapped[dict] = mapped_column(JSON)
    committed_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)


# --------------------------------------------------------------------------- access layer


@dataclass
class Chart:
    """Everything the agent may see about one patient (a read-only snapshot)."""

    patient: dict
    medications: list[dict] = field(default_factory=list)  # active MedicationRequests
    allergies: list[dict] = field(default_factory=list)
    conditions: list[dict] = field(default_factory=list)
    labs: dict[str, dict] = field(default_factory=dict)  # kind -> most recent lab


def _row(obj) -> dict:
    out = {}
    for c in obj.__table__.columns:
        v = getattr(obj, c.name)
        out[c.name] = v.isoformat() if isinstance(v, (dt.date, dt.datetime)) else v
    return out


def make_engine(url: str) -> Engine:
    kwargs = {}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    return create_engine(url, future=True, **kwargs)


class Store:
    def __init__(self, url: str):
        self.engine = make_engine(url)
        Base.metadata.create_all(self.engine)
        self._session = sessionmaker(self.engine, expire_on_commit=False)

    def session(self) -> Session:
        return self._session()

    # -- identity lookups -------------------------------------------------------------------
    def find_patients(self, *, mrn: str | None = None, family: str | None = None) -> list[dict]:
        with self.session() as s:
            q = select(Patient)
            if mrn:
                q = q.where(Patient.mrn == mrn)
            if family:
                q = q.where(func.lower(Patient.family) == family.lower())
            return [_row(p) for p in s.scalars(q.limit(50))]

    def get_patient(self, patient_id: str) -> dict | None:
        with self.session() as s:
            p = s.get(Patient, patient_id)
            return _row(p) if p else None

    # -- chart -------------------------------------------------------------------------------
    def chart(self, patient_id: str) -> Chart | None:
        with self.session() as s:
            p = s.get(Patient, patient_id)
            if not p:
                return None
            meds = s.scalars(
                select(MedicationRequest)
                .where(MedicationRequest.patient_id == patient_id)
                .where(MedicationRequest.status == "active")
                .order_by(MedicationRequest.authored_on)
            )
            allergies = s.scalars(
                select(AllergyIntolerance)
                .where(AllergyIntolerance.patient_id == patient_id)
                .where(AllergyIntolerance.clinical_status == "active")
            )
            conditions = s.scalars(
                select(Condition)
                .where(Condition.patient_id == patient_id)
                .where(Condition.clinical_status == "active")
            )
            labs = s.scalars(
                select(Lab).where(Lab.patient_id == patient_id).order_by(Lab.effective)
            )
            latest: dict[str, dict] = {}
            for lab in labs:
                latest[lab.kind] = _row(lab)  # ordered ascending, so the last one wins
            return Chart(
                patient=_row(p),
                medications=[_row(m) for m in meds],
                allergies=[_row(a) for a in allergies],
                conditions=[_row(c) for c in conditions],
                labs=latest,
            )

    def resource(self, ref: str) -> dict | None:
        """Resolve a FHIR-style reference like 'MedicationRequest/abc' to its stored row."""
        kind, _, rid = ref.partition("/")
        model = {
            "Patient": Patient,
            "MedicationRequest": MedicationRequest,
            "AllergyIntolerance": AllergyIntolerance,
            "Condition": Condition,
            "Observation": Lab,
        }.get(kind)
        if not model or not rid:
            return None
        with self.session() as s:
            obj = s.get(model, rid)
            return _row(obj) if obj else None

    # -- writes (only called from writer.py) ------------------------------------------------
    def insert_change(
        self, patient_id: str, session_id: str, actor: str, approval_id: str | None, change: dict
    ) -> int:
        with self.session() as s:
            row = MedListChange(
                patient_id=patient_id,
                session_id=session_id,
                actor=actor,
                approval_id=approval_id,
                change=json.loads(json.dumps(change, default=str)),
            )
            s.add(row)
            s.commit()
            return row.id

    def changes(self, session_id: str | None = None) -> list[dict]:
        with self.session() as s:
            q = select(MedListChange)
            if session_id:
                q = q.where(MedListChange.session_id == session_id)
            return [_row(c) for c in s.scalars(q)]


@lru_cache
def get_store(url: str) -> Store:
    return Store(url)
