# AI Review Benchmarks

Build a system for comparing AI pull request reviewers, then use it to run distinct reviewers on the same changes. This repository contains the Proofline challenge starter, eight review cases, an AWS sample deployment, and a local evidence-tracking harness.

At Proofline, a useful review helps an engineer decide what to change before shipping. A reviewer might catch a real bug, warn about a problem the code already handles, or correctly say that it needs deployment information to tell. The benchmark should distinguish those outcomes and preserve the evidence behind each score.

## Candidate implementation

The reviewbench CLI records a frozen experiment protocol, provider metadata, planned provider–case runs, raw GitHub review evidence, manual grades, and a coverage report.

    python3 -m reviewbench --db results/reviewbench.sqlite init
    cp protocol.example.json protocol.json
    python3 -m reviewbench --db results/reviewbench.sqlite protocol set --file protocol.json
    python3 -m reviewbench --db results/reviewbench.sqlite protocol freeze
    python3 -m reviewbench --db results/reviewbench.sqlite cases

Start with the benchmark harness guide in docs/benchmark-harness.md. It explains how to isolate reviewers in separate private repositories, create eight review PRs per provider, capture reviews, grade findings, and report the denominators.

Do not install reviewer apps on this public fork. The challenge requires a private candidate working copy and a separate private review repository for each provider. The scripts directory contains the setup automation.

## What the benchmark should show

| Requirement | Evidence |
| --- | --- |
| Operable experiment | Frozen protocol, repeatable commands, provider registration, run history |
| Breadth of reviews | Provider-by-case coverage matrix, quota stops, errors, unattempted cases |
| Fair comparison | Same baseline, patch, context condition, review window, and pinned PR head |
| Traceable grading | Original GitHub review JSON, comment IDs, manual disposition, rationale |
| Working systems knowledge | CI result, AWS deployment workflow, verified invocation receipt |

## Run the starter report service

The starter application runs with Python 3.10+ and SQLite 3.35+:

    python3 console.py schedule "Weekly orders" 1700000000
    python3 console.py work --queue reports --now 1700000001
    python3 console.py summary
    python3 console.py history --limit 10
    python3 console.py export
    python3 console.py archive

See docs/report-service.md, cases/README.md, and docs/aws.md.

## Run checks

    python3 -m unittest discover -s tests -v
    python3 aws/deploy.py package --output /tmp/review-bench-stack.json
    python3 aws/deploy.py smoke --template /tmp/review-bench-stack.json

The AWS workflow is manually dispatched and requires AWS OIDC roles and repository variables. It does not run as part of the local smoke test.

## Submit

Keep the candidate harness, protocol, raw reviews, grading notes, and final report in a private working repository. Keep each provider's review repository separate. Open a PR in the private candidate repository with the implementation, rerun instructions, coverage table, and evidence-backed report. See Proofline's careers page at https://proofline.sh/careers/ for the application route.
