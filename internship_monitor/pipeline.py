"""Monitor orchestration: collect all sources, then notify by last update."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from internship_monitor.jobs import dedupe_jobs
from internship_monitor.models import AppConfig, Job
from internship_monitor.normalization import order_jobs_by_last_update
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
            notify_existing=notify_existing,
            dry_run=dry_run,
        )

    def run(self) -> RunSummary:
        summary = RunSummary(dry_run=self.dry_run)
        collected: list[Job] = []

        for repository in self.config.repositories:
            if not repository.enabled:
                logger.info("[%s] Disabled; skipping", repository.name)
                continue
            summary.repositories_checked += 1
            try:
                result = self.repository_processor.collect(repository)
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

            repo_stats = result.stats
            summary.repositories_changed += repo_stats.changed
            summary.repositories_skipped_unchanged += repo_stats.unchanged
            summary.jobs_parsed += repo_stats.parsed
            summary.jobs_rejected += repo_stats.rejected
            summary.jobs_filtered += repo_stats.filtered
            summary.jobs_already_seen += repo_stats.already_seen
            collected.extend(result.new_jobs)

        self._notify_merged(summary, collected)

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

    def _notify_merged(self, summary: RunSummary, collected: list[Job]) -> None:
        """Merge pending + new jobs, order by last update across all sources, notify."""
        merged = dedupe_jobs(list(self.state.pending_jobs) + collected)
        if not merged:
            if not self.dry_run:
                self.state.replace_pending([])
            return

        ordered = order_jobs_by_last_update(merged, newest_first=False)
        slots = self.config.notifications.max_jobs_per_run
        to_notify = ordered[:slots]
        overflow = ordered[slots:]

        logger.info(
            "Merged %d job(s) across sources/pending; notifying %d "
            "(oldest last-update first), %d remain pending",
            len(ordered),
            len(to_notify),
            len(overflow),
        )

        notified: list[Job] = []
        failed: list[Job] = []
        if to_notify:
            notified, failed = self.notifications.deliver(to_notify)

        summary.jobs_notified += len(notified)
        remainder = failed + overflow
        summary.jobs_queued_pending += len(remainder)

        if self.dry_run:
            summary.jobs_would_notify += len(notified)
            summary.jobs_would_remain_pending += len(remainder)
        else:
            self.state.replace_pending(
                order_jobs_by_last_update(remainder, newest_first=False)
            )
