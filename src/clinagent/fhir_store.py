"""Optional HAPI FHIR backend.

`FhirStore` reads patients and charts from a FHIR R4 server (HAPI FHIR in docker-compose) and
exposes the same read interface as `store.Store`. Medication-list writes still go to the SQL
store's `med_list_change` table, behind the same approval gate.

    CLINAGENT_FHIR_BASE_URL=http://127.0.0.1:8080/fhir
"""

from __future__ import annotations

import httpx

from .store import LAB_CODES, Chart, Store


def _code(cc: dict | None) -> tuple[str, str]:
    coding = ((cc or {}).get("coding") or [{}])[0]
    return coding.get("code", ""), coding.get("display") or (cc or {}).get("text", "")


def _status(cc: dict | None) -> str:
    return (((cc or {}).get("coding") or [{}])[0]).get("code", "active")


class FhirStore:
    def __init__(self, base_url: str, write_store: Store, timeout: float = 30.0):
        self.base = base_url.rstrip("/")
        self.write_store = write_store
        self.http = httpx.Client(timeout=timeout, headers={"Accept": "application/fhir+json"})

    def _search(self, rtype: str, **params) -> list[dict]:
        out, url = [], f"{self.base}/{rtype}"
        params = {"_count": "200", **params}
        while url:
            r = self.http.get(url, params=params)
            r.raise_for_status()
            b = r.json()
            out += [e["resource"] for e in b.get("entry", [])]
            url = next(
                (link["url"] for link in b.get("link", []) if link["relation"] == "next"), None
            )
            params = None
        return out

    @staticmethod
    def _patient_row(p: dict) -> dict:
        name = (p.get("name") or [{}])[0]
        mrn = next(
            (
                i["value"]
                for i in p.get("identifier", [])
                if any(c.get("code") == "MR" for c in i.get("type", {}).get("coding", []))
            ),
            p["id"],
        )
        return {
            "id": p["id"],
            "mrn": mrn,
            "given": " ".join(name.get("given", [])),
            "family": name.get("family", ""),
            "birth_date": p.get("birthDate"),
            "gender": p.get("gender"),
            "postal_code": ((p.get("address") or [{}])[0]).get("postalCode"),
            "phone": next(
                (t["value"] for t in p.get("telecom", []) if t.get("system") == "phone"), None
            ),
        }

    # -- Store read interface -----------------------------------------------------------
    def find_patients(self, *, mrn: str | None = None, family: str | None = None) -> list[dict]:
        params = {}
        if mrn:
            params["identifier"] = mrn
        if family:
            params["family:exact"] = family
        return [self._patient_row(p) for p in self._search("Patient", **params)]

    def get_patient(self, patient_id: str) -> dict | None:
        r = self.http.get(f"{self.base}/Patient/{patient_id}")
        return self._patient_row(r.json()) if r.status_code == 200 else None

    def chart(self, patient_id: str) -> Chart | None:
        p = self.get_patient(patient_id)
        if not p:
            return None
        meds = []
        for m in self._search(
            "MedicationRequest", subject=f"Patient/{patient_id}", status="active"
        ):
            code, display = _code(m.get("medicationCodeableConcept"))
            di = (m.get("dosageInstruction") or [{}])[0]
            rep = (di.get("timing") or {}).get("repeat") or {}
            dose = next(
                (
                    d["doseQuantity"]["value"]
                    for d in di.get("doseAndRate", [])
                    if "doseQuantity" in d
                ),
                None,
            )
            meds.append(
                {
                    "id": m["id"],
                    "patient_id": patient_id,
                    "status": m["status"],
                    "rxnorm": code,
                    "display": display,
                    "authored_on": m.get("authoredOn"),
                    "as_needed": bool(di.get("asNeededBoolean")),
                    "dose_quantity": dose,
                    "frequency": rep.get("frequency"),
                    "period": rep.get("period"),
                    "period_unit": rep.get("periodUnit"),
                    "dosage_text": di.get("text"),
                }
            )
        allergies = []
        for a in self._search("AllergyIntolerance", patient=patient_id):
            code, display = _code(a.get("code"))
            if _status(a.get("clinicalStatus")) != "active":
                continue
            allergies.append(
                {
                    "id": a["id"],
                    "patient_id": patient_id,
                    "code": code,
                    "display": display,
                    "category": (a.get("category") or [None])[0],
                    "criticality": a.get("criticality"),
                    "clinical_status": "active",
                }
            )
        conditions = []
        for c in self._search(
            "Condition", subject=f"Patient/{patient_id}", **{"clinical-status": "active"}
        ):
            code, display = _code(c.get("code"))
            conditions.append(
                {
                    "id": c["id"],
                    "patient_id": patient_id,
                    "code": code,
                    "display": display,
                    "clinical_status": "active",
                    "onset": c.get("onsetDateTime"),
                }
            )
        labs: dict[str, dict] = {}
        obs = self._search(
            "Observation",
            subject=f"Patient/{patient_id}",
            code=",".join(f"http://loinc.org|{c}" for c in LAB_CODES),
            _sort="date",
        )
        for o in obs:
            loinc, _ = _code(o.get("code"))
            vq = o.get("valueQuantity") or {}
            if loinc in LAB_CODES and "value" in vq:
                labs[LAB_CODES[loinc]] = {
                    "id": o["id"],
                    "patient_id": patient_id,
                    "kind": LAB_CODES[loinc],
                    "loinc": loinc,
                    "value": float(vq["value"]),
                    "unit": vq.get("unit"),
                    "effective": (o.get("effectiveDateTime") or "")[:19] or None,
                }
        return Chart(p, meds, allergies, conditions, labs)

    def resource(self, ref: str) -> dict | None:
        kind, _, rid = ref.partition("/")
        r = self.http.get(f"{self.base}/{kind}/{rid}")
        if r.status_code != 200:
            return None
        body = r.json()
        subj = (body.get("subject") or body.get("patient") or {}).get("reference", "")
        out = {"id": body["id"], "patient_id": subj.split("/")[-1] if subj else body["id"]}
        if kind == "MedicationRequest":
            out["status"] = body.get("status")
            out["display"] = _code(body.get("medicationCodeableConcept"))[1]
        return out

    # -- writes go to SQL -------------------------------------------------------------------
    def insert_change(self, *a, **kw):
        return self.write_store.insert_change(*a, **kw)

    def changes(self, *a, **kw):
        return self.write_store.changes(*a, **kw)
