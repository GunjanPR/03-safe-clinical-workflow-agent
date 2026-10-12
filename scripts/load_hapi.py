"""Load patients from the compact store into a HAPI FHIR server (optional backend).

    docker compose --profile fhir up -d hapi
    python scripts/load_hapi.py --fhir http://127.0.0.1:8080/fhir --n 200

Rebuilds minimal FHIR R4 resources (same ids) from the store and PUTs them in transaction
bundles, so citations like MedicationRequest/<id> resolve on either backend.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import httpx
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from clinagent.store import (  # noqa: E402
    AllergyIntolerance,
    Condition,
    Lab,
    MedicationRequest,
    Patient,
    Store,
)

LOINC_DISPLAY = {"egfr": "eGFR", "potassium": "Potassium", "inr": "INR", "creatinine": "Creatinine"}


def _ts(v):
    return v.isoformat() + "Z" if v else None


def resources_for(s, pid: str) -> list[dict]:
    p = s.get(Patient, pid)
    out = [
        {
            "resourceType": "Patient",
            "id": p.id,
            "identifier": [
                {
                    "type": {
                        "coding": [
                            {
                                "system": "http://terminology.hl7.org/CodeSystem/v2-0203",
                                "code": "MR",
                            }
                        ]
                    },
                    "value": p.mrn,
                }
            ],
            "name": [{"use": "official", "family": p.family, "given": p.given.split()}],
            "birthDate": p.birth_date.isoformat(),
            "gender": p.gender,
            "telecom": [{"system": "phone", "value": p.phone}] if p.phone else [],
            "address": [{"postalCode": p.postal_code}] if p.postal_code else [],
        }
    ]
    for m in s.scalars(select(MedicationRequest).where(MedicationRequest.patient_id == pid)):
        di = {"asNeededBoolean": m.as_needed}
        if m.frequency:
            di["timing"] = {
                "repeat": {
                    "frequency": m.frequency,
                    "period": m.period,
                    "periodUnit": m.period_unit,
                }
            }
        if m.dose_quantity:
            di["doseAndRate"] = [{"doseQuantity": {"value": m.dose_quantity}}]
        out.append(
            {
                "resourceType": "MedicationRequest",
                "id": m.id,
                "status": m.status,
                "intent": "order",
                "subject": {"reference": f"Patient/{pid}"},
                "medicationCodeableConcept": {
                    "coding": [
                        {
                            "system": "http://www.nlm.nih.gov/research/umls/rxnorm",
                            "code": m.rxnorm,
                            "display": m.display,
                        }
                    ],
                    "text": m.display,
                },
                "authoredOn": _ts(m.authored_on),
                "dosageInstruction": [di],
            }
        )
    for a in s.scalars(select(AllergyIntolerance).where(AllergyIntolerance.patient_id == pid)):
        out.append(
            {
                "resourceType": "AllergyIntolerance",
                "id": a.id,
                "patient": {"reference": f"Patient/{pid}"},
                "clinicalStatus": {
                    "coding": [
                        {
                            "system": "http://terminology.hl7.org/CodeSystem/allergyintolerance-clinical",
                            "code": a.clinical_status,
                        }
                    ]
                },
                "code": {"coding": [{"code": a.code, "display": a.display}], "text": a.display},
                **({"category": [a.category]} if a.category else {}),
                **({"criticality": a.criticality} if a.criticality else {}),
            }
        )
    for c in s.scalars(select(Condition).where(Condition.patient_id == pid)):
        out.append(
            {
                "resourceType": "Condition",
                "id": c.id,
                "subject": {"reference": f"Patient/{pid}"},
                "clinicalStatus": {
                    "coding": [
                        {
                            "system": "http://terminology.hl7.org/CodeSystem/condition-clinical",
                            "code": c.clinical_status,
                        }
                    ]
                },
                "code": {
                    "coding": [
                        {"system": "http://snomed.info/sct", "code": c.code, "display": c.display}
                    ]
                },
                **({"onsetDateTime": _ts(c.onset)} if c.onset else {}),
            }
        )
    for lab in s.scalars(select(Lab).where(Lab.patient_id == pid)):
        out.append(
            {
                "resourceType": "Observation",
                "id": lab.id,
                "status": "final",
                "subject": {"reference": f"Patient/{pid}"},
                "code": {
                    "coding": [
                        {
                            "system": "http://loinc.org",
                            "code": lab.loinc,
                            "display": LOINC_DISPLAY[lab.kind],
                        }
                    ]
                },
                "valueQuantity": {"value": lab.value, "unit": lab.unit},
                "effectiveDateTime": _ts(lab.effective),
            }
        )
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fhir", default="http://127.0.0.1:8080/fhir")
    ap.add_argument("--db", default="sqlite:///data/clinical.db")
    ap.add_argument("--n", type=int, default=200, help="patients with active meds to load")
    args = ap.parse_args()
    store = Store(args.db)
    http = httpx.Client(timeout=120)
    t0 = time.time()
    with store.session() as s:
        pids = list(
            s.scalars(
                select(MedicationRequest.patient_id)
                .where(MedicationRequest.status == "active")
                .group_by(MedicationRequest.patient_id)
                .order_by(MedicationRequest.patient_id)
                .limit(args.n)
            )
        )
        n_res = 0
        for pid in pids:
            res = resources_for(s, pid)
            bundle = {
                "resourceType": "Bundle",
                "type": "transaction",
                "entry": [
                    {
                        "resource": r,
                        "request": {"method": "PUT", "url": f"{r['resourceType']}/{r['id']}"},
                    }
                    for r in res
                ],
            }
            r = http.post(args.fhir, json=bundle, headers={"Content-Type": "application/fhir+json"})
            r.raise_for_status()
            n_res += len(res)
    print(f"loaded {len(pids)} patients, {n_res} resources in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
