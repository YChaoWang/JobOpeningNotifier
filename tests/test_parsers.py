"""Parser and normalization unit tests."""

from __future__ import annotations

import unittest

from internship_monitor.filtering import filter_jobs
from internship_monitor.models import FilterConfig, Sponsorship
from internship_monitor.normalization import make_job_id, normalize_url
from internship_monitor.parsers.ai_fallback import parse_ai_jobs
from internship_monitor.parsers.html_table import HtmlTableParser
from internship_monitor.parsers.markdown_table import MarkdownTableParser
from tests.conftest import read_fixture, sample_repo


class MarkdownParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = MarkdownTableParser()
        self.repo = sample_repo("summer-2027", "sndsh404/summer-2027-internships")

    def test_markdown_summer_2027_style(self) -> None:
        result = self.parser.parse(read_fixture("markdown_summer_2027.md"), self.repo)
        self.assertTrue(result.table_detected)
        self.assertTrue(result.columns_mapped)
        self.assertGreaterEqual(result.confidence, 0.8)
        roles = {(job.company, job.role) for job in result.jobs}
        self.assertIn(("Google", "Software Engineering Intern, BS (Summer 2027)"), roles)
        self.assertIn(("Acme", "Machine Learning Engineer Intern"), roles)
        self.assertIn(("Acme", "Computer Vision Intern"), roles)

        google = next(job for job in result.jobs if job.company == "Google")
        self.assertIn("Mountain View", google.location)
        self.assertIn("Sunnyvale", google.location)
        self.assertIsNotNone(google.apply_url)
        self.assertNotIn("utm_source", google.apply_url or "")

        closed = next(job for job in result.jobs if job.company == "Salesforce")
        self.assertTrue(closed.closed)

        nosponsor = next(job for job in result.jobs if job.company == "Circleback")
        self.assertEqual(nosponsor.sponsorship, Sponsorship.UNAVAILABLE)

        citizen = next(job for job in result.jobs if job.company == "Anduril")
        self.assertEqual(citizen.sponsorship, Sponsorship.CITIZENSHIP_REQUIRED)

    def test_alternate_column_names(self) -> None:
        result = self.parser.parse(
            read_fixture("markdown_alternate_headers.md"), self.repo
        )
        self.assertEqual(len(result.jobs), 2)
        self.assertEqual(result.jobs[0].company, "Contoso")
        self.assertEqual(result.jobs[1].role, "Artificial Intelligence Intern")
        self.assertNotIn("utm_campaign", result.jobs[0].apply_url or "")
        self.assertNotIn("gh_src", result.jobs[0].apply_url or "")


class HtmlParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = HtmlTableParser()
        self.repo = sample_repo(
            "simplify-summer-2026", "SimplifyJobs/Summer2026-Internships", branch="dev"
        )

    def test_html_simplify_style(self) -> None:
        result = self.parser.parse(read_fixture("html_simplify_style.md"), self.repo)
        self.assertTrue(result.table_detected)
        self.assertGreaterEqual(result.confidence, 0.8)
        self.assertGreaterEqual(len(result.jobs), 5)

        first = next(job for job in result.jobs if "Agent Platform" in job.role)
        self.assertEqual(first.company, "Netic")
        self.assertIn("ashbyhq.com", first.apply_url or "")
        self.assertNotIn("simplify.jobs/c/", first.apply_url or "")

        repeated = next(job for job in result.jobs if "Forward Deployed" in job.role)
        self.assertEqual(repeated.company, "Netic")
        self.assertIn("SF", repeated.location)
        self.assertIn("NYC", repeated.location)

        closed = next(job for job in result.jobs if job.company == "ClosedCo")
        self.assertTrue(closed.closed)

        relative = next(job for job in result.jobs if job.company == "ML Labs")
        self.assertTrue((relative.apply_url or "").startswith("https://github.com/"))


class UrlAndFilterTests(unittest.TestCase):
    def test_tracking_param_normalization(self) -> None:
        left = normalize_url(
            "https://Example.com/jobs/1/?utm_source=x&b=2&a=1&ref=y#frag"
        )
        right = normalize_url("https://example.com/jobs/1?a=1&b=2")
        self.assertEqual(left, right)

        id_a = make_job_id(
            apply_url="https://Example.com/jobs/1/?utm_source=x",
            company="A",
            role="B",
            location="C",
            source_repo="owner/one",
        )
        id_b = make_job_id(
            apply_url="https://example.com/jobs/1",
            company="Different",
            role="Role",
            location="Loc",
            source_repo="owner/two",
        )
        self.assertEqual(id_a, id_b)

    def test_filtering_rules(self) -> None:
        parser = MarkdownTableParser()
        result = parser.parse(
            read_fixture("markdown_summer_2027.md"),
            sample_repo("summer-2027", "sndsh404/summer-2027-internships"),
        )
        accepted, rejected = filter_jobs(
            result.jobs,
            FilterConfig(
                include_keywords=["software engineer", "machine learning", "computer vision", "data scientist"],
                exclude_keywords=["phd"],
            ),
        )
        rejected_reasons = {job.company: reason for job, reason in rejected}
        self.assertIn("Salesforce", rejected_reasons)
        self.assertEqual(rejected_reasons["Salesforce"], "closed")
        self.assertTrue(any(job.company == "Optiver" for job, _ in rejected))
        self.assertTrue(any(job.company == "Google" for job in accepted))


class AIValidationTests(unittest.TestCase):
    def test_invalid_ai_json_rejected(self) -> None:
        jobs, warnings = parse_ai_jobs(
            "not-json",
            source_repo="owner/repo",
            source_url="https://example.com",
        )
        self.assertEqual(jobs, [])
        self.assertTrue(warnings)

    def test_hallucinated_incomplete_records_rejected(self) -> None:
        payload = """
        [
          {"company": "", "role": "", "location": "", "apply_url": null},
          {"company": "OnlyCo", "role": "", "location": "SF", "apply_url": "not-a-url"},
          {
            "company": "GoodCo",
            "role": "Software Engineer Intern",
            "location": "SF",
            "apply_url": "https://careers.good.example/jobs/1",
            "added": null,
            "closed": false,
            "sponsorship": "unknown"
          }
        ]
        """
        jobs, warnings = parse_ai_jobs(
            payload,
            source_repo="owner/repo",
            source_url="https://example.com",
        )
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].company, "GoodCo")
        self.assertTrue(any("Rejected" in warning for warning in warnings))


if __name__ == "__main__":
    unittest.main()
