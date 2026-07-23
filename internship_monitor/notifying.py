"""Notification delivery with dry-run and state checkpointing."""

from __future__ import annotations

import logging

from internship_monitor.models import Job
from internship_monitor.ports import JobNotifier
from internship_monitor.state import StateStore

logger = logging.getLogger(__name__)


class NotificationService:
    """Single responsibility: deliver jobs and persist notify progress."""

    def __init__(
        self,
        state: StateStore,
        notifier: JobNotifier | None,
        *,
        dry_run: bool = False,
    ) -> None:
        self._state = state
        self._notifier = notifier
        self._dry_run = dry_run

    def deliver(self, jobs: list[Job]) -> tuple[list[Job], list[Job]]:
        """Attempt delivery. Returns (succeeded, remaining_unsent)."""
        if not jobs:
            return [], []

        if self._dry_run:
            logger.info("Dry-run: would notify %d job(s)", len(jobs))
            return list(jobs), []

        if self._notifier is None:
            logger.warning(
                "Discord notifier unavailable; leaving %d job(s) pending", len(jobs)
            )
            return [], list(jobs)

        sent = self._notifier.send_jobs(jobs)
        sent_ids = {job.job_id for job in sent}
        failed = [job for job in jobs if job.job_id not in sent_ids]

        for job in sent:
            self._state.mark_seen(job, notified=True)
        self._state.remove_pending_ids(sent_ids)

        if sent:
            self._state.save()
            logger.info(
                "Checkpointed %d notified job(s); %d remain unsent",
                len(sent),
                len(failed),
            )
        return sent, failed
