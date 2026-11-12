# Flash Decisions: device pilot, 2026-11-12/13

## Scope and method

Device: Infinix X6765, Android 17. Code baseline: `1411b21` plus the
previously uncommitted first-action Decisions adapter. Primary model:
`gpt-6-luna--fast` through local OpenCodex; Flash operator uses `medium`,
Pro and visual summaries use `max`. Fallback:
`google-antigravity/gemini-3.8-flash` (provider default effort).

Each main Flash variant ran the same two Chinese goals three times, with
alternating order and the Settings homepage re-opened before each trial.
All evaluated phone actions used fresh standalone Artemis MCP processes;
ADB was used only for the fixed starting page and read-only foreground Activity
verification. No network switches or connections were changed. A busy device
was respected. These are small controlled samples, not a general success-rate
estimate. Session duration includes initialization, recording startup and
summary drain; tool-client polling overhead is excluded. Token totals sum only
provider-reported `llm_usage`, including background summaries, without summing
repeated session cumulative totals. They are token usage, not monetary cost.

## Main Flash results

| Task / variant | Verified | Session seconds, each | Median | Mean measured tokens |
| --- | --- | --- | --- | --- |
| Scroll once / regular Flash | 3/3 | 32.551, 29.970, 36.034 | 32.551 | 28,271 |
| Scroll once / Decisions | 3/3 | 26.906, 26.519, 33.783 | 26.906 | 19,864 |
| Open wireless networks / regular Flash | 3/3 | 32.963, 30.377, 26.303 | 30.377 | 35,374 |
| Open wireless networks / Decisions | 3/3 | 36.132, 28.424, 22.110 | 28.424 | 34,753 |

Scroll median improved 17.3% (mean 11.5%); mean tokens fell 29.7%.
Navigation median improved 6.4% (mean 3.3%); mean tokens fell only 1.8%.
Navigation token median actually increased from 27,897 to 42,157 because two
accelerated runs subsequently waited for network scanning. Do not claim a
stable navigation token reduction. All six optional first actions were selected
by the real Google API, not by the exact-instruction local rule. Selection plus
foreground and UI revalidation took 1.231–1.660 seconds; it is not an isolated
HTTP latency measurement. Ordinary first-action decision median across the six
baseline Flash runs was 3.926 seconds versus 1.313 seconds for Decisions.

Wi-Fi success was checked against the actual WifiSettingsActivity. Scroll
success required exactly one up swipe, unchanged Settings homepage, and an
observed list displacement. Scroll distance is not fixed by the goal and differs
between ordinary and candidate-generated swipes, so these results validate one
scroll action rather than equal pixel distance. Execution traces and final
hierarchies were inspected; self-reported completion alone was insufficient.

## Current model latency

Ordinary Flash first-action decisions took 2.895–7.242 seconds. Full simple
navigation/scroll sessions took 26.303–36.034 seconds. Recording/initialization
contributed about 8 seconds. After the Flash completion report, max-effort visual
summaries added 3.980–14.031 seconds in these baseline trials; the summary model
is on the final drain path even though it runs in the background during actions.

The same Wi-Fi goal with Pro completed in 114.915 seconds in one trial (92,119
measured tokens). The other trial reached the Wi-Fi page but was cancelled at
its six-minute wall-clock deadline (359.223 session seconds, at least 257,442
measured tokens). It is **not** a successful end-to-end trial. Planner took
27.1–36.4 seconds across these runs; observed Operator model calls were roughly
9.5–18.2 seconds. The completed Pro run spent 44.9 seconds in its final Checker.

The slow Pro trial's Checker required historical evidence that nearby scan
results were visible at a completed subgoal, despite later screenshots showing
the results, and reopened final checking three times. This is a plan/verification
and inference tail, not a Decisions failure. No primary-to-Gemini switch was
observed in this comparison. Unknown tokens for interrupted in-flight calls are
not included. Two samples cannot establish Pro's general success rate.

## Process-local summary-effort experiment

To isolate the summary tail, a second process-local configuration changed only
`nodes.summarizer.reasoning_effort` to `medium`. The tracked configuration was
not edited and its original hash was verified unchanged. Two further trials per
variant used the same scroll goal:

