# Running the AI review benchmark

This harness records the experiment protocol, provider coverage, review attempts, raw GitHub review evidence, and manual grades. It deliberately does not put expected findings or answer keys in provider repositories.

## Keep the repositories isolated

The review cases include deployment snapshots and the benchmark contains material that can reveal expected outcomes. Do not install a reviewer app on this public fork. After the implementation is committed and merged, create a separate private candidate copy from a clean Codespace checkout:

    bash scripts/create_private_candidate_copy.sh ai-review-benchmarks-private

Keep the protocol, raw reviews, grading notes, and final report in that private candidate repository. Create one separate private review repository per provider using scripts/setup_provider_repo.sh. The script copies only the upstream pilot/base and pilot/01–pilot/08 branches into the provider repository and opens eight PRs against pilot/base. The provider repository does not contain the case catalog, answer keys, or grading notes.

Run the setup script once per provider:

    bash scripts/setup_provider_repo.sh coderabbit
    bash scripts/setup_provider_repo.sh greptile

Each invocation creates OWNER/review-bench-SLUG as a private repository. Install only that provider's GitHub App on its own repository. Confirm that its plan permits private-repository reviews before running the experiment. A provider counts toward coverage only after an actual review has been captured.

## Initialize the experiment

Python 3.10+ is required. The CLI uses only the Python standard library.

    python3 -m reviewbench --db results/reviewbench.sqlite init
    cp protocol.example.json protocol.json
    python3 -m reviewbench --db results/reviewbench.sqlite protocol set --file protocol.json
    python3 -m reviewbench --db results/reviewbench.sqlite protocol freeze

The example protocol pins the app-only baseline commit 9b5ac64ab0d3ba414c5edec1e64fcdf8b3b19a1a. Edit the protocol before freezing if you change the batch, context conditions, or review window. Once frozen, the CLI refuses protocol edits and stamps every planned run with the protocol hash. Start a new database for a materially different experiment.

Register a reviewer and plan its runs:

    python3 -m reviewbench --db results/reviewbench.sqlite provider add --slug coderabbit --name "CodeRabbit" --repo OWNER/review-bench-coderabbit --plan "Free/trial" --trigger "GitHub App"
    python3 -m reviewbench --db results/reviewbench.sqlite plan --provider coderabbit --context code-only

The eight cases are planned as independent attempts. For snapshot comparisons, plan only cases 04–06:

    python3 -m reviewbench --db results/reviewbench.sqlite plan --provider coderabbit --case 04 --case 05 --case 06 --context deployment-snapshot

The deployment-snapshot condition is available only for cases 04, 05, and 06. Do not mix it with code-only results in the same comparison.

## Capture real reviews

For each provider PR, record its PR number and mark the run as submitted:

    python3 -m reviewbench --db results/reviewbench.sqlite run update --id RUN_ID --status submitted --repo OWNER/review-bench-coderabbit --pr PR_NUMBER

Set a GitHub token with read access to the provider repository, then capture the PR reviews and comments. The author filter is recommended so unrelated human comments do not count as provider output.

    export GITHUB_TOKEN=github_pat_...
    python3 -m reviewbench --db results/reviewbench.sqlite capture --run-id RUN_ID --author coderabbitai[bot]

The collector reads PR metadata, review submissions, inline review comments, and conversation comments through GitHub's API. It stores the original JSON in the local SQLite database and records the PR base ref/SHA and head SHA. A PR whose base SHA differs from the frozen protocol is marked as an error and does not count toward completed coverage. The collector is read-only: it does not post comments, trigger reviewers, or modify provider repositories. If a provider uses a different bot login, pass that exact login. Omit the author filter only after checking that all comments on the PR belong to the reviewer.

Record quota stops and errors explicitly:

    python3 -m reviewbench --db results/reviewbench.sqlite run update --id RUN_ID --status quota --error "Free review allowance exhausted"
    python3 -m reviewbench --db results/reviewbench.sqlite run update --id RUN_ID --status error --error "App did not review private PR"

A repeat is a new attempt with its own identifier and a link to the original run:

    python3 -m reviewbench --db results/reviewbench.sqlite run repeat --of RUN_ID

## Grade and report

Grade each distinct claim in the raw review. Keep the rationale short and evidence-based:

    python3 -m reviewbench --db results/reviewbench.sqlite grade --run-id RUN_ID --comment-id COMMENT_ID --disposition correct --points 1 --rationale "Confirmed against the pinned patch and baseline."

Allowed dispositions are correct, duplicate, unsupported, needs-context, and not-a-finding. Points are assigned manually from 0 to 1. Record the reasoning for every grade. For ambiguous findings, hide provider identity where practical and ask a second reader to adjudicate.

Generate the coverage matrix and CSV:

    python3 -m reviewbench --db results/reviewbench.sqlite report
    python3 -m reviewbench --db results/reviewbench.sqlite report --format csv --out results/attempts.csv

The Markdown report separates completed reviews, quota stops, errors, pending attempts, and unattempted cases. It reports distinct providers, cases, provider–case–context pairs, and repeats. It does not claim recall: recall requires a separately established expected-finding denominator. Keep the raw database and exported reports out of provider repositories.

## Context and evidence boundaries

- Cases 01–03 and 07–08 are code-only changes.
- Cases 04–06 include optional deployment snapshots. Supply the same snapshot to every provider in a snapshot comparison.
- The live AWS report-service invocation is a separate operational observation. It is not evidence about the synthetic deployment inventories in cases 04–06.
- Do not expose evidence files, expected findings, prior reviews, or grader notes to reviewers.

Keep the baseline commit, PR head SHA, protocol hash, provider plan, visible model/settings, trigger mode, timestamps, and original review data. The database and exported review records may contain private code or comments. Keep results local or in the private candidate repository, never in a provider review repository.

The harness does not install GitHub Apps, trigger provider reviews, or deploy AWS resources. Provider installations, plan eligibility, review triggers, and AWS credentials must be configured by the account owner.
