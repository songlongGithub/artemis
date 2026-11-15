# Personal fork maintenance

This fork keeps the local OpenAI-compatible model choices in `config/artemis.jsonc`.
Set the endpoint and its valid credential in the local `.env`; credentials are not
stored in Git. Provider-prefixed model names require the configured compatible
endpoint. Explicit optional roles inherit that provider, while omitted optional
roles retain their factory defaults.

Flash step summaries and history capsules use the operator provider for explicit
model names. Capsule fallback names must use the same provider. A provider or
client initialization error is surfaced instead of silently changing to Google.
Without an explicit Flash model, the configured summarizer agent role is used.
The inherited object detector model is a personal routing choice; select a
specialized grounding model when pixel precision is required.

Keep `origin` pointing to the personal fork and `upstream` pointing to the official
repository. `scripts/update_artemis.sh` fast-forwards the current branch from its
configured tracking branch. It refuses a dirty worktree or an unfinished Git
operation, and never stashes, rebases, or discards local customizations. Commit or
otherwise preserve local work before invoking it. Every update fetches the
tracked branch and runs `uv sync --dev --locked`, including retries after a
failed sync. The dependency check is inexpensive when nothing changed.

After reconciliation, tests are skipped only when the previous update passed
with the same commit, updater/test selection, Python environment, distribution
metadata, local `.env`, relevant process configuration and scrcpy receipt.
Receipts live in the worktree's Git metadata (`artemis-update-state.json`),
outside tracked files. Only a digest is saved, never credentials. Missing,
malformed, interrupted or failed receipts require validation; a failed attempt
invalidates even an older matching success. Updates to one checkout are locked
against concurrent updater runs. Dependency, native-build and test failures
remain failures. No script automatically pushes code or opens a pull request.

The updater also runs `build_scrcpy_recorder_fix.py --ensure --activate`.
Matching native inputs and intact installed client/server files reuse the
existing verified build. Changed patches or build recipe, legacy/missing
receipts, damaged binaries and host changes trigger a fresh build and all native
tests. Activation writes both client and server paths to local `.env`; unchanged
configuration is not rewritten. The launcher reads those paths before trying
its legacy default installation. Explicit process environment overrides still
take precedence. Existing running processes must be restarted to load updates;
the updater does not interrupt active mobile tasks or restart services.

For a fresh-process MCP readback, use the existing environment:

```sh
uv run --no-sync python scripts/check_local_mcp.py mobile_diagnose '{}'
```

This calls the named tool with the supplied JSON arguments; use only the intended
tool and arguments. Existing IDE MCP processes keep their imported modules until
reloaded. Local diagnostic records under `local-verification/` and dated config
backups stay local and are ignored by Git. Their original contents are preserved.

## Advisory review and final acceptance reports

`agent.pro.planner_validation.timeout_seconds` defaults to 20 seconds (greater
than zero, at most 120). The deadline covers retries, streaming and a single
local JSON repair. Optional plan review uses context-local non-pausing recovery:
exhaustion or invalid JSON returns `unavailable` with a reason; cancellation
propagates. It never creates the global pause file or fabricates approval.
The attempted content becomes the comparison baseline to avoid requesting the
same unavailable advice on every turn. Plan edits and actions remain applied;
deterministic plan/check protections are unchanged.

The settled `run_outcome.json` and append-only check ledger are authoritative for
acceptance. `task_status=completed` only means execution completed. Failed,
inconclusive or unchecked tests, or a partial/blocked task, produce a deterministic
final report saved to `notes/output.md`, without another LLM or structured-format
pass. This path returns text even when a custom output schema was requested;
consumers must not interpret it as a successfully formatted success object.
All-passed and no-outcome runs keep the existing synthesis path. Earlier notes
and raw traces are retained and are not rewritten into passing evidence.

For regression, compare exact failure identities against a frozen pre-change
baseline. Tests of timeout, JSON repair, pause isolation and final-report guards
use local fake providers and temporary trace directories, with no device, Broker
or weather request. Live smoke checks are separate, bounded and read-only; they
do not replace business acceptance or repair historical inconclusive results.
