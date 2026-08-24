from pathlib import Path
import unittest


WORKFLOW_PATH = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "daily.yml"


class DailyWorkflowContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow = WORKFLOW_PATH.read_text(encoding="utf-8")

    def test_requires_remote_persistence_before_ifind_collection(self) -> None:
        gate = self.workflow.index("Require approved remote data persistence before collection")
        collection = self.workflow.index("Collect iFinD A-share snapshot")
        self.assertLess(gate, collection)
        self.assertIn(
            "IFIND_DATA_STORAGE_APPROVED=true is required before any iFinD collection.",
            self.workflow,
        )
        self.assertIn(
            "IFIND_DATA_REPOSITORY must name the remote repository that will retain the collected data.",
            self.workflow,
        )
        self.assertIn(
            "STOCK_DATA_REPO_TOKEN is required before any iFinD collection.",
            self.workflow,
        )

    def test_never_uses_ephemeral_data_or_html_artifacts(self) -> None:
        self.assertNotIn("RUNNER_TEMP}/china-stock-engine-data", self.workflow)
        self.assertNotIn("actions/upload-artifact", self.workflow)
        self.assertNotIn("Build compact local data reference", self.workflow)
        self.assertNotIn("Upload ephemeral data reference", self.workflow)

    def test_publishes_only_after_remote_repository_checkout(self) -> None:
        self.assertIn("id: data_repo", self.workflow)
        self.assertIn(
            "if: always() && steps.data_repo.outcome == 'success'", self.workflow
        )
        self.assertNotIn("China-Stock-Data", self.workflow)


if __name__ == "__main__":
    unittest.main()
