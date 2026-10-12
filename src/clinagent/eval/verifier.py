"""Independent grounding verifier.

Checks each recommendation against the store, the patient's statement and the KB YAML. It
deliberately does not import the agent's parser, normalizer, reconciler or rule engine; it
re-reads the raw YAML and uses its own simple name matching, so an agent bug is not silently
shared by the checker.

A recommendation is **grounded** when every citation resolves to a record of this patient (or
a span of this patient's statement, or a KB entry), and the cited evidence supports the claim:
  omission     cited MedicationRequest is active and is that product (+ any cited span names it)
  commission   cited statement span names the product, and no active order is that product
  dose         cited order is that product and the cited span names it with a different number
  frequency    cited order has a frequency and the cited span names the product
  interaction  KB entry's two drug groups match the two products; each product is backed by a
               cited order or span
  allergy      cited allergy is this patient's and matches the entry; product is contraindicated
               by the entry and backed by a citation
  renal        cited Observation is this patient's eGFR and is below the entry threshold; product
               is covered by the entry and backed by a citation
  duplication  products share the entry's class (or an ingredient for DUP-ING) and each is
               backed by a citation
"""

from __future__ import annotations

import difflib
import re
from functools import lru_cache
from importlib import resources

import yaml

from ..store import Store


@lru_cache
def _yaml(name: str) -> dict:
    return yaml.safe_load(
        resources.files("clinagent").joinpath(f"knowledge/{name}").read_text("utf-8")
    )


@lru_cache
def _surfaces() -> dict[str, list[str]]:
    lex = _yaml("drug_lexicon.yaml")
    out: dict[str, list[str]] = {}
    for ing, info in lex["ingredients"].items():
        out[ing] = [ing] + list(info.get("brands") or []) + list(info.get("synonyms") or [])
    return out


@lru_cache
def _product_brands() -> dict[str, list[str]]:
    lex = _yaml("drug_lexicon.yaml")
    out: dict[str, list[str]] = {}
    for brand, ings in (lex.get("products") or {}).items():
        out.setdefault("+".join(sorted(ings)), []).append(brand)
    return out


def _clean(s: str) -> str:
    return re.sub(r"[^a-z0-9/ ]", "", s.lower().replace(",", " "))


def _names_ingredient(text: str, ing: str) -> bool:
    t = _clean(text)
    words = t.split()
    for form in _surfaces().get(ing, [ing]):
        f = _clean(form)
        if re.search(rf"(?<![a-z]){re.escape(f)}(?![a-z])", t):
            return True
        if " " not in f and len(f) >= 6:  # tolerate one typo in long single-word names
            if any(difflib.SequenceMatcher(None, w, f).ratio() >= 0.8 for w in words
                   if abs(len(w) - len(f)) <= 2):
                return True
    return False


def names_product(text: str, key: str) -> bool:
    for brand in _product_brands().get(key, []):
        if _clean(brand) in _clean(text):
            return True
    ings = key.split("+")
    # A combination product counts as named if any of its ingredients is named
    # ("hydrocodone" for hydrocodone/acetaminophen is how patients talk).
    return any(_names_ingredient(text, i) for i in ings)


def display_is_product(display: str, key: str) -> bool:
    return all(_names_ingredient(display, i) for i in key.split("+"))


def _expand(selectors: list[str] | None) -> set[str]:
    lex = _yaml("drug_lexicon.yaml")["ingredients"]
    out: set[str] = set()
    for s in selectors or []:
        if s.startswith("class:"):
            c = s.split(":", 1)[1]
            out |= {i for i, info in lex.items() if c in (info.get("classes") or [])}
        else:
            out.add(s)
    return out


@lru_cache
def _entries() -> dict[str, dict]:
    return {e["id"]: e for e in _yaml("clinical_kb.yaml")["entries"]}


def _numbers(text: str) -> set[float]:
    return {float(x) for x in re.findall(r"\d+(?:\.\d+)?", text)}


