"""Dependency-inversion ports (Protocols) for the monitor."""

from __future__ import annotations

from typing import Protocol

from internship_monitor.github_client import ReadmeDocument
from internship_monitor.models import Job, ParseResult, RepositoryConfig


class ReadmeFetcher(Protocol):
    def fetch_readme(
        self,
        owner: str,
        repo: str,
        *,
        path: str = "README.md",
        branch: str | None = None,
    ) -> ReadmeDocument: ...


class JobNotifier(Protocol):
    def send_jobs(self, jobs: list[Job]) -> list[Job]: ...

    def send_test_message(self) -> None: ...


class ReadmeParser(Protocol):
    name: str

    def parse(self, content: str, repository: RepositoryConfig) -> ParseResult: ...
