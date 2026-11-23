#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 <provider-slug>" >&2
  echo "Example: $0 coderabbit" >&2
  exit 2
fi

slug="$1"
if [[ ! "$slug" =~ ^[a-z0-9][a-z0-9-]{0,39}$ ]]; then
  echo "Provider slug must use lowercase letters, digits, and hyphens." >&2
  exit 2
fi

command -v git >/dev/null || { echo "git is required." >&2; exit 1; }
command -v gh >/dev/null || { echo "GitHub CLI (gh) is required." >&2; exit 1; }
gh auth status >/dev/null

owner="$(gh api user --jq .login)"
repo_name="review-bench-${slug}"
repo_full="${owner}/${repo_name}"
description="Private, isolated AI review benchmark repository for ${slug}."

if gh repo view "$repo_full" >/dev/null 2>&1; then
  is_private="$(gh api "repos/${repo_full}" --jq .private)"
  if [[ "$is_private" != "true" ]]; then
    echo "${repo_full} exists but is not private; refusing to use it." >&2
    exit 1
  fi
  echo "Using existing private repository ${repo_full}."
else
  gh api --method POST /user/repos \
    -f name="$repo_name" \
    -F private=true \
    -f description="$description" \
    -F has_issues=false \
    -F has_projects=false \
    -F has_wiki=false >/dev/null
  echo "Created private repository ${repo_full}."
fi

tmp="$(mktemp -d)"
cleanup() { rm -rf "$tmp"; }
trap cleanup EXIT

git clone --filter=blob:none --no-checkout \
  https://github.com/prooflinedev/ai-review-benchmarks.git "$tmp/source"
git -C "$tmp/source" fetch origin \
  '+refs/heads/pilot/*:refs/remotes/upstream/pilot/*'

git -C "$tmp/source" remote add provider "https://github.com/${repo_full}.git"
for branch in base 01 02 03 04 05 06 07 08; do
  git -C "$tmp/source" push provider \
    "refs/remotes/upstream/pilot/${branch}:refs/heads/pilot/${branch}"
done

for case_id in 01 02 03 04 05 06 07 08; do
  title="$(git -C "$tmp/source" show "origin/main:cases/${case_id}/title.txt" | tr -d '\r\n')"
  body="$(git -C "$tmp/source" show "origin/main:cases/${case_id}/body.md")"
  existing="$(gh pr list --repo "$repo_full" --state all --head "pilot/${case_id}" --base pilot/base --json number --jq 'length')"
  if [[ "$existing" == "0" ]]; then
    gh api --method POST "repos/${repo_full}/pulls" \
      -f title="$title" \
      -f head="pilot/${case_id}" \
      -f base="pilot/base" \
      -f body="$body" >/dev/null
  else
    echo "PR for case ${case_id} already exists in ${repo_full}; skipping."
  fi
done

cat <<EOF

Ready: https://github.com/${repo_full}

Next:
1. Install/enable the ${slug} reviewer GitHub App on this private repository only.
2. Confirm the app can review private pull requests.
3. Wait for its review on each of the eight PRs.
4. Register this repository with reviewbench and capture the reviews.

Do not reuse this repository for another provider. Each provider needs its own isolated private repo.
EOF
