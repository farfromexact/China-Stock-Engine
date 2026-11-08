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

    def test_retention_runs_before_collection_and_publication(self):
        first = self.workflow.index("python -m china_stock_engine.retention --check-capacity")
        self.assertLess(first, self.workflow.index("Collect iFinD A-share snapshot"))
        publication = self.workflow[self.workflow.index("Publish data to this repository atomically"):]
        self.assertIn("python -m china_stock_engine.retention --check-capacity", publication)
        self.assertIn("git add data/storage_status.json", publication)
        self.assertNotIn("du -sb", self.workflow)

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

    def test_supplemental_step_is_bounded_opt_in_and_before_report_build(self):
        self.assertIn("vars.IFIND_SUPPLEMENTAL_SCOPE != ''", self.workflow)
        self.assertIn('--scope "$SUPPLEMENTAL_SCOPE" --max-requests 12', self.workflow)
        self.assertLess(self.workflow.index("Collect bounded supplemental facts when enabled"),
                        self.workflow.index("Refresh compact data reference"))
        self.assertNotIn("--renew-access-token", self.workflow)

    def test_current_research_cutoff_is_explicit_after_optional_collection(self):
        refresh = self.workflow[self.workflow.index("Refresh compact data reference"):]
        self.assertIn("--research-asof", refresh)
        self.assertIn("--company-exposures config/company_commodity_exposures.json", refresh)
        self.assertIn("vars.IFIND_ENRICHMENT_ENABLED == 'true'", self.workflow)

    def test_technical_refresh_is_independent_of_candidates_and_market_success(self):
        validate = self.workflow.index("Validate promoted snapshot")
        refresh = self.workflow.index("Refresh technical adjustments before screening")
        supplemental = self.workflow.index("Collect bounded supplemental facts when enabled")
        self.assertLess(validate, refresh)
        self.assertLess(refresh, supplemental)
        step = self.workflow[refresh:supplemental]
        self.assertIn("steps.validate.outcome == 'success'", step)
        self.assertIn("continue-on-error: true", step)
        self.assertIn("refresh-technical-adjustments", step)
        self.assertIn("--research-asof", step)
        self.assertNotIn("candidate_union", step)


if __name__ == "__main__":
    unittest.main()
