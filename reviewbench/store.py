"""SQLite persistence for reproducible AI-review benchmark runs."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
STATUSES = {"unattempted", "submitted", "completed", "quota", "error"}
DISPOSITIONS = {"correct", "duplicate", "unsupported", "needs-context", "not-a-finding"}
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS protocol (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    body_json TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    frozen_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS providers (
    id INTEGER PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    repository TEXT NOT NULL,
    plan TEXT,
    model TEXT,
    trigger_mode TEXT NOT NULL DEFAULT 'github-app',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    provider_id INTEGER NOT NULL REFERENCES providers(id) ON DELETE RESTRICT,
    case_id TEXT NOT NULL,
    context TEXT NOT NULL CHECK (context IN ('code-only', 'deployment-snapshot')),
    attempt INTEGER NOT NULL DEFAULT 1 CHECK (attempt > 0),
    status TEXT NOT NULL DEFAULT 'unattempted'
        CHECK (status IN ('unattempted', 'submitted', 'completed', 'quota', 'error')),
    protocol_sha TEXT NOT NULL,
    repository TEXT,
    pr_number INTEGER,
    pr_url TEXT,
    head_sha TEXT,
    base_sha TEXT,
    base_ref TEXT,
    started_at TEXT,
    completed_at TEXT,
    captured_at TEXT,
    retry_of INTEGER REFERENCES runs(id),
    error TEXT,
    raw_reviews_json TEXT NOT NULL DEFAULT '[]',
    raw_review_comments_json TEXT NOT NULL DEFAULT '[]',
    raw_issue_comments_json TEXT NOT NULL DEFAULT '[]',
    UNIQUE (provider_id, case_id, context, attempt)
);

CREATE TABLE IF NOT EXISTS grades (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    comment_id TEXT NOT NULL,
    disposition TEXT NOT NULL
        CHECK (disposition IN ('correct', 'duplicate', 'unsupported', 'needs-context', 'not-a-finding')),
    points REAL NOT NULL CHECK (points >= 0 AND points <= 1),
    rationale TEXT NOT NULL,
    grader TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (run_id, comment_id)
);

CREATE INDEX IF NOT EXISTS idx_runs_provider_case ON runs(provider_id, case_id, context);
CREATE INDEX IF NOT EXISTS idx_grades_run ON grades(run_id);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def connect(path: str | Path) -> sqlite3.Connection:
    db_path = Path(path)
    if str(db_path) != ":memory:":
        db_path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(db_path))
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.executescript(SCHEMA)
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        db.close()
        raise RuntimeError(f"Database schema {version} is newer than this CLI supports ({SCHEMA_VERSION}).")
    if version < SCHEMA_VERSION:
        db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        db.commit()
    return db


def set_protocol(db: sqlite3.Connection, body: dict[str, Any]) -> str:
    if not isinstance(body, dict):
        raise ValueError("Protocol must be a JSON object.")
    required = {"name", "baseline_commit", "case_ids", "contexts", "review_window_hours", "scoring"}
    missing = sorted(required - body.keys())
    if missing:
        raise ValueError("Protocol is missing required fields: " + ", ".join(missing))
    if not re.fullmatch(r"[0-9a-f]{40}", str(body["baseline_commit"])):
        raise ValueError("baseline_commit must be a 40-character Git commit SHA.")
    if not isinstance(body.get("name"), str) or not body["name"].strip():
        raise ValueError("name must be a non-empty string.")
    if not isinstance(body["case_ids"], list) or not body["case_ids"]:
        raise ValueError("case_ids must be a non-empty array.")
    if any(not isinstance(case_id, str) for case_id in body["case_ids"]):
        raise ValueError("case_ids must contain strings.")
    if len(set(body["case_ids"])) != len(body["case_ids"]) or any(
        not re.fullmatch(r"\d{2}", case_id) for case_id in body["case_ids"]
    ):
        raise ValueError("case_ids must be unique two-digit case IDs.")
    if not isinstance(body["contexts"], list) or not body["contexts"]:
        raise ValueError("contexts must be a non-empty array.")
    if any(not isinstance(context, str) for context in body["contexts"]):
        raise ValueError("contexts must contain strings.")
    if len(set(body["contexts"])) != len(body["contexts"]):
        raise ValueError("contexts must not contain duplicates.")
    if not isinstance(body.get("scoring"), dict):
        raise ValueError("scoring must be a JSON object.")
    if not set(body["contexts"]).issubset({"code-only", "deployment-snapshot"}):
        raise ValueError("contexts may contain only code-only and deployment-snapshot.")
    if not isinstance(body["review_window_hours"], (int, float)) or body["review_window_hours"] <= 0:
        raise ValueError("review_window_hours must be a positive number.")
    if db.execute("SELECT 1 FROM protocol WHERE id = 1 AND frozen_at IS NOT NULL").fetchone():
        raise ValueError("The protocol is frozen. Start a new database for a new experiment.")
    if db.execute("SELECT 1 FROM runs LIMIT 1").fetchone():
        raise ValueError("The protocol cannot change after runs have been planned.")
    encoded = canonical_json(body)
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    with db:
        db.execute(
            """INSERT INTO protocol(id, body_json, sha256, frozen_at, created_at)
               VALUES(1, ?, ?, NULL, ?)
               ON CONFLICT(id) DO UPDATE SET body_json=excluded.body_json,
                   sha256=excluded.sha256, frozen_at=NULL""",
            (encoded, digest, utc_now()),
        )
    return digest


def freeze_protocol(db: sqlite3.Connection) -> str:
    row = db.execute("SELECT sha256, frozen_at FROM protocol WHERE id = 1").fetchone()
    if row is None:
        raise ValueError("Set the protocol before freezing it.")
    if db.execute("SELECT 1 FROM runs LIMIT 1").fetchone():
        raise ValueError("Freeze the protocol before planning any runs.")
    with db:
        db.execute("UPDATE protocol SET frozen_at = COALESCE(frozen_at, ?) WHERE id = 1", (utc_now(),))
    return row["sha256"]


def get_protocol(db: sqlite3.Connection) -> dict[str, Any] | None:
    row = db.execute("SELECT body_json, sha256, frozen_at FROM protocol WHERE id = 1").fetchone()
    if row is None:
        return None
    result = json.loads(row["body_json"])
    result["_sha256"] = row["sha256"]
    result["_frozen_at"] = row["frozen_at"]
    return result


def add_provider(
    db: sqlite3.Connection,
    slug: str,
    name: str,
    repository: str,
    plan: str | None = None,
    model: str | None = None,
    trigger_mode: str = "github-app",
) -> int:
    if not SLUG_RE.fullmatch(slug):
        raise ValueError("Provider slug must use lowercase letters, digits, and hyphens (max 40 characters).")
    if not name.strip():
        raise ValueError("Provider name cannot be empty.")
    if not REPO_RE.fullmatch(repository):
        raise ValueError("Repository must be in owner/name form.")
    with db:
        cur = db.execute(
            """INSERT INTO providers(slug, name, repository, plan, model, trigger_mode, created_at)
               VALUES(?, ?, ?, ?, ?, ?, ?)""",
            (slug, name.strip(), repository, plan, model, trigger_mode, utc_now()),
        )
    return int(cur.lastrowid)


def list_providers(db: sqlite3.Connection) -> list[sqlite3.Row]:
    return db.execute("SELECT * FROM providers ORDER BY name COLLATE NOCASE").fetchall()


def plan_runs(
    db: sqlite3.Connection,
    provider_slug: str,
    case_ids: list[str],
    contexts: list[str],
    cases_root: str | Path,
) -> int:
    protocol = db.execute("SELECT sha256, frozen_at, body_json FROM protocol WHERE id = 1").fetchone()
    if protocol is None or protocol["frozen_at"] is None:
        raise ValueError("Set and freeze the experiment protocol before planning runs.")
    provider = db.execute("SELECT id FROM providers WHERE slug = ?", (provider_slug,)).fetchone()
    if provider is None:
        raise ValueError(f"Unknown provider: {provider_slug}")
    allowed_cases = set(json.loads(protocol["body_json"])["case_ids"])
    allowed_contexts = set(json.loads(protocol["body_json"])["contexts"])
    root = Path(cases_root)
    inserted = 0
    for case_id in case_ids:
        if case_id not in allowed_cases:
            raise ValueError(f"Case {case_id} is not included in the frozen protocol.")
        case_dir = root / case_id
        if not (case_dir / "title.txt").is_file() or not (case_dir / "change.patch").is_file():
            raise ValueError(f"Case {case_id} is missing title.txt or change.patch under {root}.")
        has_snapshot = (case_dir / "evidence.md").is_file()
        for context in contexts:
            if context not in allowed_contexts:
                raise ValueError(f"Context {context} is not included in the frozen protocol.")
            if context == "deployment-snapshot" and not has_snapshot:
                continue
            with db:
                cur = db.execute(
                    """INSERT OR IGNORE INTO runs(provider_id, case_id, context, attempt, status, protocol_sha)
                       VALUES(?, ?, ?, 1, 'unattempted', ?)""",
                    (provider["id"], case_id, context, protocol["sha256"]),
                )
            inserted += cur.rowcount
    return inserted


def repeat_run(db: sqlite3.Connection, run_id: int) -> int:
    source = get_run(db, run_id)
    attempt = db.execute(
        "SELECT COALESCE(MAX(attempt), 0) + 1 FROM runs WHERE provider_id=? AND case_id=? AND context=?",
        (source["provider_id"], source["case_id"], source["context"]),
    ).fetchone()[0]
    with db:
        cur = db.execute(
            """INSERT INTO runs(provider_id, case_id, context, attempt, status, protocol_sha, retry_of)
               VALUES(?, ?, ?, ?, 'unattempted', ?, ?)""",
            (source["provider_id"], source["case_id"], source["context"], attempt,
             source["protocol_sha"], run_id),
        )
    return int(cur.lastrowid)


def get_run(db: sqlite3.Connection, run_id: int) -> sqlite3.Row:
    row = db.execute(
        """SELECT r.*, p.slug AS provider_slug, p.name AS provider_name,
                  p.repository AS provider_repository
           FROM runs r JOIN providers p ON p.id = r.provider_id WHERE r.id = ?""",
        (run_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"Unknown run id: {run_id}")
    return row


def update_run(
    db: sqlite3.Connection,
    run_id: int,
    status: str,
    repository: str | None = None,
    pr_number: int | None = None,
    pr_url: str | None = None,
    head_sha: str | None = None,
    error: str | None = None,
    base_sha: str | None = None,
    base_ref: str | None = None,
) -> None:
    if status not in STATUSES:
        raise ValueError("Invalid run status.")
    if status == "completed":
        raise ValueError("A run becomes completed only after a GitHub review is captured.")
    run = get_run(db, run_id)
    started = run["started_at"] or (utc_now() if status in {"submitted", "completed"} else None)
    completed = utc_now() if status in {"completed", "quota", "error"} else None
    with db:
        db.execute(
            """UPDATE runs SET status=?, repository=COALESCE(?, repository),
               pr_number=COALESCE(?, pr_number), pr_url=COALESCE(?, pr_url),
               head_sha=COALESCE(?, head_sha), base_sha=COALESCE(?, base_sha),
               base_ref=COALESCE(?, base_ref), error=?, started_at=COALESCE(started_at, ?),
               completed_at=? WHERE id=?""",
            (status, repository, pr_number, pr_url, head_sha, base_sha, base_ref,
             error, started, completed, run_id),
        )


def save_capture(
    db: sqlite3.Connection,
    run_id: int,
    reviews: list[dict[str, Any]],
    review_comments: list[dict[str, Any]],
    issue_comments: list[dict[str, Any]],
    author: str | None = None,
    base_sha: str | None = None,
    base_ref: str | None = None,
) -> dict[str, int | bool]:
    run = get_run(db, run_id)
    if author:
        def authored(item: dict[str, Any]) -> bool:
            return ((item.get("user") or {}).get("login") or "").lower() == author.lower()
        provider_reviews = [
            item for item in reviews
            if authored(item) and str(item.get("state", "")).upper() in {"APPROVED", "CHANGES_REQUESTED", "COMMENTED"}
        ]
        provider_review_comments = [item for item in review_comments if authored(item)]
        provider_issue_comments = [item for item in issue_comments if authored(item)]
    else:
        provider_reviews = [
            item for item in reviews
            if str(item.get("state", "")).upper() in {"APPROVED", "CHANGES_REQUESTED", "COMMENTED"}
        ]
        provider_review_comments = review_comments
        provider_issue_comments = issue_comments
    has_review = bool(provider_reviews or provider_review_comments or provider_issue_comments)
    protocol = db.execute("SELECT body_json FROM protocol WHERE id = 1").fetchone()
    expected_base = json.loads(protocol["body_json"])["baseline_commit"] if protocol else None
    baseline_match = bool(base_sha and expected_base and base_sha == expected_base)
    if not baseline_match:
        final_status = "error"
        error_text = f"PR base SHA mismatch: expected {expected_base}, received {base_sha}."
    else:
        final_status = "completed" if has_review else "submitted"
        error_text = None
    now = utc_now()
    with db:
        db.execute(
            """UPDATE runs SET raw_reviews_json=?, raw_review_comments_json=?,
               raw_issue_comments_json=?, captured_at=?, status=?, completed_at=?,
               base_sha=COALESCE(?, base_sha), base_ref=COALESCE(?, base_ref),
               error=? WHERE id=?""",
            (
                canonical_json(reviews),
                canonical_json(review_comments),
                canonical_json(issue_comments),
                now,
                final_status,
                now if final_status in {"completed", "error"} else None,
                base_sha,
                base_ref,
                error_text,
                run_id,
            ),
        )
    return {
        "reviews": len(reviews),
        "review_comments": len(review_comments),
        "issue_comments": len(issue_comments),
        "provider_reviews": len(provider_reviews),
        "provider_comments": len(provider_review_comments) + len(provider_issue_comments),
        "completed": final_status == "completed",
        "baseline_match": baseline_match,
    }


def add_grade(
    db: sqlite3.Connection,
    run_id: int,
    comment_id: str,
    disposition: str,
    points: float,
    rationale: str,
    grader: str | None = None,
) -> None:
    get_run(db, run_id)
    if not str(comment_id).strip():
        raise ValueError("comment_id cannot be empty.")
    if disposition not in DISPOSITIONS:
        raise ValueError("Disposition must be one of: " + ", ".join(sorted(DISPOSITIONS)))
    if not 0 <= points <= 1:
        raise ValueError("Points must be between 0 and 1.")
    if not rationale.strip():
        raise ValueError("A rationale is required for every grade.")
    with db:
        db.execute(
            """INSERT INTO grades(run_id, comment_id, disposition, points, rationale, grader, created_at)
               VALUES(?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(run_id, comment_id) DO UPDATE SET
                 disposition=excluded.disposition, points=excluded.points,
                 rationale=excluded.rationale, grader=excluded.grader""",
            (run_id, str(comment_id), disposition, points, rationale.strip(), grader, utc_now()),
        )


def report_rows(db: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = db.execute(
        """SELECT p.slug, p.name AS provider, p.repository AS provider_repository,
                  r.case_id, r.context, r.attempt, r.status, r.repository, r.pr_number,
                  r.pr_url, r.base_ref, r.base_sha, r.head_sha, r.captured_at, r.error,
                  COUNT(g.id) AS graded_findings,
                  SUM(CASE WHEN g.disposition='correct' THEN 1 ELSE 0 END) AS correct,
                  SUM(CASE WHEN g.disposition='duplicate' THEN 1 ELSE 0 END) AS duplicate,
                  SUM(CASE WHEN g.disposition='unsupported' THEN 1 ELSE 0 END) AS unsupported,
                  SUM(CASE WHEN g.disposition='needs-context' THEN 1 ELSE 0 END) AS needs_context,
                  SUM(CASE WHEN g.disposition='not-a-finding' THEN 1 ELSE 0 END) AS not_a_finding,
                  COALESCE(SUM(g.points), 0) AS points
           FROM runs r JOIN providers p ON p.id = r.provider_id
           LEFT JOIN grades g ON g.run_id = r.id
           GROUP BY r.id ORDER BY p.name COLLATE NOCASE, r.context, r.case_id, r.attempt"""
    ).fetchall()
    return [dict(row) for row in rows]


def all_cases(cases_root: str | Path) -> list[dict[str, str]]:
    root = Path(cases_root)
    result = []
    for case_dir in sorted(root.iterdir()):
        if not case_dir.is_dir() or not re.fullmatch(r"\d{2}", case_dir.name):
            continue
        title = case_dir / "title.txt"
        patch = case_dir / "change.patch"
        if title.is_file() and patch.is_file():
            result.append({"id": case_dir.name, "title": title.read_text(encoding="utf-8").strip()})
    return result
