"""Shared test helpers."""

from __future__ import annotations

from pathlib import Path

from internship_monitor.models import (
    AIConfig,
    AppConfig,
    FilterConfig,
    NotificationConfig,
    RepositoryConfig,
)

FIXTURES = Path(__file__).parent / "fixtures"


def read_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def sample_repo(
    name: str = "demo",
    repo: str = "owner/demo-repo",
    branch: str = "main",
) -> RepositoryConfig:
    return RepositoryConfig(name=name, repo=repo, branch=branch)


def sample_config(
    repositories: list[RepositoryConfig] | None = None,
    *,
    include_keywords: list[str] | None = None,
    exclude_keywords: list[str] | None = None,
    ai_enabled: bool = True,
    max_jobs_per_run: int = 30,
) -> AppConfig:
    return AppConfig(
        repositories=repositories
        or [
            sample_repo("repo-a", "owner/repo-a"),
            sample_repo("repo-b", "owner/repo-b"),
        ],
        filters=FilterConfig(
            include_keywords=include_keywords
            if include_keywords is not None
            else [
                "software engineer",
                "software engineering",
                "machine learning",
                "artificial intelligence",
                "computer vision",
                "data scientist",
                "data engineering",
            ],
            exclude_keywords=exclude_keywords
            if exclude_keywords is not None
            else ["phd", "postdoctoral"],
        ),
        ai=AIConfig(enabled=ai_enabled, minimum_rule_parser_confidence=0.8),
        notifications=NotificationConfig(
            max_jobs_per_message=5,
            max_jobs_per_run=max_jobs_per_run,
        ),
    )