| Summary effort / chooser | Verified | Session seconds | Median | Mean tokens |
| --- | --- | --- | --- | --- |
| medium / normal Flash | 2/2 | 20.070, 19.375 | 19.723 | 27,324 |
| medium / Decisions | 2/2 | 16.550, 17.017 | 16.784 | 19,026 |

The summary tail fell to 0.010–0.269 seconds. Compared with the original
max-summary baseline median, medium-summary normal Flash was 39.4% faster;
medium-summary plus Decisions was 48.4% faster. These small supplementary
samples support lowering Flash summary effort as the next experiment, **not** a
global Pro effort change or a summary-quality acceptance. The final production
configuration still uses max summaries. On medium-summary second normal Flash
trial the agent used explicit upward coordinates rather than a `direction`
argument; its trace and final hierarchy confirmed a single correct swipe.

Additional trace IDs: `3f41ed95-6535-4486-b185-fe5e47b5f552`,
`92d74303-230c-4e1b-9dab-fc161ad7d86d` (normal);
`c876812d-0228-4277-85c7-d7fbdf57ead5`,
`876a0a82-0a77-4a81-a8fe-9feaddf9d7f1` (Decisions).

## Boundaries and adoption

A separate exact `Open Wi-Fi settings` task used the **local rule**, finished in
20.295 seconds and used 19,366 total session tokens; its first action needed
0.603 seconds of validation and zero model tokens. It is not API speed evidence.

Ten additional real-API intent probes used a synthetic Settings UI and executed
no device action. Eight matched the expected choice/deferral; two supported
phrases safely deferred to Flash (downward finger swipe and mixed
navigate-then-scroll). No wrong accepted action was observed. This demonstrates
limited wording coverage, not broad semantic reliability.

Keep the pilot **off by default**, restricted to the first Settings action,
with existing Flash completion and the three-second fail-open boundary. A
scoped opt-in adapter is justified by the scroll result; navigation should remain
experimental. Do not replace Pro, verification, arbitrary app actions or global
model configuration with this classifier. Candidate filtering and multi-step
planning need separate acceptance before expansion.

Unit checks cover disabled mode, exact local routing, real tool/index reuse,
HTTP 402/403/429/503, timeout, low confidence, abstention, changed foreground,
stale bounds, malformed probability distributions, telemetry failures, and
transcript/token accounting. Related suite: 135 passed; Ruff and diff checks
passed. HTTP failure injection is unit coverage, not a real quota-exhaustion test.

Raw private local evidence: `local-verification/decisions-ab-20261112/analysis.json`
and `results.jsonl`; reproduction/analysis scripts:
`local-verification/decisions_ab.py`, `decisions_report.py`. These artifacts are
ignored by Git because UI hierarchies can contain local network names. Each row
contains the exact trace ID. Main Flash traces:

- Scroll baseline: `1284d72a-d2f9-43c5-9ab3-ff870daabbb0`,
  `d7f09def-9732-4c9d-bf21-bdbe3b3b03b7`, `0120569a-4193-4752-a759-5458e8530cbb`.
- Scroll Decisions: `26b2a1ef-93b8-47eb-ba68-297557f8a247`,
  `6e52bba9-af88-449f-95e1-926fc798d3bb`, `e1ce8679-62bd-4423-a65a-40384a7eb264`.
- Navigation baseline: `791f8a41-2085-49cd-9ccd-5d7cc14cf1e6`,
  `9728a127-e47b-4d2c-98b9-a14d37735c66`, `0f12db2b-29ed-491f-af54-dcdad88b1f7c`.
- Navigation Decisions: `f7226a00-41b3-4722-ab80-f4e41c57f541`,
  `becc40d7-badc-4df3-a5c7-8019a4e8a883`, `b98db745-9b31-47ba-9bcf-1438a25763e0`.
- Pro: `db91949c-d4b8-4969-8084-7ef5a267c29c` (deadline),
  `d9da2419-1cf5-4532-98d6-5dd30951821b` (completed).
