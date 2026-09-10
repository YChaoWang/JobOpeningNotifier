"""Config, state, Discord, and pipeline reliability tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from internship_monitor.config import ConfigError, load_config, validate_config
from internship_monitor.discord import DiscordError, DiscordNotifier
from internship_monitor.github_client import GitHubClientError, ReadmeDocument
from internship_monitor.models import Job, ParserName, Sponsorship
from internship_monitor.normalization import make_job_id, normalize_url
from internship_monitor.parsers.ai_fallback import AIFallbackParser
from internship_monitor.pipeline import MonitorPipeline
from internship_monitor.state import StateStore, atomic_write_json, migrate_legacy_state
from tests.conftest import read_fixture, sample_config, sample_repo


def _job(
    company: str,
    role: str,
    url: str,
    source_repo: str = "owner/repo",
    *,
    closed: bool = False,
) -> Job:
    return Job(
        company=company,
        role=role,
        location="SF",
        apply_url=url,
        added=None,
        closed=closed,
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


class ConfigTests(unittest.TestCase):
    def test_valid_config_file(self) -> None:
        config = load_config(Path(__file__).resolve().parents[1] / "config.yaml")
        self.assertGreaterEqual(len(config.repositories), 2)

    def test_invalid_configuration(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            validate_config(
                {
                    "repositories": [
                        {"name": "", "repo": "bad", "branch": ""},
                        {"name": "dup", "repo": "Owner/Repo", "branch": "main"},
                        {"name": "dup", "repo": "owner/repo", "branch": "main"},
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


class StateTests(unittest.TestCase):
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
        repos, seen, pending, version = migrate_legacy_state(legacy_repo, {}, [])
        self.assertIn("legacy-repo", repos)
        self.assertEqual(repos["legacy-repo"]["last_readme_sha"], "abc123")
        self.assertIn("job-1", seen)
        self.assertIn("job-2", seen)
        self.assertEqual(pending, [])
        self.assertGreaterEqual(version, 1)

    def test_missing_and_empty_state_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(tmp)
            store.load()
            self.assertEqual(store.seen_jobs, {})
            self.assertEqual(store.pending_jobs, [])
            Path(tmp, "seen_jobs.json").write_text("\n", encoding="utf-8")
            store.load()
            self.assertEqual(store.seen_jobs, {})

    def test_partially_invalid_pending_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pending_jobs.json"
            path.write_text(
                json.dumps(
                    {
                        "jobs": [
                            "bad",
                            {
                                "company": "Acme",
                                "role": "SWE",
                                "location": "SF",
                                "apply_url": "https://example.com/1",
                                "added": None,
                                "closed": False,
                                "sponsorship": "unknown",
                                "source_repo": "o/r",
                                "source_url": "https://github.com/o/r",
                                "parser": "markdown_table",
                                "job_id": "keep-me",
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            store = StateStore(tmp)
            store.load()
            self.assertEqual(len(store.pending_jobs), 1)
            self.assertEqual(store.pending_jobs[0].job_id, "keep-me")

    def test_atomic_write_and_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "out.json"
            atomic_write_json(target, {"ok": True})
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"ok": True})

            store = StateStore(tmp)
            store.load()
            state = store.get_repository_state("demo")
            state.last_readme_sha = "sha-1"
            state.initialized = True
            store.mark_seen(_job("Acme", "SWE", "https://example.com/1"), notified=True)
            store.save()

            store2 = StateStore(tmp)
            store2.load()
            self.assertEqual(store2.get_repository_state("demo").last_readme_sha, "sha-1")
            self.assertTrue(store2.is_seen(_job("Acme", "SWE", "https://example.com/1").job_id))


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
        with patch("internship_monitor.discord.time.sleep"):
            sent = notifier.send_jobs([_job("Acme", "SWE Intern", "https://ex.com/1")])
        self.assertEqual(len(sent), 1)
        self.assertEqual(session.post.call_count, 2)

    def test_transient_5xx_retry(self) -> None:
        session = MagicMock()
        fail = MagicMock()
        fail.status_code = 503
        fail.headers = {}
        fail.text = "unavailable"
        ok = MagicMock()
        ok.status_code = 204
        session.post.side_effect = [fail, ok]
        notifier = DiscordNotifier(
            "https://discord.com/api/webhooks/123/abc", session=session
        )
        with patch("internship_monitor.discord.time.sleep"):
            sent = notifier.send_jobs([_job("Acme", "SWE Intern", "https://ex.com/1")])
        self.assertEqual(len(sent), 1)

    def test_permanent_4xx_no_infinite_retry(self) -> None:
        session = MagicMock()
        response = MagicMock()
        response.status_code = 400
        response.text = "bad"
        session.post.return_value = response
        notifier = DiscordNotifier(
            "https://discord.com/api/webhooks/123/abc",
            session=session,
            max_jobs_per_message=1,
        )
        jobs = [
            _job("A", "SWE Intern", "https://ex.com/1"),
            _job("B", "SWE Intern", "https://ex.com/2"),
        ]
        sent = notifier.send_jobs(jobs)
        self.assertEqual(sent, [])
        self.assertEqual(session.post.call_count, 1)

    def test_partial_batch_success(self) -> None:
        session = MagicMock()
        ok = MagicMock()
        ok.status_code = 204
        bad = MagicMock()
        bad.status_code = 500
        bad.headers = {}
        bad.text = "err"
        # first batch ok, second batch exhausts retries
        session.post.side_effect = [ok, bad, bad, bad]
        notifier = DiscordNotifier(
            "https://discord.com/api/webhooks/123/abc",
            session=session,
            max_jobs_per_message=1,
        )
        jobs = [
            _job("A", "SWE Intern", "https://ex.com/1"),
            _job("B", "SWE Intern", "https://ex.com/2"),
        ]
        with patch("internship_monitor.discord.time.sleep"):
            sent = notifier.send_jobs(jobs)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0].company, "A")


class PipelineReliabilityTests(unittest.TestCase):
    def test_first_run_silent_baseline(self) -> None:
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
            summary = MonitorPipeline(config, state, github, discord).run()
            self.assertEqual(summary.jobs_notified, 0)
            discord.send_jobs.assert_not_called()
            self.assertTrue(state.get_repository_state("summer-2027").initialized)
            self.assertGreater(len(state.seen_jobs), 0)

    def test_notify_existing_on_first_run(self) -> None:
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
            discord.send_jobs.side_effect = lambda jobs: jobs
            config = sample_config(
                [sample_repo("summer-2027", "sndsh404/summer-2027-internships")],
                ai_enabled=False,
            )
            summary = MonitorPipeline(
                config, state, github, discord, notify_existing=True
            ).run()
            self.assertGreater(summary.jobs_notified, 0)
            discord.send_jobs.assert_called()

    def test_notify_existing_after_silent_baseline(self) -> None:
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
            discord.send_jobs.side_effect = lambda jobs: jobs
            config = sample_config(
                [sample_repo("summer-2027", "sndsh404/summer-2027-internships")],
                ai_enabled=False,
            )
            # Silent baseline first.
            MonitorPipeline(config, state, github, discord).run()
            discord.send_jobs.assert_not_called()
            self.assertGreater(len(state.seen_jobs), 0)

            # Same SHA + --notify-existing should still (re)notify matches.
            discord.reset_mock()
            discord.send_jobs.side_effect = lambda jobs: jobs
            summary = MonitorPipeline(
                config, state, github, discord, notify_existing=True
            ).run()
            self.assertGreater(summary.jobs_notified, 0)
            discord.send_jobs.assert_called()

    def test_unchanged_sha_skips_parse_but_pending_still_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = StateStore(tmp)
            state.load()
            pending = _job("PendingCo", "Software Engineer Intern", "https://ex.com/p")
            state.enqueue_pending([pending])
            state.get_repository_state("summer-2027").initialized = True
            state.get_repository_state("summer-2027").last_readme_sha = "same-sha"

            github = MagicMock()
            github.fetch_readme.return_value = ReadmeDocument(
                content=read_fixture("markdown_summer_2027.md"),
                sha="same-sha",
                path="README.md",
            )
            discord = MagicMock()
            discord.send_jobs.side_effect = lambda jobs: jobs
            config = sample_config(
                [sample_repo("summer-2027", "sndsh404/summer-2027-internships")],
                ai_enabled=False,
            )
            pipeline = MonitorPipeline(config, state, github, discord)
            with patch.object(
                pipeline.repository_processor._parser, "parse"
            ) as parse_mock:
                summary = pipeline.run()
                parse_mock.assert_not_called()
            self.assertEqual(summary.repositories_skipped_unchanged, 1)
            self.assertEqual(summary.jobs_notified, 1)
            self.assertTrue(state.is_seen(pending.job_id))

    def test_overflow_jobs_remain_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = StateStore(tmp)
            state.load()
            rs = state.get_repository_state("repo-a")
            rs.initialized = True
            rs.last_readme_sha = "old"

            rows = [
                f"| Co{i} | Software Engineer Intern | SF | [a](https://ex.com/{i}) | 2026-07-01 |"
                for i in range(5)
            ]
            content = (
                "| Company | Role | Location | Apply | Added |\n"
                "| --- | --- | --- | --- | --- |\n" + "\n".join(rows)
            )
            github = MagicMock()
            github.fetch_readme.return_value = ReadmeDocument(
                content=content, sha="new", path="README.md"
            )
            discord = MagicMock()
            discord.send_jobs.side_effect = lambda jobs: jobs
            config = sample_config(
                [sample_repo("repo-a", "owner/repo-a")],
                include_keywords=["software engineer"],
                ai_enabled=False,
                max_jobs_per_run=2,
            )
            summary = MonitorPipeline(config, state, github, discord).run()
            self.assertEqual(summary.jobs_notified, 2)
            self.assertEqual(len(state.pending_jobs), 3)
            self.assertEqual(summary.jobs_queued_pending, 3)

    def test_partial_discord_success_checkpoints(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = StateStore(tmp)
            state.load()
            rs = state.get_repository_state("repo-a")
            rs.initialized = True
            rs.last_readme_sha = "old"

            content = """
