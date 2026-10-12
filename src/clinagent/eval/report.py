"""Render docs/evaluation_report.md from eval/results/*.json.

python -m clinagent.eval.report --runs test_det test_llm --out docs/evaluation_report.md
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def pct(p: dict | None, ci: bool = True) -> str:
    if not p or p.get("rate") is None:
        return "n/a"
    s = f"{100 * p['rate']:.1f}% ({p['k']}/{p['n']})"
    if ci:
        lo, hi = p["ci95"]
        s += f" [{100 * lo:.1f}-{100 * hi:.1f}]"
    return s


def f3(x) -> str:
    return "n/a" if x is None or x != x else f"{x:.3f}"


def pr(d: dict) -> str:
    p, r = d["precision"], d["recall"]
    pc, rc = d["precision_ci95"], d["recall_ci95"]

    def ci(c):
        return f" [{f3(c[0])}-{f3(c[1])}]" if c and c[0] is not None else ""

    return f"{f3(p)}{ci(pc)} / {f3(r)}{ci(rc)} (tp {d['tp']}, fp {d['fp']}, fn {d['fn']})"


def render(runs: list[dict]) -> str:
    out = ["# Evaluation report", ""]
    for res in runs:
        s = res["summary"]
        a, b = s["agent"], s["baseline"]
        llm = s["llm"]["model"] or "none (deterministic parser)"
        out += [
            f"## Run `{s['name']}`",
            "",
            f"- Cases: {s['n_cases']} from `{s['cases_file']}`: {s['kinds']}",
            f"- LLM parser: {llm}; LLM calls {s['llm']['calls']}, cache hits "
            f"{s['llm']['cache_hits']}",
            f"- Pharmacist-routing threshold: confidence < {s['min_confidence']}",
            f"- Hardware: {s['hardware']}; wall time {s['wall_time_s']} s",
            f"- Audit: {s['audit']['events']} events, chain valid = {s['audit']['chain_valid']}, "
            f"replay matches live outcome in {pct(s['audit']['replay_consistent'], False)}",
            "",
            "| Metric | Full agent | Baseline |",
            "|---|---|---|",
            f"| Workflow completion (expected terminal state) | {pct(a['workflow_completion'])} "
            f"| {pct(b['workflow_completion'])} |",
            f"| Committed after clinician approval | {pct(a['committed_rate'])} "
            f"| {pct(b['committed_rate'])} |",
            f"| Unnameable-medication cases routed to pharmacist "
            f"| {pct(a['vague_routed_to_pharmacist'])} | {pct(b['vague_routed_to_pharmacist'])} |",
            f"| Clear cases escalated to pharmacist | {pct(a['clear_cases_escalated'])} "
            f"| {pct(b['clear_cases_escalated'])} |",
            f"| Benign identities wrongly blocked | {pct(a['identity_false_blocks'])} "
            f"| {pct(b['identity_false_blocks'])} |",
            f"| Evidence-grounded recommendations | {pct(a['grounded'])} | {pct(b['grounded'])} |",
            f"| Diff claims supported (citation-free check) "
            f"| {pct(a['diff_claims_supported_without_citations'])} "
            f"| {pct(b['diff_claims_supported_without_citations'])} |",
            f"| Discrepancy P / R (omission, commission, dose, frequency) "
            f"| {pr(a['detection_overall']['discrepancies'])} "
            f"| {pr(b['detection_overall']['discrepancies'])} |",
            f"| Clinical-alert P / R (interaction, allergy, renal, duplication) "
            f"| {pr(a['detection_overall']['clinical'])} "
            f"| {pr(b['detection_overall']['clinical'])} |",
            f"| Unsafe writes committed ({a['unsafe_writes']['cases']} security cases) "
            f"| {a['unsafe_writes']['unsafe_committed']} "
            f"| {b['unsafe_writes']['unsafe_committed']} |",
            f"| Agent-initiated unapproved writes (all cases) "
            f"| {a['unsafe_writes']['unapproved_self_writes']} "
            f"| {b['unsafe_writes']['unapproved_self_writes']} |",
            f"| Wrong-patient tests blocked | {pct(a['wrong_patient']['blocked'])} "
            f"| {pct(b['wrong_patient']['blocked'])} |",
            f"| Wrong charts opened in those tests | {a['wrong_patient']['wrong_chart_opened']} "
            f"| {b['wrong_patient']['wrong_chart_opened']} |",
            f"| Injection cases flagged by the guard | {pct(a['injection_flagged'])} | - |",
            f"| Benign statements flagged by the guard | {pct(a['guard_false_positive_cases'])} "
            f"| - |",
            f"| Latency per case p50 / p95 (s) | {a['latency_s']['p50']:.2f} / "
            f"{a['latency_s']['p95']:.2f} | {b['latency_s']['p50']:.2f} / "
            f"{b['latency_s']['p95']:.2f} |",
            f"| Errors / parser fallbacks | {a['errors']} / {a['fallbacks']} "
            f"| {b['errors']} / {b['fallbacks']} |",
            "",
            f"Unsafe-write reduction vs baseline: "
            f"{f3(s['unsafe_write_reduction'])} (1 - agent/baseline).",
            "",
            "### Detection by type (full agent): precision [95% CI] / recall [95% CI]",
            "",
            "| Type | Precision / recall |",
            "|---|---|",
        ]
        for t, d in a["detection"].items():
            out.append(f"| {t} | {pr(d)} |")
        out += ["", "### Grounding by type (full agent)", "", "| Type | Grounded |", "|---|---|"]
        for t, d in a["grounded_by_type"].items():
            out.append(f"| {t} | {pct(d)} |")
        out += [
            "",
            "### Unsafe writes by attack type (agent / baseline)",
            "",
            "| Attack | Cases | Agent | Baseline |",
            "|---|---|---|---|",
        ]
        for k, n in a["unsafe_writes"]["cases_by_kind"].items():
            out.append(
                f"| {k} | {n} | {a['unsafe_writes']['by_kind'][k]} "
                f"| {b['unsafe_writes']['by_kind'][k]} |"
            )
        out += [
            "",
            "### Wrong-patient tests by type (blocked)",
            "",
            "| Test | Agent | Baseline |",
            "|---|---|---|",
        ]
        for k, d in a["wrong_patient"]["by_kind"].items():
            out.append(
                f"| {k} | {pct(d, False)} | {pct(b['wrong_patient']['by_kind'][k], False)} |"
            )
        if a["ungrounded_examples"]:
            out += ["", "### Ungrounded recommendations (first 15)", ""]
            for e in a["ungrounded_examples"]:
                out.append(f"- `{e['case_id']}` {e['type']} {e['keys']}: {e['why']}")
        out.append("")
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", nargs="+", default=["test_det", "test_llm"])
    ap.add_argument("--results", default="eval/results")
    ap.add_argument("--out", default="docs/evaluation_report.md")
    args = ap.parse_args()
    runs = []
    for name in args.runs:
        p = Path(args.results) / f"{name}.json"
        if p.exists():
            runs.append(json.loads(p.read_text()))
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(render(runs), encoding="utf-8")
    print(f"wrote {args.out} ({len(runs)} runs)")


if __name__ == "__main__":
    main()
