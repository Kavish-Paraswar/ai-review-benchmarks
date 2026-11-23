#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 <new-private-repository-name>" >&2
  echo "Example: $0 ai-review-benchmarks-private" >&2
  exit 2
fi

repo_name="$1"
if [[ ! "$repo_name" =~ ^[A-Za-z0-9_.-]{1,100}$ ]]; then
  echo "Repository name contains unsupported characters." >&2
  exit 2
fi

command -v git >/dev/null || { echo "git is required." >&2; exit 1; }
command -v gh >/dev/null || { echo "GitHub CLI (gh) is required." >&2; exit 1; }
gh auth status >/dev/null

if [[ -n "$(git status --porcelain)" ]]; then
  echo "Commit or stash workspace changes before creating the private copy." >&2
  exit 1
fi

owner="${owner}"
repo_full="${owner}/${repo_name}"
if gh repo view "$repo_full" >/dev/null 2>&1; then
  echo "${repo_full} already exists; refusing to overwrite it." >&2
  exit 1
fi

gh repo create "$repo_full" \
  --private \
  --source=. \
  --remote=private-candidate \
  --push \
  --include-all-branches \
  --description "Private candidate workspace for the Proofline AI review benchmark."

is_private="$(gh api "repos/${repo_full}" --jq .private)"
if [[ "$is_private" != "true" ]]; then
  echo "GitHub did not create a private repository; stop before adding review evidence." >&2
  exit 1
fi

echo "Created private candidate repository: https://github.com/${repo_full}"
echo "Keep protocol, raw reviews, grading notes, and results here, never in provider repositories."
