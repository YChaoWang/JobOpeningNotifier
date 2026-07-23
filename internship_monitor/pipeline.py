"""Repository processing pipeline."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from internship_monitor.discord import DiscordNotifier
from internship_monitor.filtering import filter_jobs
from internship_monitor.github_client import GitHubClient
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
    jobs_would_notify: int = 0
    jobs_would_remain_pending: int = 0
    dry_run: bool = False
    details: list[str] = field(default_factory=list)


class MonitorPipeline:
    def __init__(
        self,
        config: AppConfig,
        state: StateStore,
        github: GitHubClient,
        discord: DiscordNotifier | None,
        *,
        notify_existing: bool = False,
        dry_run: bool = False,
        ai_parser: AIFallbackParser | None = None,
    ) -> None:
        self.config = config
        self.state = state
        self.github = github
        self.discord = discord
        self.notify_existing = notify_existing
        self.dry_run = dry_run
        self.markdown_parser = MarkdownTableParser()
        self.html_parser = HtmlTableParser()
        self.ai_parser = ai_parser or AIFallbackParser(config.ai)

    def run(self) -> RunSummary:
        summary = RunSummary(dry_run=self.dry_run)
        remaining_slots = self.config.notifications.max_jobs_per_run

        # Pending queue always runs first (even when README SHAs are unchanged).
        pending = self._take_pending(remaining_slots)
        if pending:
            logger.info("Processing %d pending job(s) first", len(pending))
            notified, failed = self._notify(pending)
            summary.jobs_notified += len(notified)
            remaining_slots -= len(notified)
            if failed:
                self.state.enqueue_pending(failed)
                summary.jobs_queued_pending += len(failed)
            if self.dry_run:
                summary.jobs_would_notify += len(notified)
                # In dry-run, "failed" are jobs beyond what we pretend to send.
                summary.jobs_would_remain_pending += len(failed)

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
                logger.exception(
                    "[%s] Repository processing failed: %s", repository.name, message
                )
                if not self.dry_run:
                    repo_state = self.state.get_repository_state(repository.name)
                    repo_state.last_error = message[:500]
                    self.state.save()
                continue

            summary.repositories_changed += repo_summary.get("changed", 0)
            summary.repositories_skipped_unchanged += repo_summary.get("unchanged", 0)
            summary.jobs_parsed += repo_summary.get("parsed", 0)
            summary.jobs_rejected += repo_summary.get("rejected", 0)
            summary.jobs_filtered += repo_summary.get("filtered", 0)
            summary.jobs_already_seen += repo_summary.get("already_seen", 0)
            summary.jobs_notified += repo_summary.get("notified", 0)
            summary.jobs_queued_pending += repo_summary.get("queued", 0)
            summary.jobs_would_notify += repo_summary.get("would_notify", 0)
            summary.jobs_would_remain_pending += repo_summary.get(
                "would_remain_pending", 0
            )
            remaining_slots = max(
                0, remaining_slots - int(repo_summary.get("notified", 0))
            )

        if not self.dry_run:
            self.state.save()

        logger.info(
            "Run complete%s: checked=%d changed=%d unchanged=%d failed=%d "
            "parsed=%d filtered=%d seen=%d notified=%d pending_queued=%d "
            "would_notify=%d would_remain_pending=%d",
            " (dry-run)" if self.dry_run else "",
            summary.repositories_checked,
            summary.repositories_changed,
            summary.repositories_skipped_unchanged,
            summary.repositories_failed,
            summary.jobs_parsed,
            summary.jobs_filtered,
            summary.jobs_already_seen,
            summary.jobs_notified,
            summary.jobs_queued_pending,
            summary.jobs_would_notify,
            summary.jobs_would_remain_pending,
        )
        return summary

    def _take_pending(self, limit: int) -> list[Job]:
        if self.dry_run:
            return list(self.state.pending_jobs[: max(0, limit)])
        return self.state.pop_pending(limit)

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
            "would_notify": 0,
            "would_remain_pending": 0,
        }
        logger.info(
            "[%s] Checking %s@%s", repository.name, repository.repo, repository.branch
        )

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
            and not self.notify_existing
        ):
            logger.info(
                "[%s] README unchanged (sha=%s); skipping parse",
                repository.name,
                readme.sha[:12],
            )
            stats["unchanged"] = 1
            if not self.dry_run:
                repo_state.last_error = None
            return stats

        stats["changed"] = 1
        logger.info(
            "[%s] README changed or first run (sha=%s)",
            repository.name,
            readme.sha[:12],
        )

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
            if not self.dry_run:
                repo_state.last_error = (
                    "Parsing failed to produce a reliable result; SHA not updated"
                )
            logger.error(
                "[%s] Skipping state SHA update so a future run can retry parsing",
                repository.name,
            )
            return stats

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
        if is_first_run and not self.notify_existing:
            logger.info(
                "[%s] First-run baseline: storing %d jobs as seen (no Discord)",
                repository.name,
                len(unique_jobs),
            )
            if not self.dry_run:
                self.state.mark_seen_many(unique_jobs, notified=False)
        else:
            if is_first_run and self.notify_existing:
                logger.info(
                    "[%s] First run with --notify-existing: notifying matching jobs",
                    repository.name,
                )

            new_jobs: list[Job] = []
            for job in accepted:
                # --notify-existing re-sends current matches even if already baselined/seen.
                if self.state.is_seen(job.job_id) and not self.notify_existing:
                    stats["already_seen"] += 1
                    continue
                new_jobs.append(job)

            pending_ids = {job.job_id for job in self.state.pending_jobs}
            new_jobs = [job for job in new_jobs if job.job_id not in pending_ids]

            if self.notify_existing and not is_first_run:
                logger.info(
                    "[%s] --notify-existing: %d matching job(s) eligible to (re)notify",
                    repository.name,
                    len(new_jobs),
                )

            to_notify = new_jobs[:remaining_notify_slots]
            overflow = new_jobs[remaining_notify_slots:]

            if to_notify:
                notified, failed = self._notify(to_notify)
                stats["notified"] = len(notified)
                if self.dry_run:
                    stats["would_notify"] = len(notified)
                if failed:
                    if not self.dry_run:
                        self.state.enqueue_pending(failed)
                    stats["queued"] += len(failed)
                    if self.dry_run:
                        stats["would_remain_pending"] += len(failed)

            if overflow:
                if not self.dry_run:
                    self.state.enqueue_pending(overflow)
                stats["queued"] += len(overflow)
                if self.dry_run:
                    stats["would_remain_pending"] += len(overflow)
                logger.info(
                    "[%s] Queued %d job(s) for a future run (max_jobs_per_run reached)",
                    repository.name,
                    len(overflow),
                )

            # Only mark closed jobs as seen among rejects. Keyword/location filter
            # rejects stay unseen so a later filter change can resurface them when
            # the README next changes.
            if not self.dry_run:
                for job, reason in rejected:
                    if reason == "closed":
                        self.state.mark_seen(job, notified=False)

        if not self.dry_run:
            repo_state.last_readme_sha = readme.sha
            repo_state.last_success_at = datetime.now(timezone.utc).isoformat()
            repo_state.last_parser = parse_result.parser.value
            repo_state.last_parser_confidence = parse_result.confidence
            repo_state.last_error = None
            repo_state.initialized = True
            self.state.save()
        return stats

    def _parse_readme(
        self, content: str, repository: RepositoryConfig
    ) -> tuple[ParseResult, int, bool]:
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

    def _notify(self, jobs: list[Job]) -> tuple[list[Job], list[Job]]:
        """Attempt Discord delivery. Returns (succeeded, remaining_unsent)."""
        if not jobs:
            return [], []

        if self.dry_run:
            # Respect message batching visually: pretend all within this call succeed
            # up to the jobs provided (already capped by max_jobs_per_run).
            logger.info("Dry-run: would notify %d job(s)", len(jobs))
            return list(jobs), []

        if self.discord is None:
            logger.warning(
                "Discord notifier unavailable; leaving %d job(s) pending", len(jobs)
            )
            return [], list(jobs)

        sent = self.discord.send_jobs(jobs)
        sent_ids = {job.job_id for job in sent}
        failed = [job for job in jobs if job.job_id not in sent_ids]

        for job in sent:
            self.state.mark_seen(job, notified=True)
        self.state.remove_pending_ids(sent_ids)

        # Checkpoint after each successful notification attempt so a later crash
        # or batch failure cannot cause duplicates of already-accepted jobs.
        if sent:
            self.state.save()
            logger.info(
                "Checkpointed %d notified job(s); %d remain unsent",
                len(sent),
                len(failed),
            )
        return sent, failed


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
    return sum(
        1 for warning in result.warnings if warning.lower().startswith("rejected")
    )


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
