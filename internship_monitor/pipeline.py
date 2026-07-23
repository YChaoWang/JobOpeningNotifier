"""Repository processing pipeline."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from internship_monitor.discord import DiscordError, DiscordNotifier
from internship_monitor.filtering import filter_jobs
from internship_monitor.github_client import GitHubClient, GitHubClientError
from internship_monitor.models import (
    AppConfig,
    Job,
    ParseResult,
    RepositoryConfig,
)
from internship_monitor.parsers.ai_fallback import AIFallbackParser
from internship_monitor.parsers.html_table import HtmlTableParser
from internship_monitor.parsers.markdown_table import MarkdownTableParser
from internship_monitor.state import StateStore

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class RunSummary:
    repositories_checked: int = 0
    repositories_changed: int = 0
    repositories_skipped_unchanged: int = 0
    repositories_failed: int = 0
    jobs_parsed: int = 0
    jobs_rejected: int = 0
    jobs_filtered: int = 0
    jobs_already_seen: int = 0
    jobs_notified: int = 0
    jobs_queued_pending: int = 0


class MonitorPipeline:
    def __init__(
        self,
        config: AppConfig,
        state: StateStore,
        github: GitHubClient,
        discord: DiscordNotifier | None,
        *,
        baseline_only: bool = False,
        ai_parser: AIFallbackParser | None = None,
    ) -> None:
        self.config = config
        self.state = state
        self.github = github
        self.discord = discord
        self.baseline_only = baseline_only
        self.markdown_parser = MarkdownTableParser()
        self.html_parser = HtmlTableParser()
        self.ai_parser = ai_parser or AIFallbackParser(config.ai)

    def run(self) -> RunSummary:
        summary = RunSummary()

        # Pending queue from previous runs gets priority within max_jobs_per_run.
        remaining_slots = self.config.notifications.max_jobs_per_run
        pending = self.state.pop_pending(remaining_slots)
        if pending:
            notified = self._notify(pending)
            summary.jobs_notified += len(notified)
            remaining_slots -= len(notified)
            # Re-queue anything not successfully notified.
            failed_pending = [
                job for job in pending if job.job_id not in {n.job_id for n in notified}
            ]
            if failed_pending:
                self.state.enqueue_pending(failed_pending)

        for repository in self.config.repositories:
            if not repository.enabled:
                logger.info("[%s] Disabled; skipping", repository.name)
                continue
            summary.repositories_checked += 1
            try:
                repo_summary = self._process_repository(
                    repository, remaining_notify_slots=remaining_slots
                )
            except Exception as exc:  # noqa: BLE001 - isolate per-repo failures
                summary.repositories_failed += 1
                message = f"{exc.__class__.__name__}: {exc}"
                logger.exception("[%s] Repository processing failed: %s", repository.name, message)
                repo_state = self.state.get_repository_state(repository.name)
                repo_state.last_error = message[:500]
                # Do not update SHA on failure.
                continue

            summary.repositories_changed += repo_summary.get("changed", 0)
            summary.repositories_skipped_unchanged += repo_summary.get("unchanged", 0)
            summary.jobs_parsed += repo_summary.get("parsed", 0)
            summary.jobs_rejected += repo_summary.get("rejected", 0)
            summary.jobs_filtered += repo_summary.get("filtered", 0)
            summary.jobs_already_seen += repo_summary.get("already_seen", 0)
            summary.jobs_notified += repo_summary.get("notified", 0)
            summary.jobs_queued_pending += repo_summary.get("queued", 0)
            remaining_slots = max(
                0, remaining_slots - int(repo_summary.get("notified", 0))
            )

        self.state.save()
        logger.info(
            "Run complete: checked=%d changed=%d unchanged=%d failed=%d "
            "parsed=%d filtered=%d seen=%d notified=%d pending_queued=%d",
            summary.repositories_checked,
            summary.repositories_changed,
            summary.repositories_skipped_unchanged,
            summary.repositories_failed,
            summary.jobs_parsed,
            summary.jobs_filtered,
            summary.jobs_already_seen,
            summary.jobs_notified,
            summary.jobs_queued_pending,
        )
        return summary

    def _process_repository(
        self, repository: RepositoryConfig, *, remaining_notify_slots: int
    ) -> dict[str, int]:
        stats = {
            "changed": 0,
            "unchanged": 0,
            "parsed": 0,
            "rejected": 0,
            "filtered": 0,
            "already_seen": 0,
            "notified": 0,
            "queued": 0,
        }
        logger.info("[%s] Checking %s@%s", repository.name, repository.repo, repository.branch)

        readme = self.github.fetch_readme(
            repository.owner,
            repository.repository,
            path=repository.readme_path,
            branch=repository.branch,
        )
        repo_state = self.state.get_repository_state(repository.name)

        if (
            repo_state.initialized
            and repo_state.last_readme_sha
            and repo_state.last_readme_sha == readme.sha
        ):
            logger.info("[%s] README unchanged (sha=%s); skipping", repository.name, readme.sha[:12])
            stats["unchanged"] = 1
            repo_state.last_error = None
            return stats

        stats["changed"] = 1
        logger.info("[%s] README changed or first run (sha=%s)", repository.name, readme.sha[:12])

        parse_result, rejected_count, parse_ok = self._parse_readme(
            readme.content, repository
        )
        stats["parsed"] = len(parse_result.jobs)
        stats["rejected"] = rejected_count

        logger.info(
            "[%s] Parser=%s confidence=%.3f jobs=%d warnings=%d",
            repository.name,
            parse_result.parser.value,
            parse_result.confidence,
            len(parse_result.jobs),
            len(parse_result.warnings),
        )
        for warning in parse_result.warnings[:10]:
            logger.warning("[%s] Parser warning: %s", repository.name, warning)

        if not parse_ok:
            repo_state.last_error = (
                "Parsing failed to produce a reliable result; SHA not updated"
            )
            logger.error(
                "[%s] Skipping state SHA update so a future run can retry parsing",
                repository.name,
            )
            return stats

        # Deduplicate within this repository parse by job_id.
        unique_jobs = _dedupe_jobs(parse_result.jobs)

        accepted, rejected = filter_jobs(unique_jobs, self.config.filters)
        stats["filtered"] = len(rejected)
        logger.info(
            "[%s] After filters: accepted=%d filtered=%d",
            repository.name,
            len(accepted),
            len(rejected),
        )

        is_first_run = not repo_state.initialized
        if is_first_run and self.baseline_only:
            # Opt-out: seed seen IDs without Discord notifications.
            self.state.mark_seen_many(unique_jobs, notified=False)
            logger.info(
                "[%s] First-run baseline-only: stored %d jobs as seen (no Discord notifications)",
                repository.name,
                len(unique_jobs),
            )
        else:
            if is_first_run:
                logger.info(
                    "[%s] First run: notifying all matching jobs (%d accepted after filters)",
                    repository.name,
                    len(accepted),
                )

            new_jobs: list[Job] = []
            for job in accepted:
                if self.state.is_seen(job.job_id):
                    stats["already_seen"] += 1
                    continue
                new_jobs.append(job)

            # Cross-repo URL identity already handled via job_id; also skip if pending.
            pending_ids = {job.job_id for job in self.state.pending_jobs}
            new_jobs = [job for job in new_jobs if job.job_id not in pending_ids]

            to_notify = new_jobs[:remaining_notify_slots]
            overflow = new_jobs[remaining_notify_slots:]

            if to_notify:
                notified = self._notify(to_notify)
                stats["notified"] = len(notified)
                # Jobs that failed to notify stay eligible via pending queue.
                notified_ids = {job.job_id for job in notified}
                failed = [job for job in to_notify if job.job_id not in notified_ids]
                if failed:
                    self.state.enqueue_pending(failed)
                    stats["queued"] += len(failed)

            if overflow:
                self.state.enqueue_pending(overflow)
                stats["queued"] += len(overflow)
                logger.info(
                    "[%s] Queued %d job(s) for a future run (max_jobs_per_run reached)",
                    repository.name,
                    len(overflow),
                )

            # Mark filtered/closed jobs as seen so they do not resurface endlessly.
            # Only mark successfully processed identities; unnotified new jobs remain unseen/pending.
            for job in unique_jobs:
                if job.job_id in {j.job_id for j in accepted}:
                    continue
                self.state.mark_seen(job, notified=False)

        # Persist SHA only after successful processing.
        repo_state.last_readme_sha = readme.sha
        repo_state.last_success_at = datetime.now(timezone.utc).isoformat()
        repo_state.last_parser = parse_result.parser.value
        repo_state.last_parser_confidence = parse_result.confidence
        repo_state.last_error = None
        repo_state.initialized = True
        return stats

    def _parse_readme(
        self, content: str, repository: RepositoryConfig
    ) -> tuple[ParseResult, int, bool]:
        """Return (result, rejected_count, parse_ok).

        parse_ok is False when AI was required and neither rule nor AI parsing
        produced a reliable outcome — callers must not persist the README SHA.
        """
        markdown_result = self.markdown_parser.parse(content, repository)
        html_result = self.html_parser.parse(content, repository)

        rule_result = _best_rule_result(markdown_result, html_result)
        rejected_estimate = _estimate_rejected(rule_result)

        needs_ai = self._needs_ai(content, rule_result)
        if needs_ai:
            logger.info(
                "[%s] Rule parser insufficient (parser=%s confidence=%.3f); "
                "attempting AI fallback",
                repository.name,
                rule_result.parser.value,
                rule_result.confidence,
            )
            ai_result = self.ai_parser.parse(content, repository)
            if ai_result.jobs:
                return ai_result, 0, True
            merged_warnings = list(rule_result.warnings) + list(ai_result.warnings)
            rule_result.warnings = merged_warnings
            if rule_result.jobs:
                return rule_result, rejected_estimate, True
            # AI was needed and nothing usable was produced.
            return rule_result, rejected_estimate, False

        return rule_result, rejected_estimate, True

    def _needs_ai(self, content: str, result: ParseResult) -> bool:
        if not self.config.ai.enabled:
            return False
        threshold = self.config.ai.minimum_rule_parser_confidence
        if not result.table_detected:
            return _looks_like_job_listing(content)
        if not result.columns_mapped:
            return True
        if result.confidence < threshold:
            return True
        if not result.jobs and _looks_like_job_listing(content):
            return True
        return False

    def _notify(self, jobs: list[Job]) -> list[Job]:
        if not jobs:
            return []
        if self.discord is None:
            logger.warning(
                "Discord notifier unavailable; leaving %d job(s) unnotified", len(jobs)
            )
            return []
        try:
            sent = self.discord.send_jobs(jobs)
        except DiscordError as exc:
            logger.error("Discord notification failed: %s", exc)
            return []
        for job in sent:
            self.state.mark_seen(job, notified=True)
        self.state.remove_pending_ids({job.job_id for job in sent})
        return sent


def _best_rule_result(first: ParseResult, second: ParseResult) -> ParseResult:
    def key(result: ParseResult) -> tuple[float, int, int]:
        return (
            result.confidence,
            len(result.jobs),
            1 if result.columns_mapped else 0,
        )

    if key(second) > key(first):
        return second
    return first


def _estimate_rejected(result: ParseResult) -> int:
    return sum(1 for warning in result.warnings if warning.lower().startswith("rejected"))


def _dedupe_jobs(jobs: list[Job]) -> list[Job]:
    seen: set[str] = set()
    unique: list[Job] = []
    for job in jobs:
        if job.job_id in seen:
            continue
        seen.add(job.job_id)
        unique.append(job)
    return unique


def _looks_like_job_listing(content: str) -> bool:
    lower = content.lower()
    signals = 0
    for token in (
        "internship",
        "intern",
        "software engineer",
        "apply",
        "company",
        "location",
        "<table",
        "| company",
    ):
        if token in lower:
            signals += 1
    return signals >= 3
