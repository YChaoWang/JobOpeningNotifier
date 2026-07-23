"""Monitor orchestration: pending drain + per-repository processing."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from internship_monitor.models import AppConfig, Job
from internship_monitor.normalization import order_jobs_oldest_first
from internship_monitor.notifying import NotificationService
from internship_monitor.parsers.ai_fallback import AIFallbackParser
from internship_monitor.parsers.html_table import HtmlTableParser
from internship_monitor.parsers.markdown_table import MarkdownTableParser
from internship_monitor.parsing import ParseCoordinator
from internship_monitor.ports import JobNotifier, ReadmeFetcher
from internship_monitor.processing import RepositoryProcessor
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


class MonitorPipeline:
    """Coordinates the monitoring run; delegates work to focused services."""

    def __init__(
        self,
        config: AppConfig,
        state: StateStore,
        github: ReadmeFetcher,
        discord: JobNotifier | None,
        *,
        notify_existing: bool = False,
        dry_run: bool = False,
        ai_parser: AIFallbackParser | None = None,
        parse_coordinator: ParseCoordinator | None = None,
        notifications: NotificationService | None = None,
        repository_processor: RepositoryProcessor | None = None,
    ) -> None:
        self.config = config
        self.state = state
        self.github = github
        self.discord = discord
        self.notify_existing = notify_existing
        self.dry_run = dry_run

        self.notifications = notifications or NotificationService(
            state, discord, dry_run=dry_run
        )
        self.parse_coordinator = parse_coordinator or ParseCoordinator(
            rule_parsers=[MarkdownTableParser(), HtmlTableParser()],
            ai_parser=ai_parser or AIFallbackParser(config.ai),
            ai_config=config.ai,
        )
        self.repository_processor = repository_processor or RepositoryProcessor(
            config,
            state,
            github,
            self.parse_coordinator,
            self.notifications,
            notify_existing=notify_existing,
            dry_run=dry_run,
        )

    def run(self) -> RunSummary:
        summary = RunSummary(dry_run=self.dry_run)
        remaining_slots = self.config.notifications.max_jobs_per_run

        remaining_slots = self._drain_pending(summary, remaining_slots)

        for repository in self.config.repositories:
            if not repository.enabled:
                logger.info("[%s] Disabled; skipping", repository.name)
                continue
            summary.repositories_checked += 1
            try:
                repo_stats = self.repository_processor.process(
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

            summary.repositories_changed += repo_stats.changed
            summary.repositories_skipped_unchanged += repo_stats.unchanged
            summary.jobs_parsed += repo_stats.parsed
            summary.jobs_rejected += repo_stats.rejected
            summary.jobs_filtered += repo_stats.filtered
            summary.jobs_already_seen += repo_stats.already_seen
            summary.jobs_notified += repo_stats.notified
            summary.jobs_queued_pending += repo_stats.queued
            summary.jobs_would_notify += repo_stats.would_notify
            summary.jobs_would_remain_pending += repo_stats.would_remain_pending
            remaining_slots = max(0, remaining_slots - repo_stats.notified)

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

    def _drain_pending(self, summary: RunSummary, remaining_slots: int) -> int:
        pending = order_jobs_oldest_first(self._take_pending(remaining_slots))
        if not pending:
            return remaining_slots

        logger.info(
            "Processing %d pending job(s) first (oldest → newest)", len(pending)
        )
        notified, failed = self.notifications.deliver(pending)
        summary.jobs_notified += len(notified)
        remaining_slots -= len(notified)
        if failed:
            self.state.enqueue_pending(failed)
            summary.jobs_queued_pending += len(failed)
        if self.dry_run:
            summary.jobs_would_notify += len(notified)
            summary.jobs_would_remain_pending += len(failed)
        return remaining_slots

    def _take_pending(self, limit: int) -> list[Job]:
        if self.dry_run:
            return list(self.state.pending_jobs[: max(0, limit)])
        return self.state.pop_pending(limit)
