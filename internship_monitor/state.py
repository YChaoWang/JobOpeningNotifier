"""Atomic JSON state persistence with backward-compatible migration."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from internship_monitor.models import Job, RepositoryState

logger = logging.getLogger(__name__)

STATE_SCHEMA_VERSION = 2


class StateStore:
    def __init__(
        self,
        data_dir: str | Path,
        *,
        repository_state_file: str = "repository_state.json",
        seen_jobs_file: str = "seen_jobs.json",
        pending_jobs_file: str = "pending_jobs.json",
    ) -> None:
        self.data_dir = Path(data_dir)
        self.repository_state_path = self.data_dir / repository_state_file
        self.seen_jobs_path = self.data_dir / seen_jobs_file
        self.pending_jobs_path = self.data_dir / pending_jobs_file

        self.schema_version = STATE_SCHEMA_VERSION
        self.repository_state: dict[str, RepositoryState] = {}
        self.seen_jobs: dict[str, dict[str, Any]] = {}
        self.pending_jobs: list[Job] = []

    def load(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        raw_repo = _read_json(self.repository_state_path, default={})
        raw_seen = _read_json(self.seen_jobs_path, default={})
        raw_pending = _read_json(self.pending_jobs_path, default=[])

        migrated_repo, migrated_seen, migrated_pending, version = migrate_legacy_state(
            raw_repo, raw_seen, raw_pending
        )
        self.schema_version = version

        self.repository_state = {
            str(name): RepositoryState.from_dict(value)
            for name, value in migrated_repo.items()
            if isinstance(value, dict)
        }
        self.seen_jobs = {}
        for job_id, value in migrated_seen.items():
            if not job_id:
                continue
            if isinstance(value, dict):
                self.seen_jobs[str(job_id)] = value
            else:
                self.seen_jobs[str(job_id)] = {"seen": True}

        self.pending_jobs = []
        if isinstance(migrated_pending, list):
            for item in migrated_pending:
                if not isinstance(item, dict):
                    continue
                try:
                    job = Job.from_dict(item)
                except (TypeError, ValueError, KeyError) as exc:
                    logger.warning("Skipping invalid pending job record: %s", exc)
                    continue
                if job.job_id:
                    self.pending_jobs.append(job)

    def get_repository_state(self, name: str) -> RepositoryState:
        if name not in self.repository_state:
            self.repository_state[name] = RepositoryState()
        return self.repository_state[name]

    def is_seen(self, job_id: str) -> bool:
        return job_id in self.seen_jobs

    def mark_seen(self, job: Job, *, notified: bool) -> None:
        self.seen_jobs[job.job_id] = {
            "company": job.company,
            "role": job.role,
            "source_repo": job.source_repo,
            "apply_url": job.apply_url,
            "notified": notified,
        }

    def mark_seen_many(self, jobs: list[Job], *, notified: bool) -> None:
        for job in jobs:
            self.mark_seen(job, notified=notified)

    def enqueue_pending(self, jobs: list[Job]) -> None:
        existing_ids = {job.job_id for job in self.pending_jobs}
        for job in jobs:
            if job.job_id in existing_ids or self.is_seen(job.job_id):
                continue
            self.pending_jobs.append(job)
            existing_ids.add(job.job_id)

    def pop_pending(self, limit: int) -> list[Job]:
        if limit <= 0:
            return []
        selected = self.pending_jobs[:limit]
        self.pending_jobs = self.pending_jobs[limit:]
        return selected

    def remove_pending_ids(self, job_ids: set[str]) -> None:
        if not job_ids:
            return
        self.pending_jobs = [
            job for job in self.pending_jobs if job.job_id not in job_ids
        ]

    def save(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        repo_payload = {
            "_schema_version": STATE_SCHEMA_VERSION,
            **{
                name: state.to_dict()
                for name, state in sorted(self.repository_state.items())
            },
        }
        seen_payload = {
            "_schema_version": STATE_SCHEMA_VERSION,
            "jobs": dict(sorted(self.seen_jobs.items())),
        }
        pending_payload = {
            "_schema_version": STATE_SCHEMA_VERSION,
            "jobs": [job.to_dict() for job in self.pending_jobs],
        }

        atomic_write_json(self.repository_state_path, repo_payload)
        atomic_write_json(self.seen_jobs_path, seen_payload)
        atomic_write_json(self.pending_jobs_path, pending_payload)


def migrate_legacy_state(
    raw_repo: Any,
    raw_seen: Any,
    raw_pending: Any,
) -> tuple[dict[str, Any], dict[str, Any], list[Any], int]:
    """Migrate older formats without dropping seen history.

    Returns (repositories, seen, pending, schema_version).
    """
    repositories: dict[str, Any] = {}
    seen: dict[str, Any] = {}
    pending: list[Any] = []
    version = STATE_SCHEMA_VERSION

    if isinstance(raw_repo, dict):
        version = int(raw_repo.get("_schema_version") or 1)
        repo_body = {
            key: value
            for key, value in raw_repo.items()
            if key != "_schema_version"
        }
        if _looks_like_repo_map(repo_body):
            repositories = dict(repo_body)
        else:
            legacy_name = (
                raw_repo.get("repository_name")
                or raw_repo.get("name")
                or raw_repo.get("repo")
                or "default"
            )
            repositories[str(legacy_name)] = {
                "last_readme_sha": raw_repo.get("last_readme_sha")
                or raw_repo.get("last_sha")
                or raw_repo.get("readme_sha"),
                "last_success_at": raw_repo.get("last_success_at")
                or raw_repo.get("last_run")
                or raw_repo.get("updated_at"),
                "last_parser": raw_repo.get("last_parser"),
                "last_parser_confidence": raw_repo.get("last_parser_confidence"),
                "last_error": raw_repo.get("last_error"),
                "initialized": raw_repo.get(
                    "initialized",
                    bool(
                        raw_repo.get("last_readme_sha")
                        or raw_repo.get("last_sha")
                        or raw_repo.get("seen_jobs")
                    ),
                ),
            }
            embedded_seen = raw_repo.get("seen_jobs") or raw_repo.get("seen")
            if isinstance(embedded_seen, dict):
                seen.update(_normalize_seen_map(embedded_seen))
            elif isinstance(embedded_seen, list):
                for item in embedded_seen:
                    if isinstance(item, str):
                        seen[item] = {"seen": True}
                    elif isinstance(item, dict) and item.get("job_id"):
                        seen[str(item["job_id"])] = item

    if isinstance(raw_seen, dict):
        if "jobs" in raw_seen and isinstance(raw_seen["jobs"], dict):
            seen.update(_normalize_seen_map(raw_seen["jobs"]))
        elif "jobs" in raw_seen and isinstance(raw_seen["jobs"], list):
            for item in raw_seen["jobs"]:
                if isinstance(item, str):
                    seen[item] = {"seen": True}
                elif isinstance(item, dict) and item.get("job_id"):
                    seen[str(item["job_id"])] = item
        elif "seen_jobs" in raw_seen and isinstance(raw_seen["seen_jobs"], dict):
            seen.update(_normalize_seen_map(raw_seen["seen_jobs"]))
        else:
            body = {
                key: value
                for key, value in raw_seen.items()
                if key != "_schema_version"
            }
            seen.update(_normalize_seen_map(body))
    elif isinstance(raw_seen, list):
        for item in raw_seen:
            if isinstance(item, str):
                seen[item] = {"seen": True}
            elif isinstance(item, dict) and item.get("job_id"):
                seen[str(item["job_id"])] = item

    if isinstance(raw_pending, list):
        pending = list(raw_pending)
    elif isinstance(raw_pending, dict):
        if isinstance(raw_pending.get("jobs"), list):
            pending = list(raw_pending["jobs"])
        else:
            pending = []

    return repositories, seen, pending, max(version, 1)


def _looks_like_repo_map(raw: dict[str, Any]) -> bool:
    if not raw:
        return True
    legacy_keys = {
        "last_sha",
        "last_readme_sha",
        "readme_sha",
        "seen_jobs",
        "seen",
        "repository_name",
    }
    if any(key in raw for key in legacy_keys) and not all(
        isinstance(value, dict) for value in raw.values()
    ):
        return False
    # Ignore metadata keys when checking value types.
    values = [value for key, value in raw.items() if not str(key).startswith("_")]
    if not values:
        return True
    return all(isinstance(value, dict) for value in values)


def _normalize_seen_map(raw: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in raw.items():
        job_id = str(key)
        if isinstance(value, dict):
            result[job_id] = value
        else:
            result[job_id] = {"seen": True}
    return result


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return default
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        logger.warning("Failed to parse %s (%s); using default", path, exc)
        return default


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True)
    json.loads(serialized)

    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(serialized)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
