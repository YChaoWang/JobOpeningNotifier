"""Per-repository processing (fetch → parse → filter → notify/queue)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from internship_monitor.filtering import filter_jobs
from internship_monitor.jobs import dedupe_jobs
from internship_monitor.models import AppConfig, Job, RepositoryConfig
from internship_monitor.normalization import order_jobs_oldest_first
from internship_monitor.notifying import NotificationService
from internship_monitor.parsing import ParseCoordinator
from internship_monitor.ports import ReadmeFetcher
from internship_monitor.state import StateStore

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class RepositoryRunStats:
    changed: int = 0
    unchanged: int = 0
    parsed: int = 0
    rejected: int = 0
    filtered: int = 0
    already_seen: int = 0
    notified: int = 0
    queued: int = 0
    would_notify: int = 0
    would_remain_pending: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "changed": self.changed,
            "unchanged": self.unchanged,
            "parsed": self.parsed,
            "rejected": self.rejected,
            "filtered": self.filtered,
            "already_seen": self.already_seen,
            "notified": self.notified,
            "queued": self.queued,
            "would_notify": self.would_notify,
            "would_remain_pending": self.would_remain_pending,
        }


class RepositoryProcessor:
    """Processes one configured source repository."""

    def __init__(
        self,
        config: AppConfig,
        state: StateStore,
        github: ReadmeFetcher,
        parser: ParseCoordinator,
        notifications: NotificationService,
        *,
        notify_existing: bool = False,
        dry_run: bool = False,
    ) -> None:
        self._config = config
        self._state = state
        self._github = github
        self._parser = parser
        self._notifications = notifications
        self._notify_existing = notify_existing
        self._dry_run = dry_run

    def process(
        self, repository: RepositoryConfig, *, remaining_notify_slots: int
    ) -> RepositoryRunStats:
        stats = RepositoryRunStats()
        logger.info(
            "[%s] Checking %s@%s", repository.name, repository.repo, repository.branch
        )

        readme = self._github.fetch_readme(
            repository.owner,
            repository.repository,
            path=repository.readme_path,
            branch=repository.branch,
        )
        repo_state = self._state.get_repository_state(repository.name)

        if (
            repo_state.initialized
            and repo_state.last_readme_sha
            and repo_state.last_readme_sha == readme.sha
            and not self._notify_existing
        ):
            logger.info(
                "[%s] README unchanged (sha=%s); skipping parse",
                repository.name,
                readme.sha[:12],
            )
            stats.unchanged = 1
            if not self._dry_run:
                repo_state.last_error = None
            return stats

        stats.changed = 1
        logger.info(
            "[%s] README changed or first run (sha=%s)",
            repository.name,
            readme.sha[:12],
        )

        parse_result, rejected_count, parse_ok = self._parser.parse(
            readme.content, repository
        )
        stats.parsed = len(parse_result.jobs)
        stats.rejected = rejected_count

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
            if not self._dry_run:
                repo_state.last_error = (
                    "Parsing failed to produce a reliable result; SHA not updated"
                )
            logger.error(
                "[%s] Skipping state SHA update so a future run can retry parsing",
                repository.name,
            )
            return stats

        unique_jobs = dedupe_jobs(parse_result.jobs)
        accepted, rejected = filter_jobs(unique_jobs, self._config.filters)
        stats.filtered = len(rejected)
        logger.info(
            "[%s] After filters: accepted=%d filtered=%d",
            repository.name,
            len(accepted),
            len(rejected),
        )

        is_first_run = not repo_state.initialized
        if is_first_run and not self._notify_existing:
            self._baseline(repository.name, unique_jobs)
        else:
            self._notify_new_matches(
                repository.name,
                accepted,
                rejected,
                remaining_notify_slots=remaining_notify_slots,
                stats=stats,
                is_first_run=is_first_run,
            )

        if not self._dry_run:
            repo_state.last_readme_sha = readme.sha
            repo_state.last_success_at = datetime.now(timezone.utc).isoformat()
            repo_state.last_parser = parse_result.parser.value
            repo_state.last_parser_confidence = parse_result.confidence
            repo_state.last_error = None
            repo_state.initialized = True
            self._state.save()
        return stats

    def _baseline(self, repo_name: str, unique_jobs: list[Job]) -> None:
        logger.info(
            "[%s] First-run baseline: storing %d jobs as seen (no Discord)",
            repo_name,
            len(unique_jobs),
        )
        if not self._dry_run:
            self._state.mark_seen_many(unique_jobs, notified=False)

    def _notify_new_matches(
        self,
        repo_name: str,
        accepted: list[Job],
        rejected: list[tuple[Job, str]],
        *,
        remaining_notify_slots: int,
        stats: RepositoryRunStats,
        is_first_run: bool,
    ) -> None:
        if is_first_run and self._notify_existing:
            logger.info(
                "[%s] First run with --notify-existing: notifying matching jobs",
                repo_name,
            )

        new_jobs: list[Job] = []
        for job in accepted:
            if self._state.is_seen(job.job_id) and not self._notify_existing:
                stats.already_seen += 1
                continue
            new_jobs.append(job)

        pending_ids = {job.job_id for job in self._state.pending_jobs}
        new_jobs = [job for job in new_jobs if job.job_id not in pending_ids]

        if self._notify_existing and not is_first_run:
            logger.info(
                "[%s] --notify-existing: %d matching job(s) eligible to (re)notify",
                repo_name,
                len(new_jobs),
            )

        ordered = order_jobs_oldest_first(new_jobs)
        to_notify = ordered[:remaining_notify_slots]
        overflow = ordered[remaining_notify_slots:]

        if to_notify:
            notified, failed = self._notifications.deliver(to_notify)
            stats.notified = len(notified)
            if self._dry_run:
                stats.would_notify = len(notified)
            if failed:
                if not self._dry_run:
                    self._state.enqueue_pending(failed)
                stats.queued += len(failed)
                if self._dry_run:
                    stats.would_remain_pending += len(failed)

        if overflow:
            if not self._dry_run:
                self._state.enqueue_pending(overflow)
            stats.queued += len(overflow)
            if self._dry_run:
                stats.would_remain_pending += len(overflow)
            logger.info(
                "[%s] Queued %d job(s) for a future run (max_jobs_per_run reached)",
                repo_name,
                len(overflow),
            )

        if not self._dry_run:
            for job, reason in rejected:
                if reason == "closed":
                    self._state.mark_seen(job, notified=False)
