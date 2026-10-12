"""Discrepancy detection, citations and uncertainty scoring.

Inputs: the chart snapshot and the parsed patient-reported list. Output: findings, each with
citations (FHIR resource refs, statement spans, KB entries) and a confidence in [0, 1], plus the
list of medication-list changes the agent proposes for clinician approval.

Discrepancy types
  omission    chart medication the patient does not report taking
  commission  patient-reported medication that is not on the chart
  dose        same product, different strength
  frequency   same product, different doses per day
  interaction / allergy / renal / duplication   KB rules over the reconciled active list
"""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass, field

from .kb import ActiveItem, KnowledgeBase
from .meds import Product, chart_per_day, lexicon, parse_display
from .parser import ReportedItem
from .store import Chart

DISCREPANCY_TYPES = ("omission", "commission", "dose", "frequency")
CLINICAL_TYPES = ("interaction", "allergy", "renal", "duplication")


@dataclass
class Finding:
    type: str
    product_keys: list[str]
    kb_id: str | None
    summary: str
    action: str
    confidence: float
    citations: dict = field(default_factory=dict)  # fhir: [...], statement: [[s,e]], kb: [...]
    severity: str | None = None

    @property
    def key(self) -> tuple:
        return (self.type, self.kb_id, frozenset(self.product_keys))

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ChartItem:
    med: dict
    product: Product
    per_day: float | None

    @property
    def ref(self) -> str:
        return f"MedicationRequest/{self.med['id']}"


def home_meds(chart: Chart) -> list[ChartItem]:
    """Active chart medications that belong on a home medication list."""
    lex = lexicon()
    out = []
    for m in chart.medications:
        p = parse_display(m["display"])
        if not p.ingredients:
            continue
        if any(lex.ingredients.get(i, {}).get("inpatient_only") for i in p.ingredients):
            continue
        out.append(ChartItem(m, p, chart_per_day(m["frequency"], m["period"], m["period_unit"])))
    return out


def chart_daily_mg(ci: ChartItem) -> dict[str, float]:
    qty = ci.med.get("dose_quantity") or 1.0
    per_day = ci.per_day or 1.0
    return {
        i: ci.product.strength_mg(i) * qty * per_day
        for i in ci.product.ingredients
        if ci.product.strength_mg(i)
    }


def reported_daily_mg(r: ReportedItem) -> dict[str, float]:
    mg = r.strength_mg()
    if not mg or len(mg) != len(r.ingredients):
        return {}
    return {i: v * (r.per_day or 1.0) for i, v in zip(r.ingredients, mg)}


def _match(chart_items: list[ChartItem], reported: list[ReportedItem]):
    """One-to-one matching: exact product key first, then ingredient subset/overlap."""
    pairs: list[tuple[ChartItem, ReportedItem, float]] = []
    free_c = list(range(len(chart_items)))
    free_r = list(range(len(reported)))
    for score_fn, quality in (
        (lambda c, r: c.product.key == r.key, 1.0),
        (lambda c, r: set(r.ingredients) < set(c.product.ingredients), 0.75),
        (lambda c, r: set(c.product.ingredients) < set(r.ingredients), 0.75),
    ):
        for ri in list(free_r):
            r = reported[ri]
            cands = [ci for ci in free_c if score_fn(chart_items[ci], r)]
            if not cands:
                continue
            # Prefer the candidate whose strength matches what the patient said.
            rmg = r.strength_mg()
            best = next(
                (ci for ci in cands
                 if rmg and chart_items[ci].product.strength_signature() == tuple(sorted(rmg))),
                cands[0],
            )
            pairs.append((chart_items[best], r, quality))
            free_c.remove(best)
            free_r.remove(ri)
    return pairs, [chart_items[i] for i in free_c], [reported[i] for i in free_r]


def _fmt_per_day(v: float | None) -> str:
    if v is None:
        return "unspecified"
    return {1.0: "once daily", 2.0: "twice daily", 3.0: "three times daily",
            4.0: "four times daily"}.get(v, f"{v:g}/day")


def _span(r: ReportedItem) -> list[list[int]]:
    return [list(r.span)] if r.span else []


def _lab_age_days(lab: dict, as_of: dt.date) -> float | None:
    if not lab or not lab.get("effective"):
        return None
    return (as_of - dt.date.fromisoformat(lab["effective"][:10])).days


