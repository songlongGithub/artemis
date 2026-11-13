# Flash summary medium: production configuration acceptance, 2026-11-13

## Change and isolation

`agent.flash.step_summarizer.reasoning_effort` is now `medium` in the personal
configuration. Flash passes this optional value to its visual summary service
and its history-capsule lens. The shared summarizer role and global model config
remain `gpt-6-luna--fast` / `max`; Pro creates these services without the Flash
override. Fallback models keep their own effort. The new optional field defaults
to inheritance for installations without this configuration. Decisions remains
disabled by default, independent of summary effort.

No shared role mutation or new temporary model configuration was used. Live
factory readback confirmed Flash visual summary `medium` and Pro `max`.
Unit tests cover both composition roots, visual and capsule model construction,
fallback effort preservation, and passing the Flash value through the manager.

## Device results

Device: Infinix X6765, Android 17. Fresh standalone Artemis MCP processes loaded
the tracked production config, without a model config override or an explicit
Decisions flag. The same Settings goals as the prior comparison were reused.
ADB reopened the Settings homepage before each trial and read the final Activity;
all evaluated interactions ran through Artemis MCP. Busy tasks were respected.

| Profile / task | Session seconds | Measured total tokens | Summary drain seconds |
| --- | --- | --- | --- |
| Flash / scroll 1 | 20.883 | 27,273 | 0.099 |
| Flash / scroll 2 | 18.688 | 27,273 | 0.008 |
| Flash / Wi-Fi page 1 | 17.702 | 27,191 | 0.444 |
| Flash / Wi-Fi page 2 | 18.239 | 27,314 | 0.013 |
| Pro / scroll | 123.680 | 103,303 | not separately measured |

The four Flash trials all completed with exactly one requested device action
and the correct final page; scroll traces and final hierarchies confirmed upward
list displacement. Each trial's visual summary initialization log reported
`reasoning_effort=medium`. All visual summary calls returned usable summaries;
no critical stderr errors were found. No `decision` trace or Google Decisions
usage was present in any of the five sessions.

Flash scroll median was 19.786 seconds, versus 32.551 seconds in the previous
max-summary baseline (39.2% lower); Wi-Fi navigation median was 17.971 seconds,
versus 30.377 seconds (40.8% lower). These are small samples collected at different
times, not a newly interleaved controlled benchmark. The directly observed
summary drain was 0.008–0.444 seconds. Do not infer long-history quality from
these short tasks: capsule effort routing is unit-verified, and short device
runs do not trigger L2/L3 history compression.

The Pro trial reported completed, executed one correct upward swipe and stayed
on the Settings homepage. Its visual summary log reported `reasoning_effort=max`.
The Checker returned **2 passed, 1 inconclusive, 0 failed**: the on-complete
checkpoint lacked a historical post-action screenshot, even though the final
page and action record showed the scroll. Thus the Pro model/summary isolation
and device action are verified; a fully conclusive strict business acceptance
is not claimed. Planner took 24.98 seconds and final Checker 61.37 seconds.

## Validation and provenance

Related unit suite: **270 passed** with the existing dummy Google test credential
(`GOOGLE_API_KEY=unit-test-only`, no live Google calls). One broad invocation
initially omitted that fixture variable and failed during mock Google-client
construction; rerunning with the normal fixture passed. Ruff lint/format and
`git diff --check` passed.

Private raw measurements and action/hierarchy readbacks:
`local-verification/flash-summary-medium-20261113/analysis.json` and
`results.jsonl`. Reproduction:
`local-verification/flash_summary_medium_acceptance.py` and
`flash_summary_medium_report.py`. They remain ignored because UI hierarchies
can contain local network names.

Trace IDs:

- Flash scroll: `f7de6e25-2f8e-4f99-a95d-46d4440a192f`,
  `47ee3b07-7c31-48a5-8991-a78c07b31a23`.
- Flash navigation: `13014b38-a306-4883-a141-49c7cd90e6d1`,
  `95d7ddbd-8527-4692-a35e-0bde3c4faecb`.
- Pro isolation: `f9755367-19a2-480d-975f-201da5a9cefb`.
