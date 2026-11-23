import tempfile
import unittest
from pathlib import Path

from reviewbench.store import (
    add_grade,
    add_provider,
    connect,
    freeze_protocol,
    get_run,
    plan_runs,
    repeat_run,
    report_rows,
    save_capture,
    set_protocol,
    update_run,
)


class ReviewBenchStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cases = self.root / "cases"
        for case_id in ("01", "04"):
            case_dir = self.cases / case_id
            case_dir.mkdir(parents=True)
            (case_dir / "title.txt").write_text(f"Case {case_id}\n", encoding="utf-8")
            (case_dir / "change.patch").write_text("diff --git a/x b/x\n", encoding="utf-8")
        (self.cases / "04" / "evidence.md").write_text("snapshot\n", encoding="utf-8")
        self.db = connect(":memory:")
        self.protocol = {
            "name": "test",
            "baseline_commit": "a" * 40,
            "case_ids": ["01", "04"],
            "contexts": ["code-only", "deployment-snapshot"],
            "review_window_hours": 24,
            "scoring": {"correct": 1, "unsupported": 0},
        }
        set_protocol(self.db, self.protocol)
        freeze_protocol(self.db)
        self.provider_id = add_provider(
            self.db, "reviewer-one", "Reviewer One", "owner/review-bench-reviewer-one"
        )

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_plan_is_idempotent_and_snapshot_only_for_supported_cases(self):
        inserted = plan_runs(
            self.db, "reviewer-one", ["01", "04"],
            ["code-only", "deployment-snapshot"], self.cases
        )
        self.assertEqual(inserted, 3)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 3)
        repeated = plan_runs(
            self.db, "reviewer-one", ["01", "04"],
            ["code-only", "deployment-snapshot"], self.cases
        )
        self.assertEqual(repeated, 0)

    def test_protocol_cannot_change_after_freeze(self):
        with self.assertRaisesRegex(ValueError, "frozen"):
            set_protocol(self.db, self.protocol)

    def test_review_capture_requires_matching_author_when_supplied(self):
        plan_runs(self.db, "reviewer-one", ["01"], ["code-only"], self.cases)
        run_id = self.db.execute("SELECT id FROM runs").fetchone()[0]
        unrelated = [{"id": 1, "user": {"login": "human"}, "body": "hello"}]
        summary = save_capture(
            self.db, run_id, [], [], unrelated, author="reviewer[bot]",
            base_sha="a" * 40, base_ref="pilot/base"
        )
        self.assertFalse(summary["completed"])
        self.assertEqual(get_run(self.db, run_id)["status"], "submitted")

        provider_review = [{"id": 2, "state": "COMMENTED", "user": {"login": "reviewer[bot]"}}]
        summary = save_capture(
            self.db, run_id, provider_review, [], [], author="reviewer[bot]",
            base_sha="a" * 40, base_ref="pilot/base"
        )
        self.assertTrue(summary["completed"])
        self.assertEqual(get_run(self.db, run_id)["status"], "completed")

    def test_capture_rejects_a_pr_based_on_the_wrong_commit(self):
        plan_runs(self.db, "reviewer-one", ["01"], ["code-only"], self.cases)
        run_id = self.db.execute("SELECT id FROM runs").fetchone()[0]
        review = [{"id": 2, "state": "COMMENTED", "user": {"login": "reviewer[bot]"}}]
        summary = save_capture(
            self.db, run_id, review, [], [], author="reviewer[bot]",
            base_sha="b" * 40, base_ref="pilot/base"
        )
        self.assertFalse(summary["completed"])
        self.assertFalse(summary["baseline_match"])
        self.assertEqual(get_run(self.db, run_id)["status"], "error")

    def test_completed_cannot_be_marked_without_captured_evidence(self):
        plan_runs(self.db, "reviewer-one", ["01"], ["code-only"], self.cases)
        run_id = self.db.execute("SELECT id FROM runs").fetchone()[0]
        with self.assertRaisesRegex(ValueError, "captured"):
            update_run(self.db, run_id, "completed")

    def test_repeat_is_a_distinct_attempt_linked_to_original(self):
        plan_runs(self.db, "reviewer-one", ["01"], ["code-only"], self.cases)
        original = self.db.execute("SELECT id FROM runs").fetchone()[0]
        repeated = repeat_run(self.db, original)
        row = get_run(self.db, repeated)
        self.assertEqual(row["attempt"], 2)
        self.assertEqual(row["retry_of"], original)

    def test_grades_are_auditable_and_replace_by_comment_id(self):
        plan_runs(self.db, "reviewer-one", ["01"], ["code-only"], self.cases)
        run_id = self.db.execute("SELECT id FROM runs").fetchone()[0]
        add_grade(self.db, run_id, "comment-1", "correct", 1, "Confirmed by reproducer.", "grader")
        add_grade(self.db, run_id, "comment-1", "needs-context", 0.5, "Needs deployment evidence.", "grader")
        row = report_rows(self.db)[0]
        self.assertEqual(row["graded_findings"], 1)
        self.assertEqual(row["needs_context"], 1)
        self.assertEqual(row["points"], 0.5)


if __name__ == "__main__":
    unittest.main()