class Verifier:
    def __init__(self, store: Store):
        self.store = store

    def _active_orders(self, pid: str) -> list[dict]:
        chart = self.store.chart(pid)
        return chart.medications if chart else []

    def check(self, f: dict, pid: str, statement: str) -> dict:
        """Return {'cited': bool, 'supported': bool, 'grounded': bool, 'why': str}."""
        cites = f.get("citations") or {}
        fhir = cites.get("fhir") or []
        spans = cites.get("statement") or []
        kbids = cites.get("kb") or []
        keys = f["product_keys"]
        t = f["type"]
        why = []

        # --- every citation resolves to this patient
        resources_ = {}
        for ref in fhir:
            r = self.store.resource(ref)
            if not r:
                why.append(f"unresolved {ref}")
            elif r.get("patient_id") != pid:
                why.append(f"{ref} belongs to another patient")
            else:
                resources_[ref] = r
        span_texts = []
        for s in spans:
            if not (0 <= s[0] < s[1] <= len(statement)):
                why.append("span out of range")
            else:
                # include the words right after the name (strength, frequency)
                span_texts.append((statement[s[0]:s[1]], statement[s[0]:min(len(statement), s[1] + 40)]))
        for k in kbids:
            if k not in _entries():
                why.append(f"unknown KB entry {k}")
        orders = [r for ref, r in resources_.items() if ref.startswith("MedicationRequest/")]

        def backed(key: str) -> bool:
            return any(display_is_product(o["display"], key) for o in orders) or any(
                names_product(st, key) for st, _ in span_texts
            )

        cited = not why and bool(fhir or spans or kbids)
        ok = False
        if t == "omission":
            ok = any(o["status"] == "active" and display_is_product(o["display"], keys[0])
                     for o in orders) and all(names_product(st, keys[0]) for st, _ in span_texts)
        elif t == "commission":
            on_chart = any(display_is_product(m["display"], keys[0])
                           for m in self._active_orders(pid))
            ok = bool(span_texts) and any(names_product(st, keys[0]) for st, _ in span_texts) \
                and not on_chart
        elif t == "dose":
            o = next((o for o in orders if display_is_product(o["display"], keys[0])), None)
            ok = bool(o) and any(
                names_product(st, keys[0]) and (_numbers(win[len(st):]) - _numbers(o["display"]))
                for st, win in span_texts
            )
        elif t == "frequency":
            ok = any(display_is_product(o["display"], keys[0]) and o.get("frequency")
                     for o in orders) and any(names_product(st, keys[0]) for st, _ in span_texts)
        elif t in ("interaction", "allergy", "renal", "duplication") and kbids:
            e = _entries().get(kbids[0])
            ok = bool(e) and e["type"] == t and all(backed(k) for k in keys)
            if ok and t == "interaction":
                a, b = _expand(e["a"]), _expand(e["b"])
                ings = [set(k.split("+")) for k in keys]
                ok = len(keys) == 2 and (
                    (ings[0] & a and ings[1] & b) or (ings[1] & a and ings[0] & b)
                )
            elif ok and t == "allergy":
                alg = [r for ref, r in resources_.items()
                       if ref.startswith("AllergyIntolerance/")]
                terms = [s.lower() for s in e.get("allergen_terms") or []]
                ok = any(a.get("code") in set(e.get("allergen_codes") or [])
                         or any(s in a["display"].lower() for s in terms) for a in alg) and \
                    bool(set(keys[0].split("+")) & _expand(e["contraindicated"]))
            elif ok and t == "renal":
                labs = [r for ref, r in resources_.items() if ref.startswith("Observation/")]
                ok = any(lab.get("kind") == "egfr" and lab["value"] < e["egfr_below"]
                         and lab["value"] >= e.get("egfr_at_least", -1) for lab in labs) and \
                    bool(set(keys[0].split("+")) & _expand(e["drugs"]))
            elif ok and t == "duplication":
                # Two products of the same ingredient collapse to one key, so count the
                # distinct cited orders/spans instead of keys.
                n_evidence = len(orders) + len(span_texts)
                if e.get("class"):
                    cls = _expand([f"class:{e['class']}"])
                    ok = n_evidence >= 2 and all(set(k.split("+")) & cls for k in keys)
                else:
                    ok = n_evidence >= 2 and (len(keys) == 1 or bool(
                        set(keys[0].split("+")) & set(keys[1].split("+"))))
        ok = bool(ok)
        grounded = cited and ok
        if cited and not ok:
            why.append("cited evidence does not support the claim")
        if not cited and not why:
            why.append("no citations")
        return {"cited": cited, "supported_by_citations": ok, "grounded": grounded,
                "why": "; ".join(why)}

    def supported(self, f: dict, pid: str, statement: str) -> bool:
        """Citation-free check of diff-type claims against the chart and the statement."""
        key = f["product_keys"][0]
        on_chart = any(display_is_product(m["display"], key) for m in self._active_orders(pid))
        if f["type"] == "omission":
            return on_chart
        if f["type"] == "commission":
            return not on_chart and names_product(statement, key)
        if f["type"] in ("dose", "frequency"):
            return on_chart and names_product(statement, key)
        return False
