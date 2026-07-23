"""Typed domain models for the internship monitor."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class Sponsorship(str, Enum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    CITIZENSHIP_REQUIRED = "citizenship_required"
    UNKNOWN = "unknown"


class ParserName(str, Enum):
    MARKDOWN_TABLE = "markdown_table"
    HTML_TABLE = "html_table"
    AI = "ai"


@dataclass(slots=True)
class Job:
    company: str
    role: str
    location: str
    apply_url: str | None
    added: str | None
    closed: bool
    sponsorship: Sponsorship
    source_repo: str
    source_url: str
    parser: ParserName
    job_id: str

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["sponsorship"] = self.sponsorship.value
        data["parser"] = self.parser.value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Job:
        sponsorship_raw = data.get("sponsorship", Sponsorship.UNKNOWN.value)
        try:
            sponsorship = Sponsorship(sponsorship_raw)
        except ValueError:
            sponsorship = Sponsorship.UNKNOWN

        parser_raw = data.get("parser", ParserName.MARKDOWN_TABLE.value)
        try:
            parser = ParserName(parser_raw)
        except ValueError:
            parser = ParserName.MARKDOWN_TABLE

        return cls(
            company=str(data.get("company") or ""),
            role=str(data.get("role") or ""),
            location=str(data.get("location") or ""),
            apply_url=data.get("apply_url") or None,
            added=data.get("added") or None,
            closed=bool(data.get("closed", False)),
            sponsorship=sponsorship,
            source_repo=str(data.get("source_repo") or ""),
            source_url=str(data.get("source_url") or ""),
            parser=parser,
            job_id=str(data.get("job_id") or ""),
        )


@dataclass(slots=True)
class ParseResult:
    jobs: list[Job]
    parser: ParserName
    confidence: float
    warnings: list[str] = field(default_factory=list)
    table_detected: bool = False
    columns_mapped: bool = False


@dataclass(slots=True)
class RepositoryConfig:
    name: str
    repo: str
    branch: str
    readme_path: str = "README.md"
    enabled: bool = True

    @property
    def owner(self) -> str:
        return self.repo.split("/", 1)[0]

    @property
    def repository(self) -> str:
        return self.repo.split("/", 1)[1]

    @property
    def source_url(self) -> str:
        return (
            f"https://github.com/{self.repo}/blob/{self.branch}/{self.readme_path}"
        )


@dataclass(slots=True)
class FilterConfig:
    include_keywords: list[str] = field(default_factory=list)
    exclude_keywords: list[str] = field(default_factory=list)
    locations: list[str] = field(default_factory=list)
    exclude_no_sponsorship: bool = False
    exclude_us_citizenship_required: bool = False


@dataclass(slots=True)
class AIConfig:
    enabled: bool = True
    provider: str = "github_models"
    model: str = "openai/gpt-4.1-mini"
    minimum_rule_parser_confidence: float = 0.8
    max_input_characters: int = 50_000


@dataclass(slots=True)
class NotificationConfig:
    max_jobs_per_message: int = 5
    max_jobs_per_run: int = 30


@dataclass(slots=True)
class AppConfig:
    repositories: list[RepositoryConfig]
    filters: FilterConfig
    ai: AIConfig
    notifications: NotificationConfig


@dataclass(slots=True)
class RepositoryState:
    last_readme_sha: str | None = None
    last_success_at: str | None = None
    last_parser: str | None = None
    last_parser_confidence: float | None = None
    last_error: str | None = None
    initialized: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "last_readme_sha": self.last_readme_sha,
            "last_success_at": self.last_success_at,
            "last_parser": self.last_parser,
            "last_parser_confidence": self.last_parser_confidence,
            "last_error": self.last_error,
            "initialized": self.initialized,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> RepositoryState:
        if not isinstance(data, dict):
            return cls()
        return cls(
            last_readme_sha=data.get("last_readme_sha") or data.get("last_sha"),
            last_success_at=data.get("last_success_at") or data.get("last_run"),
            last_parser=data.get("last_parser"),
            last_parser_confidence=_as_optional_float(
                data.get("last_parser_confidence")
            ),
            last_error=data.get("last_error"),
            initialized=bool(
                data.get("initialized", bool(data.get("last_readme_sha") or data.get("last_sha")))
            ),
        )


def _as_optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
