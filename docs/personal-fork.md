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
otherwise preserve local work before invoking it. Every update runs
`uv sync --dev --locked`, including retries after a failed sync; dependency and
test failures remain failures. No script
automatically pushes code or opens a pull request.

For a fresh-process MCP readback, use the existing environment:

```sh
uv run --no-sync python scripts/check_local_mcp.py mobile_diagnose '{}'
```

This calls the named tool with the supplied JSON arguments; use only the intended
tool and arguments. Existing IDE MCP processes keep their imported modules until
reloaded. Local diagnostic records under `local-verification/` and dated config
backups stay local and are ignored by Git. Their original contents are preserved.