| Company | Role | Location | Apply | Added |
| --- | --- | --- | --- | --- |
| A | Software Engineer Intern | SF | [a](https://ex.com/a) | 2026-07-01 |
| B | Software Engineer Intern | SF | [a](https://ex.com/b) | 2026-07-01 |
"""
            github = MagicMock()
            github.fetch_readme.return_value = ReadmeDocument(
                content=content, sha="new", path="README.md"
            )

            def send(jobs: list[Job]) -> list[Job]:
                # Simulate first job accepted, second failed.
                return jobs[:1]

            discord = MagicMock()
            discord.send_jobs.side_effect = send
            config = sample_config(
                [sample_repo("repo-a", "owner/repo-a")],
                include_keywords=["software engineer"],
                ai_enabled=False,
                max_jobs_per_run=30,
            )
            MonitorPipeline(config, state, github, discord).run()
            # Same Added date → stable order A then B; mock accepts only first → A done, B pending.
            self.assertTrue(
                state.is_seen(
                    make_job_id(
                        apply_url="https://ex.com/a",
                        company="A",
                        role="Software Engineer Intern",
                        location="SF",
                        source_repo="owner/repo-a",
                    )
                )
            )
            self.assertEqual(len(state.pending_jobs), 1)
            self.assertEqual(state.pending_jobs[0].company, "B")

    def test_one_repo_failure_does_not_stop_other(self) -> None:
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
            summary = MonitorPipeline(
                config, state, github, discord, notify_existing=True
            ).run()
            self.assertEqual(summary.repositories_failed, 1)
            self.assertEqual(summary.repositories_changed, 1)
            self.assertGreaterEqual(summary.jobs_notified, 1)

    def test_duplicate_jobs_across_repositories(self) -> None:
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
            summary = MonitorPipeline(config, state, github, discord).run()
            self.assertEqual(summary.jobs_notified, 1)

    def test_failed_parse_does_not_persist_sha(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = StateStore(tmp)
            state.load()
            rs = state.get_repository_state("repo-a")
            rs.initialized = True
            rs.last_readme_sha = "old"
            github = MagicMock()
            github.fetch_readme.return_value = ReadmeDocument(
                content="# Notes\ninternship software engineer apply company location listings",
                sha="new",
                path="README.md",
            )
            discord = MagicMock()
            ai = AIFallbackParser(sample_config(ai_enabled=True).ai, token="fake")
            ai._complete = MagicMock(return_value="not-json")  # type: ignore[method-assign]
            config = sample_config(
                [sample_repo("repo-a", "owner/repo-a")], ai_enabled=True
            )
            MonitorPipeline(config, state, github, discord, ai_parser=ai).run()
            self.assertEqual(state.get_repository_state("repo-a").last_readme_sha, "old")
            discord.send_jobs.assert_not_called()

    def test_dry_run_does_not_mutate_or_notify(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = StateStore(tmp)
            state.load()
            github = MagicMock()
            github.fetch_readme.return_value = ReadmeDocument(
                content=read_fixture("markdown_summer_2027.md"),
                sha="sha1",
                path="README.md",
            )
            discord = MagicMock()
            config = sample_config(
                [sample_repo("summer-2027", "sndsh404/summer-2027-internships")],
                ai_enabled=False,
                max_jobs_per_run=2,
            )
            summary = MonitorPipeline(
                config, state, github, discord, notify_existing=True, dry_run=True
            ).run()
            discord.send_jobs.assert_not_called()
            self.assertFalse(state.get_repository_state("summer-2027").initialized)
            self.assertEqual(state.seen_jobs, {})
            self.assertGreater(summary.jobs_would_notify, 0)
            self.assertLessEqual(
                summary.jobs_would_notify, config.notifications.max_jobs_per_run
            )


class UrlIdentityTests(unittest.TestCase):
    def test_tracking_fragment_slash_and_query_order(self) -> None:
        left = normalize_url(
            "https://Example.com/jobs/1/?utm_source=x&b=2&a=1&ref=y#frag"
        )
        right = normalize_url("https://example.com/jobs/1?a=1&b=2")
        self.assertEqual(left, right)
        self.assertEqual(
            make_job_id(
                apply_url="https://Example.com/jobs/1/?utm_source=x",
                company="A",
                role="B",
                location="C",
                source_repo="o/one",
            ),
            make_job_id(
                apply_url="https://example.com/jobs/1",
                company="X",
                role="Y",
                location="Z",
                source_repo="o/two",
            ),
        )

    def test_fallback_id_includes_source_repo(self) -> None:
        a = make_job_id(
            apply_url=None,
            company="Acme",
            role="SWE",
            location="SF",
            source_repo="owner/one",
        )
        b = make_job_id(
            apply_url=None,
            company="Acme",
            role="SWE",
            location="SF",
            source_repo="owner/two",
        )
        self.assertNotEqual(a, b)

    def test_format_added_date_yyyy_mm_dd(self) -> None:
        from datetime import datetime, timezone

        from internship_monitor.discord import build_job_embed
        from internship_monitor.normalization import format_added_date

        self.assertEqual(format_added_date("2026-07-20"), "2026/07/20")
        self.assertEqual(format_added_date("2026/07/20"), "2026/07/20")
        now = datetime(2026, 7, 23, tzinfo=timezone.utc)
        self.assertEqual(format_added_date("3d", now=now), "2026/07/20")
        self.assertIsNone(format_added_date("-"))

        job = _job("Acme", "SWE Intern", "https://ex.com/1")
        job.added = "2026-07-20"
        fields = {f["name"]: f["value"] for f in build_job_embed(job)["fields"]}
        self.assertEqual(fields["Last update"], "2026/07/20")

    def test_order_jobs_oldest_first_by_date_and_reverse(self) -> None:
        from internship_monitor.normalization import order_jobs_oldest_first

        newer = _job("New", "SWE Intern", "https://ex.com/new")
        newer.added = "2026-07-20"
        older = _job("Old", "SWE Intern", "https://ex.com/old")
        older.added = "2026-06-01"
        ordered = order_jobs_oldest_first([newer, older])
        self.assertEqual([job.company for job in ordered], ["Old", "New"])

        # No dates: assume newest-first input → reverse.
        a = _job("A", "SWE Intern", "https://ex.com/a")
        b = _job("B", "SWE Intern", "https://ex.com/b")
        self.assertEqual(
            [job.company for job in order_jobs_oldest_first([a, b])],
            ["B", "A"],
        )


if __name__ == "__main__":
    unittest.main()
