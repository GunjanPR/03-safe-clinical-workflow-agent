"""Build the compact clinical store from Synthea FHIR R4 bulk (ndjson) output.

    python scripts/build_store.py --src data/synthea/fhir --db sqlite:///data/clinical.db

Streams each ndjson file, keeps Patient, MedicationRequest, AllergyIntolerance, Condition and
the labs the rules use (eGFR, potassium, INR, creatinine). Idempotent: the target tables are
dropped and rebuilt. Writes data/store_manifest.json with counts and a content hash.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from clinagent.store import (  # noqa: E402
    LAB_CODES,
    AllergyIntolerance,
    Base,
    Condition,
    Lab,
    MedicationRequest,
    Patient,
    make_engine,
)

BATCH = 5000


def _ts(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(dt.UTC).replace(
        tzinfo=None
    )


def _ref_id(ref: str) -> str:
    return ref.split("/", 1)[1] if "/" in ref else ref.replace("urn:uuid:", "")


def _decode(raw: bytes) -> str:
    # Synthea on Windows writes with the platform charset (cp1252) unless the JVM is started
    # with -Dfile.encoding=UTF-8, so accept either.
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def _strip_digits(name: str) -> str:
    # Synthea appends digits to names ("Leland44"); drop them for realistic matching.
    return re.sub(r"\d+", "", name).strip()


def patient_row(r: dict) -> Patient:
    mrn = next(
        (
            i["value"]
            for i in r.get("identifier", [])
            if any(c.get("code") == "MR" for c in i.get("type", {}).get("coding", []))
        ),
        r["id"],
    )
    name = next((n for n in r.get("name", []) if n.get("use") == "official"), r["name"][0])
    addr = (r.get("address") or [{}])[0]
    phone = next((t["value"] for t in r.get("telecom", []) if t.get("system") == "phone"), None)
    return Patient(
        id=r["id"],
        mrn=mrn,
        given=_strip_digits(" ".join(name.get("given", []))),
        family=_strip_digits(name.get("family", "")),
        birth_date=dt.date.fromisoformat(r["birthDate"]),
        gender=r.get("gender", "unknown"),
        postal_code=addr.get("postalCode"),
        phone=phone,
    )


def medreq_row(r: dict) -> MedicationRequest:
    cc = r.get("medicationCodeableConcept") or {}
    coding = (cc.get("coding") or [{}])[0]
    di = (r.get("dosageInstruction") or [{}])[0]
    rep = (di.get("timing") or {}).get("repeat") or {}
    dose = None
    for dr in di.get("doseAndRate", []):
        if "doseQuantity" in dr:
            dose = dr["doseQuantity"].get("value")
    return MedicationRequest(
        id=r["id"],
        patient_id=_ref_id(r["subject"]["reference"]),
        status=r.get("status", "unknown"),
        rxnorm=coding.get("code", ""),
        display=coding.get("display") or cc.get("text", ""),
        authored_on=_ts(r.get("authoredOn")),
        as_needed=bool(di.get("asNeededBoolean", False)),
        dose_quantity=dose,
        frequency=rep.get("frequency"),
        period=rep.get("period"),
        period_unit=rep.get("periodUnit"),
        dosage_text=di.get("text"),
    )


def allergy_row(r: dict) -> AllergyIntolerance:
    coding = (r.get("code", {}).get("coding") or [{}])[0]
    status = ((r.get("clinicalStatus") or {}).get("coding") or [{}])[0].get("code", "active")
    return AllergyIntolerance(
        id=r["id"],
        patient_id=_ref_id(r["patient"]["reference"]),
        code=coding.get("code", ""),
        display=coding.get("display") or r.get("code", {}).get("text", ""),
        category=(r.get("category") or [None])[0],
        criticality=r.get("criticality"),
        clinical_status=status,
    )


def condition_row(r: dict) -> Condition:
    coding = (r.get("code", {}).get("coding") or [{}])[0]
    status = ((r.get("clinicalStatus") or {}).get("coding") or [{}])[0].get("code", "active")
    return Condition(
        id=r["id"],
        patient_id=_ref_id(r["subject"]["reference"]),
        code=coding.get("code", ""),
        display=coding.get("display", ""),
        clinical_status=status,
        onset=_ts(r.get("onsetDateTime")),
    )


def lab_row(r: dict) -> Lab | None:
    codes = [c.get("code") for c in r.get("code", {}).get("coding", [])]
    loinc = next((c for c in codes if c in LAB_CODES), None)
    vq = r.get("valueQuantity")
    if not loinc or not vq or "value" not in vq:
        return None
    return Lab(
        id=r["id"],
        patient_id=_ref_id(r["subject"]["reference"]),
        kind=LAB_CODES[loinc],
        loinc=loinc,
        value=float(vq["value"]),
        unit=vq.get("unit"),
        effective=_ts(r.get("effectiveDateTime")),
    )


FILES = [
    ("Patient.ndjson", patient_row, None),
    ("MedicationRequest.ndjson", medreq_row, None),
    ("AllergyIntolerance.ndjson", allergy_row, None),
    ("Condition.ndjson", condition_row, None),
    # Pre-filter Observation lines by substring before parsing JSON: the file is gigabytes.
    ("Observation.ndjson", lab_row, tuple(f'"{c}"' for c in LAB_CODES)),
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default="data/synthea/fhir")
    ap.add_argument("--db", default="sqlite:///data/clinical.db")
    ap.add_argument("--manifest", default="data/store_manifest.json")
    args = ap.parse_args()

    engine = make_engine(args.db)
    tables = [t for t in Base.metadata.sorted_tables if t.name != "med_list_change"]
    Base.metadata.drop_all(engine, tables=tables)
    Base.metadata.create_all(engine)

    from sqlalchemy.orm import Session

    src = Path(args.src)
    counts: dict[str, int] = {}
    digest = hashlib.sha256()
    t0 = time.time()
    for fname, fn, prefilter in FILES:
        path = src / fname
        n = 0
        batch = []
        if path.exists():
            with path.open("rb") as f, Session(engine) as s:
                for raw in f:
                    line = _decode(raw)
                    if prefilter and not any(p in line for p in prefilter):
                        continue
                    row = fn(json.loads(line))
                    if row is None:
                        continue
                    batch.append(row)
                    digest.update(row.id.encode())
                    n += 1
                    if len(batch) >= BATCH:
                        s.add_all(batch)
                        s.commit()
                        batch = []
                s.add_all(batch)
                s.commit()
        counts[fname.split(".")[0]] = n
        print(f"{fname}: {n} rows ({time.time() - t0:.0f}s)", flush=True)

    with Session(engine) as s:
        from sqlalchemy import func, select

        counts["active_medication_requests"] = s.scalar(
            select(func.count()).select_from(MedicationRequest).where(
                MedicationRequest.status == "active"
            )
        )
        counts["patients_with_active_meds"] = s.scalar(
            select(func.count(func.distinct(MedicationRequest.patient_id))).where(
                MedicationRequest.status == "active"
            )
        )
        for kind in ("egfr", "potassium", "inr", "creatinine"):
            counts[f"lab_{kind}"] = s.scalar(
                select(func.count()).select_from(Lab).where(Lab.kind == kind)
            )

    manifest = {
        "built_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "source": "Synthea (master-branch-latest), seed 42, clinician seed 42, -p 5000, "
        "Delaware, reference date 2026-01-01, years_of_history=5, only_alive_patients",
        "database": args.db.split("://")[0],
        "counts": counts,
        "row_id_sha256": digest.hexdigest(),
    }
    Path(args.manifest).write_text(json.dumps(manifest, indent=2))
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
