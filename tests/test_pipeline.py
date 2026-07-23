"""Config, state migration, Discord, and pipeline integration tests."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from internship_monitor.config import ConfigError, load_config, validate_config
from internship_monitor.discord import DiscordError, DiscordNotifier
from internship_monitor.github_client import GitHubClientError, ReadmeDocument
from internship_monitor.models import Job, ParserName, Sponsorship
from internship_monitor.normalization import make_job_id
from internship_monitor.parsers.ai_fallback import AIFallbackParser
from internship_monitor.pipeline import MonitorPipeline
from internship_monitor.state import StateStore, migrate_legacy_state
from tests.conftest import read_fixture, sample_config, sample_repo


class ConfigTests(unittest.TestCase):
    def test_valid_config_file(self) -> None:
        config = load_config(Path(__file__).resolve().parents[1] / "config.yaml")
        self.assertGreaterEqual(len(config.repositories), 2)

    def test_invalid_config_messages(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            validate_config(
                {
                    "repositories": [
                        {"name": "", "repo": "bad", "branch": ""},
                        {
                            "name": "dup",
                            "repo": "Owner/Repo",
                            "branch": "main",
                        },
                        {
                            "name": "dup",
                            "repo": "owner/repo",
                            "branch": "main",
                        },
                    ],
                    "filters": {"include_keywords": "nope"},
                }
            )
        message = str(ctx.exception)
        self.assertIn("missing repo name", message)
        self.assertIn("invalid owner/repository format", message)
        self.assertIn("missing branch", message)
        self.assertIn("duplicate repository name", message)
        self.assertIn("duplicate repository entry", message)
        self.assertIn("include_keywords must be a list", message)


class StateMigrationTests(unittest.TestCase):
    def test_legacy_flat_state_migration(self) -> None:
        legacy_repo = {
            "repository_name": "legacy-repo",
            "last_sha": "abc123",
            "last_run": "2026-01-01T00:00:00+00:00",
            "seen_jobs": {
                "job-1": {"company": "Acme", "notified": True},
                "job-2": {"company": "Beta"},
            },
        }
        repos, seen, pending = migrate_legacy_state(legacy_repo, {}, [])
        self.assertIn("legacy-repo", repos)
        self.assertEqual(repos["legacy-repo"]["last_readme_sha"], "abc123")
        self.assertIn("job-1", seen)
        self.assertIn("job-2", seen)
        self.assertEqual(pending, [])

    def test_state_store_round_trip(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(tmp)
            store.load()
            state = store.get_repository_state("demo")
            state.last_readme_sha = "sha-1"
            state.initialized = True
            job = Job(
                company="Acme",
                role="SWE Intern",
                location="SF",
                apply_url="https://example.com/jobs/1",
                added=None,
                closed=False,
                sponsorship=Sponsorship.UNKNOWN,
                source_repo="owner/repo",
                source_url="https://github.com/owner/repo",
                parser=ParserName.MARKDOWN_TABLE,
                job_id="abc",
            )
            store.mark_seen(job, notified=True)
            store.save()

            store2 = StateStore(tmp)
            store2.load()
            self.assertEqual(store2.get_repository_state("demo").last_readme_sha, "sha-1")
            self.assertTrue(store2.is_seen("abc"))


class DiscordTests(unittest.TestCase):
    def test_rate_limit_retry_then_success(self) -> None:
        session = MagicMock()
        limited = MagicMock()
        limited.status_code = 429
        limited.json.return_value = {"retry_after": 0.01}
        limited.headers = {}
        ok = MagicMock()
        ok.status_code = 204
        session.post.side_effect = [limited, ok]

        notifier = DiscordNotifier(
            "https://discord.com/api/webhooks/123/abc",
            session=session,
            max_jobs_per_message=5,
        )
        job = Job(
            company="Acme",
            role="Software Engineer Intern",
            location="SF",
            apply_url="https://example.com/jobs/1",
            added="2026-07-01",
            closed=False,
            sponsorship=Sponsorship.UNKNOWN,
            source_repo="owner/repo",
            source_url="https://github.com/owner/repo",
            parser=ParserName.MARKDOWN_TABLE,
            job_id="id-1",
        )
        with patch("internship_monitor.discord.time.sleep"):
            sent = notifier.send_jobs([job])
        self.assertEqual(len(sent), 1)
        self.assertEqual(session.post.call_count, 2)

    def test_permanent_4xx_raises(self) -> None:
        session = MagicMock()
        response = MagicMock()
        response.status_code = 400
        session.post.return_value = response
        notifier = DiscordNotifier(
            "https://discord.com/api/webhooks/123/abc", session=session
        )
        with self.assertRaises(DiscordError):
            notifier.send_test_message()


class PipelineTests(unittest.TestCase):
    def _job(self, company: str, role: str, url: str, source_repo: str) -> Job:
        return Job(
            company=company,
            role=role,
            location="SF",
            apply_url=url,
            added=None,
            closed=False,
            sponsorship=Sponsorship.UNKNOWN,
            source_repo=source_repo,
            source_url=f"https://github.com/{source_repo}",
            parser=ParserName.MARKDOWN_TABLE,
            job_id=make_job_id(
                apply_url=url,
                company=company,
                role=role,
                location="SF",
                source_repo=source_repo,
            ),
        )

    def test_first_run_baseline_no_notify(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            state = StateStore(tmp)
            state.load()
            github = MagicMock()
            github.fetch_readme.return_value = ReadmeDocument(
                content=read_fixture("markdown_summer_2027.md"),
                sha="sha-first",
                path="README.md",
            )
            discord = MagicMock()
            config = sample_config(
                [sample_repo("summer-2027", "sndsh404/summer-2027-internships")],
                ai_enabled=False,
            )
            pipeline = MonitorPipeline(config, state, github, discord)
            summary = pipeline.run()
            self.assertEqual(summary.jobs_notified, 0)
            discord.send_jobs.assert_not_called()
            self.assertTrue(state.get_repository_state("summer-2027").initialized)
            self.assertEqual(
                state.get_repository_state("summer-2027").last_readme_sha, "sha-first"
            )
            self.assertGreater(len(state.seen_jobs), 0)

    def test_unchanged_sha_skips_parsing(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            state = StateStore(tmp)
            state.load()
            repo_state = state.get_repository_state("summer-2027")
            repo_state.initialized = True
            repo_state.last_readme_sha = "same-sha"

            github = MagicMock()
            github.fetch_readme.return_value = ReadmeDocument(
                content=read_fixture("markdown_summer_2027.md"),
                sha="same-sha",
                path="README.md",
            )
            discord = MagicMock()
            config = sample_config(
                [sample_repo("summer-2027", "sndsh404/summer-2027-internships")],
                ai_enabled=False,
            )
            pipeline = MonitorPipeline(config, state, github, discord)
            with patch.object(pipeline, "_parse_readme") as parse_mock:
                summary = pipeline.run()
                parse_mock.assert_not_called()
            self.assertEqual(summary.repositories_skipped_unchanged, 1)
            discord.send_jobs.assert_not_called()

    def test_one_repo_failure_does_not_stop_other(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            state = StateStore(tmp)
            state.load()

            def fetch(owner, repo, path="README.md", branch=None):
                if repo == "repo-a":
                    raise GitHubClientError("boom")
                return ReadmeDocument(
                    content=read_fixture("markdown_alternate_headers.md"),
                    sha="sha-b",
                    path="README.md",
                )

            github = MagicMock()
            github.fetch_readme.side_effect = fetch
            discord = MagicMock()
            discord.send_jobs.side_effect = lambda jobs: jobs

            config = sample_config(
                [
                    sample_repo("repo-a", "owner/repo-a"),
                    sample_repo("repo-b", "owner/repo-b"),
                ],
                include_keywords=[],
                ai_enabled=False,
            )
            pipeline = MonitorPipeline(
                config, state, github, discord, notify_existing=True
            )
            summary = pipeline.run()
            self.assertEqual(summary.repositories_failed, 1)
            self.assertEqual(summary.repositories_changed, 1)
            self.assertGreaterEqual(summary.jobs_notified, 1)

    def test_duplicate_jobs_across_repositories(self) -> None:
        import tempfile

        shared_url = "https://careers.shared.example/jobs/1?utm_source=one"
        content_a = """
