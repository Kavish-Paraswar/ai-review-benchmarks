"""Read-only GitHub PR evidence collector using the standard library."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

API_ROOT = "https://api.github.com"
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class GitHubAPIError(RuntimeError):
    pass


def _next_link(header: str | None) -> str | None:
    if not header:
        return None
    for part in header.split(","):
        match = re.match(r'\s*<([^>]+)>;\s*rel="next"', part)
        if match:
            return match.group(1)
    return None


def _get_json(url: str, token: str) -> tuple[Any, str | None]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "reviewbench/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
            return payload, response.headers.get("Link")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        try:
            message = json.loads(detail).get("message", detail)
        except json.JSONDecodeError:
            message = detail
        raise GitHubAPIError(f"GitHub API returned HTTP {exc.code}: {message}") from exc
    except urllib.error.URLError as exc:
        raise GitHubAPIError(f"Could not reach GitHub API: {exc.reason}") from exc


def _get_all(url: str, token: str) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    next_url: str | None = url
    while next_url:
        payload, link = _get_json(next_url, token)
        if not isinstance(payload, list):
            raise GitHubAPIError(f"Expected a list from {next_url}")
        results.extend(payload)
        next_url = _next_link(link)
    return results


def fetch_pull_request_evidence(
    repository: str,
    pull_number: int,
    token: str,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    if not REPO_RE.fullmatch(repository):
        raise ValueError("Repository must be in owner/name form.")
    if pull_number < 1:
        raise ValueError("Pull request number must be positive.")
    if not token:
        raise ValueError("Set GITHUB_TOKEN or GH_TOKEN before capturing a PR.")
    base = f"{API_ROOT}/repos/{repository}"
    query = urllib.parse.urlencode({"per_page": 100})
    pull_request, _ = _get_json(f"{base}/pulls/{pull_number}", token)
    reviews = _get_all(f"{base}/pulls/{pull_number}/reviews?{query}", token)
    review_comments = _get_all(f"{base}/pulls/{pull_number}/comments?{query}", token)
    issue_comments = _get_all(f"{base}/issues/{pull_number}/comments?{query}", token)
    return pull_request, reviews, review_comments, issue_comments
