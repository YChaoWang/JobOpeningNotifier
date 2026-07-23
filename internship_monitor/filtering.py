"""Case-insensitive job filtering."""

from __future__ import annotations

import logging

from internship_monitor.models import FilterConfig, Job, Sponsorship
from internship_monitor.normalization import normalize_text

logger = logging.getLogger(__name__)


def job_search_text(job: Job) -> str:
    return normalize_text(" ".join([job.company, job.role, job.location]))


def filter_jobs(
    jobs: list[Job], config: FilterConfig
) -> tuple[list[Job], list[tuple[Job, str]]]:
    """Return (accepted, rejected_with_reason)."""
    accepted: list[Job] = []
    rejected: list[tuple[Job, str]] = []

    include = [normalize_text(k) for k in config.include_keywords if normalize_text(k)]
    exclude = [normalize_text(k) for k in config.exclude_keywords if normalize_text(k)]
    locations = [normalize_text(k) for k in config.locations if normalize_text(k)]

    for job in jobs:
        reason = _reject_reason(job, include, exclude, locations, config)
        if reason:
            rejected.append((job, reason))
            logger.debug(
                "Filtered job %s / %s (%s): %s",
                job.company,
                job.role,
                job.job_id[:12],
                reason,
            )
        else:
            accepted.append(job)

    return accepted, rejected


def _reject_reason(
    job: Job,
    include: list[str],
    exclude: list[str],
    locations: list[str],
    config: FilterConfig,
) -> str | None:
    if job.closed:
        return "closed"

    text = job_search_text(job)

    if include and not any(keyword in text for keyword in include):
        return "missing include keyword"

    for keyword in exclude:
        if keyword in text:
            return f"exclude keyword '{keyword}'"

    if locations:
        location_text = normalize_text(job.location)
        if not any(loc in location_text for loc in locations):
            return "location mismatch"

    if config.exclude_no_sponsorship and job.sponsorship == Sponsorship.UNAVAILABLE:
        return "no sponsorship"

    if (
        config.exclude_us_citizenship_required
        and job.sponsorship == Sponsorship.CITIZENSHIP_REQUIRED
    ):
        return "citizenship required"

    return None