def reconcile(
    chart: Chart,
    reported: list[ReportedItem],
    kb: KnowledgeBase,
    *,
    unidentified: list[str] | None = None,
    agreement: dict[str, float] | None = None,
    as_of: dt.date = dt.date(2026, 1, 1),
) -> tuple[list[Finding], list[dict], float, list[str]]:
    """Return (findings, proposed_changes, case_confidence, uncertainty_reasons)."""
    agreement = agreement or {}
    chart_items = home_meds(chart)
    pairs, unmatched_chart, unmatched_rep = _match(chart_items, reported)
    findings: list[Finding] = []
    changes: list[dict] = []
    reasons: list[str] = []

    def item_conf(r: ReportedItem, quality: float = 1.0) -> float:
        return round(min(1.0, r.match_score) * quality * agreement.get(r.key, 1.0), 3)

    # -- omissions: on the chart, not reported as taken
    stopped = {id(c): r for c, r, _ in pairs if not r.taking}
    for c in unmatched_chart + [c for c, r, _ in pairs if not r.taking]:
        r = stopped.get(id(c))
        explicit = r is not None
        findings.append(
            Finding(
                "omission", [c.product.key], None,
                f"{c.med['display']} is on the chart but the patient "
                + ("says they stopped it." if explicit else "did not report taking it."),
                "Confirm with the patient and prescriber; mark as not taking if discontinued.",
                item_conf(r) * 0.95 if explicit else 0.8,
                {"fhir": [c.ref], "statement": _span(r) if r else [], "kb": []},
            )
        )
        changes.append({"op": "mark_not_taking", "medication_request": c.ref,
                        "product": c.product.key})

    # -- commissions: reported and taken, not on the chart
    for r in unmatched_rep:
        if not r.taking:
            continue
        findings.append(
            Finding(
                "commission", [r.key], None,
                f"Patient reports taking {r.surface}"
                + (f" {r.strengths[0][0]:g} {r.strengths[0][1]}" if r.strengths else "")
                + ", which is not on the chart.",
                "Verify the source (OTC, other prescriber) and add to the medication list.",
                item_conf(r),
                {"fhir": [], "statement": _span(r), "kb": []},
            )
        )
        changes.append({"op": "add_reported", "product": r.key, "surface": r.surface,
                        "strengths": r.strengths, "per_day": r.per_day, "prn": r.prn})

    # -- dose and frequency mismatches on matched, taken products
    for c, r, q in pairs:
        if not r.taking:
            continue
        chart_sig = c.product.strength_signature()
        rep_mg = r.strength_mg()
        if chart_sig and rep_mg and len(rep_mg) == len(chart_sig) and tuple(sorted(rep_mg)) != chart_sig:
            findings.append(
                Finding(
                    "dose", [c.product.key], None,
                    f"Chart: {c.med['display']}; patient reports "
                    f"{'/'.join(f'{v:g}' for v in rep_mg)} mg.",
                    "Confirm the current dose with the patient/prescriber and update the record.",
                    item_conf(r, q),
                    {"fhir": [c.ref], "statement": _span(r), "kb": []},
                )
            )
            changes.append({"op": "update_dose", "medication_request": c.ref,
                            "product": c.product.key, "reported_mg": rep_mg})
        if (
            c.per_day is not None and r.per_day is not None and not r.prn
            and not c.med.get("as_needed") and abs(c.per_day - r.per_day) > 1e-6
        ):
            findings.append(
                Finding(
                    "frequency", [c.product.key], None,
                    f"Chart frequency {_fmt_per_day(c.per_day)}; patient reports "
                    f"{_fmt_per_day(r.per_day)}.",
                    "Confirm the intended schedule and update the record.",
                    item_conf(r, q),
                    {"fhir": [c.ref], "statement": _span(r), "kb": []},
                )
            )
            changes.append({"op": "update_frequency", "medication_request": c.ref,
                            "product": c.product.key, "reported_per_day": r.per_day})

    # -- clinical rules over the reconciled active list
    active: list[ActiveItem] = []
    conf_by_item: dict[str, float] = {}
    for c, r, q in pairs:
        if not r.taking:
            continue
        dm = reported_daily_mg(r) if r.strength_mg() and c.product.key == r.key else {}
        if not dm:
            dm = chart_daily_mg(c)
        iid = f"chart:{c.med['id']}"
        refs = [c.ref] + ([f"statement:{r.span[0]}-{r.span[1]}"] if r.span else [])
        active.append(ActiveItem(iid, c.product, c.med["display"], refs, dm))
        conf_by_item[iid] = item_conf(r, q)
    for n, r in enumerate(unmatched_rep):
        if not r.taking:
            continue
        iid = f"reported:{n}"
        active.append(ActiveItem(iid, r.product(), r.surface,
                                 [f"statement:{r.span[0]}-{r.span[1]}"] if r.span else [],
                                 reported_daily_mg(r)))
        conf_by_item[iid] = item_conf(r)

    egfr = chart.labs.get("egfr")
    by_id = {a.item_id: a for a in active}
    for hit in kb.evaluate(active, chart.allergies, egfr):
        entry = kb.entries[hit.entry_id]
        members = [by_id[i] for i in hit.item_ids]
        fhir = [ref for a in members for ref in a.refs if not ref.startswith("statement:")]
        fhir += list(hit.extra_refs)
        spans = [[int(x) for x in ref.split(":")[1].split("-")]
                 for a in members for ref in a.refs if ref.startswith("statement:")]
        conf = min(conf_by_item.get(i, 1.0) for i in hit.item_ids)
        if hit.type == "renal":
            age = _lab_age_days(egfr, as_of)
            if age is None or age > 365:
                conf *= 0.6
                reasons.append(f"eGFR older than a year ({age} days) for {hit.entry_id}")
        # Retrieval cross-check: the cited entry should be retrievable from the drug names.
        names = " ".join(sorted({i for a in members for i in a.product.ingredients}))
        retrieved = [eid for eid, _ in kb.search(f"{hit.type} {names}", k=8, type_=hit.type)]
        if hit.entry_id not in retrieved:
            conf *= 0.9
        labels = ", ".join(a.label for a in members)
        findings.append(
            Finding(
                hit.type, sorted(hit.product_keys), hit.entry_id,
                f"{entry['title']}: {labels}.",
                entry["recommendation"],
                round(conf, 3),
                {"fhir": fhir, "statement": spans, "kb": [hit.entry_id]},
                entry["severity"],
            )
        )

    # -- case-level uncertainty
    case_conf = min([f.confidence for f in findings], default=1.0)
    low_items = [r.surface for r in reported if r.match_score < 0.9]
    if low_items:
        reasons.append(f"fuzzy medication-name match: {', '.join(low_items)}")
    disagree = [k for k, v in agreement.items() if v < 1.0]
    if disagree:
        reasons.append(f"parsers disagree on: {', '.join(disagree)}")
    if unidentified:
        case_conf = min(case_conf, 0.3)
        reasons.append(f"patient described medication(s) they could not name: {unidentified}")
    return findings, changes, round(case_conf, 3), reasons
