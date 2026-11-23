"""Command-line interface for the AI review benchmark."""

from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

from .github import fetch_pull_request_evidence
from .store import (
    DISPOSITIONS,
    STATUSES,
    add_grade,
    add_provider,
    all_cases,
    connect,
    freeze_protocol,
    get_protocol,
    get_run,
    list_providers,
    plan_runs,
    report_rows,
    repeat_run,
    save_capture,
    set_protocol,
    update_run,
)


def _json_default(value: Any) -> str:
    return str(value)


def _markdown_report(db, cases_root: Path) -> str:
    rows = report_rows(db)
    providers = list_providers(db)
    cases = all_cases(cases_root)
    contexts = sorted({row["context"] for row in rows})
    if not rows:
        return "# AI review benchmark report\n\nNo runs have been planned yet.\n"
    completed_attempts = sum(row["status"] == "completed" for row in rows)
    completed_pairs = {
        (row["slug"], row["case_id"], row["context"])
        for row in rows if row["status"] == "completed"
    }
    active_providers = {row["slug"] for row in rows if row["status"] == "completed"}
    completed_cases = {row["case_id"] for row in rows if row["status"] == "completed"}
    repeats = sum(1 for row in rows if row["attempt"] > 1)
    out = [
        "# AI review benchmark report",
        "",
        f"- Providers with at least one captured review: **{len(active_providers)}**",
        f"- Distinct cases with a captured review: **{len(completed_cases)}** / {len(cases)}",
        f"- Completed provider–case–context pairs: **{len(completed_pairs)}**",
        f"- Completed review attempts: **{completed_attempts}**",
        f"- Repeat attempts: **{repeats}**",
        "",
        "## Coverage matrix",
        "",
    ]
    for context in contexts:
        out.extend([f"### {context}", ""])
        out.append("| Provider | " + " | ".join(c["id"] for c in cases) + " | Completed | Quota | Errors | Pending | Unattempted |")
        out.append("|---|" + "|".join("---:" for _ in cases) + "|---:|---:|---:|---:|---:|")
        for provider in providers:
            provider_rows = [r for r in rows if r["slug"] == provider["slug"] and r["context"] == context]
            if not provider_rows:
                continue
            cells = []
            for case in cases:
                matching = [r for r in provider_rows if r["case_id"] == case["id"]]
                statuses = [r["status"] for r in matching]
                completed = statuses.count("completed")
                if completed:
                    cell = f"done ×{completed}" if completed > 1 else "done"
                elif "quota" in statuses:
                    cell = "quota"
                elif "error" in statuses:
                    cell = "error"
                elif "submitted" in statuses:
                    cell = "pending"
                else:
                    cell = "—"
                cells.append(cell)
            completed = sum(r["status"] == "completed" for r in provider_rows)
            quota = sum(r["status"] == "quota" for r in provider_rows)
            errors = sum(r["status"] == "error" for r in provider_rows)
            pending = sum(r["status"] == "submitted" for r in provider_rows)
            unattempted = sum(r["status"] == "unattempted" for r in provider_rows)
            out.append(
                f"| {provider['name']} | " + " | ".join(cells)
                + f" | {completed} | {quota} | {errors} | {pending} | {unattempted} |"
            )
        out.append("")
    out.extend([
        "## Grading summary",
        "",
        "Grades are manual and trace to the original comment ID. Points are assigned per finding; "
        "they are not a recall estimate unless the expected-finding denominator was independently established.",
        "",
        "| Provider | Context | Correct | Duplicate | Unsupported | Needs context | Not a finding | Points |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ])
    aggregates: dict[tuple[str, str], dict[str, float]] = {}
    for row in rows:
        key = (row["provider"], row["context"])
        agg = aggregates.setdefault(key, {
            "correct": 0, "duplicate": 0, "unsupported": 0,
            "needs_context": 0, "not_a_finding": 0, "points": 0,
        })
        for field in ("correct", "duplicate", "unsupported", "needs_context", "not_a_finding", "points"):
            agg[field] += row[field] or 0
    for (provider, context), agg in sorted(aggregates.items()):
        out.append(
            f"| {provider} | {context} | {int(agg['correct'])} | {int(agg['duplicate'])} "
            f"| {int(agg['unsupported'])} | {int(agg['needs_context'])} "
            f"| {int(agg['not_a_finding'])} | {agg['points']:.2f} |"
        )
    out.extend([
        "",
        "## Interpretation notes",
        "",
        "- A provider counts toward coverage only after a review from that provider was captured.",
        "- Quota stops, errors, pending submissions, and unattempted cases remain separate outcomes.",
        "- Compare providers on shared cases and the same context condition. Report repeats separately.",
        "- Cases 04–06 have deployment snapshots; compare code-only and snapshot runs as separate conditions.",
        "- The harness stores raw GitHub reviews and comments. Preserve the raw records when grading or disputing a finding.",
        "",
    ])
    return "\n".join(out)


def _csv_report(db) -> str:
    rows = report_rows(db)
    fields = [
        "slug", "provider", "provider_repository", "case_id", "context", "attempt",
        "status", "repository", "pr_number", "pr_url", "base_ref", "base_sha",
        "head_sha", "captured_at", "error", "graded_findings", "correct", "duplicate",
        "unsupported", "needs_context", "not_a_finding", "points",
    ]
    from io import StringIO
    stream = StringIO()
    writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return stream.getvalue()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="reviewbench", description=__doc__)
    parser.add_argument("--db", default="reviewbench.sqlite", help="SQLite database path (default: reviewbench.sqlite)")
    parser.add_argument("--cases-root", default="cases", help="Case directory (default: ./cases)")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("init", help="Create the local benchmark database.")

    protocol = commands.add_parser("protocol", help="Set, freeze, or inspect the experiment protocol.")
    protocol_sub = protocol.add_subparsers(dest="protocol_command", required=True)
    protocol_set = protocol_sub.add_parser("set")
    protocol_set.add_argument("--file", required=True, help="Protocol JSON file.")
    protocol_sub.add_parser("freeze")
    protocol_sub.add_parser("show")

    provider = commands.add_parser("provider", help="Register or list reviewers.")
    provider_sub = provider.add_subparsers(dest="provider_command", required=True)
    provider_add = provider_sub.add_parser("add")
    provider_add.add_argument("--slug", required=True)
    provider_add.add_argument("--name", required=True)
    provider_add.add_argument("--repo", required=True, help="Private reviewer repository in owner/name form.")
    provider_add.add_argument("--plan")
    provider_add.add_argument("--model")
    provider_add.add_argument("--trigger", default="github-app")
    provider_sub.add_parser("list")

    plan = commands.add_parser("plan", help="Create one planned run per provider/case/context.")
    plan.add_argument("--provider", required=True)
    plan.add_argument("--case", action="append", dest="cases", help="Case ID; repeat to select multiple.")
    plan.add_argument(
        "--context", action="append", dest="contexts",
        choices=["code-only", "deployment-snapshot"],
        help="Context condition; repeat to select multiple. Defaults to code-only.",
    )

    run = commands.add_parser("run", help="Update or inspect a planned run.")
    run_sub = run.add_subparsers(dest="run_command", required=True)
    run_update = run_sub.add_parser("update")
    run_update.add_argument("--id", required=True, type=int)
    run_update.add_argument("--status", required=True, choices=sorted(STATUSES - {"completed"}))
    run_update.add_argument("--repo")
    run_update.add_argument("--pr", type=int)
    run_update.add_argument("--pr-url")
    run_update.add_argument("--head-sha")
    run_update.add_argument("--error")
    run_show = run_sub.add_parser("show")
    run_show.add_argument("--id", required=True, type=int)
    run_repeat = run_sub.add_parser("repeat")
    run_repeat.add_argument("--of", required=True, type=int, help="Original run ID to repeat.")

    capture = commands.add_parser("capture", help="Capture reviews and comments from a GitHub PR.")
    capture.add_argument("--run-id", required=True, type=int)
    capture.add_argument("--repo", help="Defaults to the registered provider repository.")
    capture.add_argument("--pr", type=int, help="Defaults to the PR number already stored on the run.")
    capture.add_argument("--author", help="Optional exact GitHub login of the reviewer bot.")

    grade = commands.add_parser("grade", help="Grade one captured review comment.")
    grade.add_argument("--run-id", required=True, type=int)
    grade.add_argument("--comment-id", required=True)
    grade.add_argument("--disposition", required=True, choices=sorted(DISPOSITIONS))
    grade.add_argument("--points", required=True, type=float)
    grade.add_argument("--rationale", required=True)
    grade.add_argument("--grader")

    report = commands.add_parser("report", help="Render coverage and grading summaries.")
    report.add_argument("--format", choices=["markdown", "csv"], default="markdown")
    report.add_argument("--out", help="Write the report to a file instead of stdout.")

    cases = commands.add_parser("cases", help="List the supplied review cases.")
    cases.set_defaults()

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    db = connect(args.db)
    cases_root = Path(args.cases_root)

    try:
        if args.command == "init":
            print(f"Initialized {args.db}")
        elif args.command == "protocol":
            if args.protocol_command == "set":
                body = json.loads(Path(args.file).read_text(encoding="utf-8"))
                print(f"Protocol SHA-256: {set_protocol(db, body)}")
            elif args.protocol_command == "freeze":
                print(f"Frozen protocol SHA-256: {freeze_protocol(db)}")
            else:
                protocol = get_protocol(db)
                print(json.dumps(protocol, indent=2) if protocol else "No protocol configured.")
        elif args.command == "provider":
            if args.provider_command == "add":
                provider_id = add_provider(db, args.slug, args.name, args.repo, args.plan, args.model, args.trigger)
                print(f"Registered provider {args.slug} (id={provider_id}).")
            else:
                for provider in list_providers(db):
                    print(f"{provider['slug']}\t{provider['name']}\t{provider['repository']}\t{provider['plan'] or ''}")
        elif args.command == "plan":
            protocol = get_protocol(db)
            if protocol is None:
                raise ValueError("Configure the protocol first.")
            selected_cases = args.cases or protocol["case_ids"]
            selected_contexts = args.contexts or ["code-only"]
            inserted = plan_runs(db, args.provider, selected_cases, selected_contexts, cases_root)
            print(f"Planned {inserted} new run(s) for {args.provider}.")
        elif args.command == "run":
            if args.run_command == "update":
                update_run(db, args.id, args.status, args.repo, args.pr, args.pr_url, args.head_sha, args.error)
                print(f"Updated run {args.id}.")
            elif args.run_command == "repeat":
                new_id = repeat_run(db, args.of)
                print(f"Created repeat run {new_id} from run {args.of}.")
            else:
                row = dict(get_run(db, args.id))
                for key in ("raw_reviews_json", "raw_review_comments_json", "raw_issue_comments_json"):
                    row[key] = json.loads(row[key])
                print(json.dumps(row, indent=2, default=_json_default))
        elif args.command == "capture":
            run_row = get_run(db, args.run_id)
            repo = args.repo or run_row["repository"] or run_row["provider_repository"]
            pr_number = args.pr or run_row["pr_number"]
            if not repo or not pr_number:
                raise ValueError("Provide --repo and --pr, or store them with 'run update' first.")
            token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
            pull_request, reviews, review_comments, issue_comments = fetch_pull_request_evidence(
                repo, pr_number, token or ""
            )
            update_run(
                db, args.run_id, "submitted", repository=repo, pr_number=pr_number,
                pr_url=pull_request.get("html_url") or f"https://github.com/{repo}/pull/{pr_number}",
                head_sha=((pull_request.get("head") or {}).get("sha")),
                base_sha=((pull_request.get("base") or {}).get("sha")),
                base_ref=((pull_request.get("base") or {}).get("ref")),
            )
            summary = save_capture(
                db, args.run_id, reviews, review_comments, issue_comments,
                author=args.author,
                base_sha=((pull_request.get("base") or {}).get("sha")),
                base_ref=((pull_request.get("base") or {}).get("ref")),
            )
            print(json.dumps(summary, indent=2))
        elif args.command == "grade":
            add_grade(db, args.run_id, args.comment_id, args.disposition, args.points, args.rationale, args.grader)
            print(f"Saved grade for run {args.run_id}, comment {args.comment_id}.")
        elif args.command == "report":
            content = _markdown_report(db, cases_root) if args.format == "markdown" else _csv_report(db)
            if args.out:
                Path(args.out).write_text(content, encoding="utf-8")
                print(f"Wrote {args.out}")
            else:
                sys.stdout.write(content)
        elif args.command == "cases":
            for case in all_cases(cases_root):
                has_snapshot = (cases_root / case["id"] / "evidence.md").is_file()
                print(f"{case['id']}\t{case['title']}\t{'snapshot' if has_snapshot else 'code-only'}")
        return 0
    except (ValueError, OSError, sqlite3.Error, json.JSONDecodeError) as exc:
        parser.exit(2, f"reviewbench: error: {exc}\n")
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
