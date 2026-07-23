"""Job list helpers (dedupe)."""

from __future__ import annotations

from internship_monitor.models import Job


def dedupe_jobs(jobs: list[Job]) -> list[Job]:
    """Keep first occurrence of each job_id."""
    seen: set[str] = set()
    unique: list[Job] = []
    for job in jobs:
        if job.job_id in seen:
            continue
        seen.add(job.job_id)
        unique.append(job)
    return unique
