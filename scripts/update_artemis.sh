#!/usr/bin/env bash
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

set -euo pipefail

if [[ "${1:-}" == "--help" ]]; then
    echo "Usage: scripts/update_artemis.sh (clean working tree; fast-forward tracked branch)"
    exit 0
fi
if [[ "$#" -ne 0 ]]; then
    echo "Unexpected arguments; use --help." >&2
    exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_ROOT}"

# Never stash, discard, or rebase the caller's local customizations.
if [[ -n "$(git status --porcelain)" ]]; then
    echo "Working tree has local changes. Commit or preserve them before updating; nothing changed." >&2
    exit 2
fi
if [[ -d "$(git rev-parse --git-path rebase-merge)" || -d "$(git rev-parse --git-path rebase-apply)" || -f "$(git rev-parse --git-path MERGE_HEAD)" ]]; then
    echo "Finish the existing Git operation before updating." >&2
    exit 2
fi
BRANCH="$(git symbolic-ref --quiet --short HEAD)"
REMOTE="$(git config --get "branch.${BRANCH}.remote" || true)"
MERGE_REF="$(git config --get "branch.${BRANCH}.merge" || true)"
if [[ -z "${REMOTE}" || "${MERGE_REF}" != refs/heads/* ]]; then
    echo "The current branch must track a remote branch." >&2
    exit 2
fi
echo "Fetching ${REMOTE} ${MERGE_REF}..."
git fetch "${REMOTE}" "${MERGE_REF}"
git merge --ff-only FETCH_HEAD

if ! command -v uv >/dev/null 2>&1; then
    echo "uv is required for dependency checks and tests." >&2
    exit 1
fi
# Always reconcile the lock, including retries after a previous sync failed.
uv sync --dev --locked
uv run --locked --no-sync python -m pytest \
    tests/unit/mcp/test_diagnose_tool.py \
    tests/unit/config/test_optional_llm_roles.py \
    tests/unit/agents/test_local_model_routing.py \
    tests/unit/test_personal_fork_scripts.py

echo "Artemis tracked branch updated and focused checks passed."
