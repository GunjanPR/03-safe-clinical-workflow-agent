# Evaluation report

## Run `dev_det_v4`

- Cases: 150 from `eval/cases_dev.jsonl`: {'standard': 107, 'inj_note': 8, 'inj_med': 7, 'nurse_write': 3, 'claimed_approval': 3, 'expired_approval': 3, 'replayed_approval': 3, 'stale_proposal': 3, 'cross_patient_write': 3, 'wrong_mrn': 2, 'wrong_dob': 2, 'insufficient_identifiers': 2, 'mid_session_read': 2, 'mid_session_reidentify': 2}
- LLM parser: none (deterministic parser); LLM calls 0, cache hits 0
- Pharmacist-routing threshold: confidence < 0.55
- Hardware: Intel64 Family 6 Model 186 Stepping 2, GenuineIntel; wall time 8.1 s
- Audit: 1190 events, chain valid = True, replay matches live outcome in 100.0% (150/150)

| Metric | Full agent | Baseline |
|---|---|---|
| Workflow completion (expected terminal state) | 100.0% (122/122) [96.9-100.0] | 91.0% (111/122) [84.6-94.9] |
| Committed after clinician approval | 91.0% (111/122) [84.6-94.9] | 100.0% (122/122) [96.9-100.0] |
| Unnameable-medication cases routed to pharmacist | 100.0% (11/11) [74.1-100.0] | 0.0% (0/11) [0.0-25.9] |
| Clear cases escalated to pharmacist | 0.0% (0/111) [0.0-3.3] | 0.0% (0/111) [0.0-3.3] |
| Benign identities wrongly blocked | 0.0% (0/122) [0.0-3.1] | 0.0% (0/122) [0.0-3.1] |
| Evidence-grounded recommendations | 100.0% (269/269) [98.6-100.0] | 0.0% (0/181) [0.0-2.1] |
| Diff claims supported (citation-free check) | 100.0% (174/174) [97.8-100.0] | 99.4% (180/181) [96.9-99.9] |
| Discrepancy P / R (omission, commission, dose, frequency) | 1.000 [1.000-1.000] / 1.000 [1.000-1.000] (tp 174, fp 0, fn 0) | 0.961 [0.933-0.984] / 1.000 [1.000-1.000] (tp 174, fp 7, fn 0) |
| Clinical-alert P / R (interaction, allergy, renal, duplication) | 1.000 [1.000-1.000] / 1.000 [1.000-1.000] (tp 95, fp 0, fn 0) | n/a / 0.000 [0.000-0.000] (tp 0, fp 0, fn 95) |
| Unsafe writes committed (33 security cases) | 0 | 40 |
| Agent-initiated unapproved writes (all cases) | 0 | 15 |
| Wrong-patient tests blocked | 100.0% (10/10) [72.2-100.0] | 0.0% (0/10) [0.0-27.8] |
| Wrong charts opened in those tests | 0 | 8 |
| Injection cases flagged by the guard | 100.0% (15/15) [79.6-100.0] | - |
| Benign statements flagged by the guard | 0.0% (0/107) [0.0-3.5] | - |
| Latency per case p50 / p95 (s) | 0.02 / 0.09 | 0.02 / 0.04 |
| Errors / parser fallbacks | 0 / 0 | 0 / 0 |

Unsafe-write reduction vs baseline: 1.000 (1 - agent/baseline).

### Detection by type (full agent): precision [95% CI] / recall [95% CI]

| Type | Precision / recall |
|---|---|
| omission | 1.000 [1.000-1.000] / 1.000 [1.000-1.000] (tp 59, fp 0, fn 0) |
| commission | 1.000 [1.000-1.000] / 1.000 [1.000-1.000] (tp 76, fp 0, fn 0) |
| dose | 1.000 [1.000-1.000] / 1.000 [1.000-1.000] (tp 25, fp 0, fn 0) |
| frequency | 1.000 [1.000-1.000] / 1.000 [1.000-1.000] (tp 14, fp 0, fn 0) |
| interaction | 1.000 [1.000-1.000] / 1.000 [1.000-1.000] (tp 46, fp 0, fn 0) |
| allergy | 1.000 [1.000-1.000] / 1.000 [1.000-1.000] (tp 2, fp 0, fn 0) |
| renal | 1.000 [1.000-1.000] / 1.000 [1.000-1.000] (tp 5, fp 0, fn 0) |
| duplication | 1.000 [1.000-1.000] / 1.000 [1.000-1.000] (tp 42, fp 0, fn 0) |

### Grounding by type (full agent)

| Type | Grounded |
|---|---|
| omission | 100.0% (59/59) [93.9-100.0] |
| commission | 100.0% (76/76) [95.2-100.0] |
| dose | 100.0% (25/25) [86.7-100.0] |
| frequency | 100.0% (14/14) [78.5-100.0] |
| interaction | 100.0% (46/46) [92.3-100.0] |
| allergy | 100.0% (2/2) [34.2-100.0] |
| renal | 100.0% (5/5) [56.6-100.0] |
| duplication | 100.0% (42/42) [91.6-100.0] |

### Unsafe writes by attack type (agent / baseline)

| Attack | Cases | Agent | Baseline |
|---|---|---|---|
| inj_note | 8 | 0 | 8 |
| inj_med | 7 | 0 | 14 |
| nurse_write | 3 | 0 | 3 |
| claimed_approval | 3 | 0 | 3 |
| expired_approval | 3 | 0 | 3 |
| replayed_approval | 3 | 0 | 3 |
| stale_proposal | 3 | 0 | 3 |
| cross_patient_write | 3 | 0 | 3 |

### Wrong-patient tests by type (blocked)

| Test | Agent | Baseline |
|---|---|---|
| wrong_mrn | 100.0% (2/2) | 0.0% (0/2) |
| wrong_dob | 100.0% (2/2) | 0.0% (0/2) |
| insufficient_identifiers | 100.0% (2/2) | 0.0% (0/2) |
| mid_session_read | 100.0% (2/2) | 0.0% (0/2) |
| mid_session_reidentify | 100.0% (2/2) | 0.0% (0/2) |
