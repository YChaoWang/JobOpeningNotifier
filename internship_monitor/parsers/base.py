"""Parser protocol / base helpers."""

from __future__ import annotations

from typing import Protocol

from internship_monitor.models import ParseResult, RepositoryConfig


class ReadmeParser(Protocol):
    name: str

    def parse(self, content: str, repository: RepositoryConfig) -> ParseResult:
        """Parse README content into normalized jobs."""