| Company | Role | Location | Apply | Added |
| --- | --- | --- | --- | --- |
| SharedCo | Software Engineer Intern | SF | [apply](https://careers.shared.example/jobs/1?utm_source=one) | 2026-07-01 |
"""
        content_b = """
| Company | Role | Location | Apply | Added |
| --- | --- | --- | --- | --- |
| SharedCo | Software Engineer Intern | SF | [apply](https://careers.shared.example/jobs/1?utm_medium=two) | 2026-07-01 |
"""
        with tempfile.TemporaryDirectory() as tmp:
            state = StateStore(tmp)
            state.load()
            # Pretend both repos already initialized with different SHAs.
            for name, sha in (("repo-a", "old-a"), ("repo-b", "old-b")):
                rs = state.get_repository_state(name)
                rs.initialized = True
                rs.last_readme_sha = sha

            def fetch(owner, repo, path="README.md", branch=None):
                if repo == "repo-a":
                    return ReadmeDocument(content=content_a, sha="new-a", path="README.md")
                return ReadmeDocument(content=content_b, sha="new-b", path="README.md")

            github = MagicMock()
            github.fetch_readme.side_effect = fetch
            discord = MagicMock()
            discord.send_jobs.side_effect = lambda jobs: jobs

            config = sample_config(
                [
                    sample_repo("repo-a", "owner/repo-a"),
                    sample_repo("repo-b", "owner/repo-b"),
                ],
                include_keywords=["software engineer"],
                ai_enabled=False,
            )
            pipeline = MonitorPipeline(config, state, github, discord)
            summary = pipeline.run()
            self.assertEqual(summary.jobs_notified, 1)
            expected_id = make_job_id(
                apply_url=shared_url,
                company="SharedCo",
                role="Software Engineer Intern",
                location="SF",
                source_repo="owner/repo-a",
            )
            self.assertTrue(state.is_seen(expected_id))

    def test_invalid_ai_does_not_notify(self) -> None:
        import tempfile

        unparseable = "# Notes\n\nNo table here, but internship software engineer apply company location listings maybe."
        with tempfile.TemporaryDirectory() as tmp:
            state = StateStore(tmp)
            state.load()
            rs = state.get_repository_state("repo-a")
            rs.initialized = True
            rs.last_readme_sha = "old"

            github = MagicMock()
            github.fetch_readme.return_value = ReadmeDocument(
                content=unparseable, sha="new", path="README.md"
            )
            discord = MagicMock()

            ai = AIFallbackParser(sample_config(ai_enabled=True).ai, token="fake-token")
            ai._complete = MagicMock(return_value="not-json")  # type: ignore[method-assign]

            config = sample_config(
                [sample_repo("repo-a", "owner/repo-a")],
                ai_enabled=True,
            )
            pipeline = MonitorPipeline(config, state, github, discord, ai_parser=ai)
            summary = pipeline.run()
            discord.send_jobs.assert_not_called()
            # SHA must not advance when AI was required and failed.
            self.assertEqual(state.get_repository_state("repo-a").last_readme_sha, "old")
            self.assertEqual(summary.jobs_notified, 0)


class GithubClientUnitTests(unittest.TestCase):
    def test_rate_limit_exhaustion(self) -> None:
        session = MagicMock()
        response = MagicMock()
        response.status_code = 403
        response.text = "API rate limit exceeded"
        response.headers = {"Retry-After": "0"}
        session.request.return_value = response
        client = __import__(
            "internship_monitor.github_client", fromlist=["GitHubClient"]
        ).GitHubClient(session=session)
        with patch("internship_monitor.github_client.time.sleep"):
            with self.assertRaises(GitHubClientError):
                client.fetch_readme("o", "r")


if __name__ == "__main__":
    unittest.main()
