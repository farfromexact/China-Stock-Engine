from pathlib import Path
import unittest


WORKFLOW_PATH = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "daily.yml"


class DailyWorkflowContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow = WORKFLOW_PATH.read_text(encoding="utf-8")

    def test_verifies_this_repository_before_ifind_collection(self) -> None:
        gate = self.workflow.index("Verify this repository can retain data before collection")
        collection = self.workflow.index("Collect iFinD A-share snapshot")
        self.assertLess(gate, collection)
        self.assertIn(
            "contents: write",
            self.workflow,
        )
        self.assertIn(
            "git push --dry-run origin HEAD:main",
            self.workflow,
        )
        self.assertIn(
            'DATA_DIR="${GITHUB_WORKSPACE}/data"',
            self.workflow,
        )

    def test_never_uses_ephemeral_data_or_html_artifacts(self) -> None:
        self.assertNotIn("RUNNER_TEMP}/china-stock-engine-data", self.workflow)
        self.assertNotIn("actions/upload-artifact", self.workflow)
        self.assertNotIn("Build compact local data reference", self.workflow)
        self.assertNotIn("Upload ephemeral data reference", self.workflow)

    def test_publishes_back_to_this_repository(self) -> None:
        self.assertIn("Publish data to this repository atomically", self.workflow)
        self.assertIn('cd "${GITHUB_WORKSPACE}"', self.workflow)
        self.assertIn("git push origin HEAD:main", self.workflow)
        self.assertNotIn("China-Stock-Data", self.workflow)
        self.assertNotIn("IFIND_DATA_REPOSITORY", self.workflow)
        self.assertNotIn(".remote-stock-data", self.workflow)


if __name__ == "__main__":
    unittest.main()
