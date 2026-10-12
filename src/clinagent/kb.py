"""Clinical knowledge base: rule evaluation over active products, plus BM25 evidence retrieval.

Rules (interaction, allergy, renal, duplication) live in knowledge/clinical_kb.yaml with their
sources. `evaluate()` is a pure function of (active items, allergies, latest eGFR), so the
agent and the evaluation's ground-truth builder apply the same clinical knowledge; what differs
is what each one believes the active list is. `KnowledgeBase.search()` retrieves evidence
passages for citations and for the `/kb/search` endpoint.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import resources
from itertools import combinations

import yaml

from .meds import Lexicon, Product, lexicon


@dataclass
class ActiveItem:
    item_id: str  # "chart:<MedicationRequest id>" or "reported:<n>"
    product: Product
    label: str
    refs: list[str] = field(default_factory=list)  # evidence refs for this item
    daily_mg: dict[str, float] = field(default_factory=dict)  # ingredient -> mg/day if known


@dataclass
class RuleHit:
    entry_id: str
    type: str  # interaction | allergy | renal | duplication
    item_ids: tuple[str, ...]
    product_keys: frozenset[str]
    extra_refs: tuple[str, ...] = ()  # AllergyIntolerance/<id>, Observation/<id>

    @property
    def key(self) -> tuple:
        return (self.type, self.entry_id, self.product_keys)


_TOKEN = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


class KnowledgeBase:
    def __init__(self, entries: list[dict], lex: Lexicon):
        self.entries = {e["id"]: e for e in entries}
        self.lex = lex
        self._docs = {eid: _tokens(self._doc_text(e)) for eid, e in self.entries.items()}
        self._df = Counter(t for toks in self._docs.values() for t in set(toks))
        self._avgdl = sum(len(t) for t in self._docs.values()) / max(len(self._docs), 1)

    # ------------------------------------------------------------------ selectors
    def _expand(self, selectors: list[str] | None) -> set[str]:
        out: set[str] = set()
        for sel in selectors or []:
            if sel.startswith("class:"):
                cls = sel.split(":", 1)[1]
                out |= {i for i in self.lex.ingredients if cls in self.lex.classes(i)}
            else:
                out.add(sel)
        return out

    def _doc_text(self, e: dict) -> str:
        names: set[str] = set()
        for k in ("a", "b", "drugs", "contraindicated"):
            names |= self._expand(e.get(k))
        if e.get("class"):
            names |= self._expand([f"class:{e['class']}"])
        names |= set(e.get("allergen_terms") or [])
        return " ".join(
            [e["type"], e["title"], e["evidence"], e["recommendation"], " ".join(sorted(names))]
        )

    def matches(self, product: Product, selectors: list[str] | None) -> set[str]:
        return set(product.ingredients) & self._expand(selectors)

    # ------------------------------------------------------------------ rule evaluation
    def evaluate(
        self,
        items: list[ActiveItem],
        allergies: list[dict],
        egfr: dict | None,
    ) -> list[RuleHit]:
        hits: list[RuleHit] = []
        dup_pairs_covered: set[frozenset[str]] = set()
        for e in self.entries.values():
            t = e["type"]
            if t == "interaction":
                seen: set[frozenset[str]] = set()
                for x in items:
                    for y in items:
                        if x.item_id == y.item_id:
                            continue
                        if not (self.matches(x.product, e["a"]) and self.matches(y.product, e["b"])):
                            continue
                        pair = frozenset((x.item_id, y.item_id))
                        if pair in seen:
                            continue
                        lim = e.get("dose_limit")
                        if lim:
                            drug = lim["drug"]
                            holder = x if drug in x.product.ingredients else y
                            mg = holder.daily_mg.get(drug)
                            if mg is None or mg <= lim["max_daily_mg"]:
                                continue
                        seen.add(pair)
                        hits.append(
                            RuleHit(e["id"], t, tuple(sorted(pair)),
                                    frozenset((x.product.key, y.product.key)))
                        )
            elif t == "allergy":
                codes = set(e.get("allergen_codes") or [])
                terms = [s.lower() for s in e.get("allergen_terms") or []]
                for a in allergies:
                    if a.get("code") not in codes and not any(
                        s in (a.get("display") or "").lower() for s in terms
                    ):
                        continue
                    for x in items:
                        if self.matches(x.product, e["contraindicated"]):
                            hits.append(
                                RuleHit(e["id"], t, (x.item_id,), frozenset((x.product.key,)),
                                        (f"AllergyIntolerance/{a['id']}",))
                            )
            elif t == "renal":
                if not egfr:
                    continue
                v = egfr["value"]
                if v >= e["egfr_below"] or v < e.get("egfr_at_least", -1):
                    continue
                for x in items:
                    if self.matches(x.product, e["drugs"]):
                        hits.append(
                            RuleHit(e["id"], t, (x.item_id,), frozenset((x.product.key,)),
                                    (f"Observation/{egfr['id']}",))
                        )
            elif t == "duplication" and e.get("class"):
                members = [x for x in items if self.matches(x.product, [f"class:{e['class']}"])]
                # Duplication needs two distinct products (two items of the same product count).
                if len(members) >= 2:
                    ids = tuple(sorted(x.item_id for x in members))
                    hits.append(
                        RuleHit(e["id"], t, ids, frozenset(x.product.key for x in members))
                    )
                    for x, y in combinations(members, 2):
                        dup_pairs_covered.add(frozenset((x.item_id, y.item_id)))
        if "DUP-ING" in self.entries:
            for x, y in combinations(items, 2):
                pair = frozenset((x.item_id, y.item_id))
                if pair in dup_pairs_covered:
                    continue
                if set(x.product.ingredients) & set(y.product.ingredients):
                    hits.append(
                        RuleHit("DUP-ING", "duplication", tuple(sorted(pair)),
                                frozenset((x.product.key, y.product.key)))
                    )
        return hits

    # ------------------------------------------------------------------ retrieval (BM25)
    def search(self, query: str, k: int = 5, type_: str | None = None) -> list[tuple[str, float]]:
        q = _tokens(query)
        n = len(self._docs)
        scores = []
        for eid, toks in self._docs.items():
            if type_ and self.entries[eid]["type"] != type_:
                continue
            tf = Counter(toks)
            s = 0.0
            for t in q:
                if t not in tf:
                    continue
                idf = math.log(1 + (n - self._df[t] + 0.5) / (self._df[t] + 0.5))
                f = tf[t]
                s += idf * f * 2.2 / (f + 1.2 * (0.25 + 0.75 * len(toks) / self._avgdl))
            if s > 0:
                scores.append((eid, s))
        scores.sort(key=lambda x: -x[1])
        return scores[:k]

    def citation(self, entry_id: str) -> dict:
        e = self.entries[entry_id]
        return {
            "kb_id": e["id"],
            "title": e["title"],
            "evidence": " ".join(e["evidence"].split()),
            "recommendation": e["recommendation"],
            "severity": e["severity"],
            "source": e["source"],
        }


@lru_cache
def knowledge_base() -> KnowledgeBase:
    data = yaml.safe_load(
        resources.files("clinagent").joinpath("knowledge/clinical_kb.yaml").read_text("utf-8")
    )
    return KnowledgeBase(data["entries"], lexicon())
