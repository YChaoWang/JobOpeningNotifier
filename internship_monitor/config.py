"""Configuration loading and validation."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

from internship_monitor.models import (
    AIConfig,
    AppConfig,
    FilterConfig,
    NotificationConfig,
    RepositoryConfig,
)

REPO_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class ConfigError(ValueError):
    """Raised when configuration is invalid."""


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path)
    if not config_path.exists():
        raise ConfigError(f"Config file not found: {config_path}")

    raw_text = config_path.read_text(encoding="utf-8")
    suffix = config_path.suffix.lower()
    try:
        if suffix in {".yaml", ".yml"}:
            raw = yaml.safe_load(raw_text)
        elif suffix == ".json":
            raw = json.loads(raw_text)
        else:
            # Prefer YAML, fall back to JSON.
            try:
                raw = yaml.safe_load(raw_text)
            except yaml.YAMLError:
                raw = json.loads(raw_text)
    except (yaml.YAMLError, json.JSONDecodeError) as exc:
        raise ConfigError(f"Failed to parse config file {config_path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError("Config root must be a mapping/object")

    return validate_config(raw)


def validate_config(raw: dict[str, Any]) -> AppConfig:
    errors: list[str] = []

    repositories_raw = raw.get("repositories")
    if not isinstance(repositories_raw, list) or not repositories_raw:
        errors.append("repositories must be a non-empty list")
        repositories_raw = []

    repositories: list[RepositoryConfig] = []
    seen_names: set[str] = set()
    seen_repos: set[str] = set()

    for index, item in enumerate(repositories_raw):
        prefix = f"repositories[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{prefix} must be a mapping")
            continue

        name = str(item.get("name") or "").strip()
        repo = str(item.get("repo") or "").strip()
        branch = str(item.get("branch") or "").strip()
        readme_path = str(item.get("readme_path") or "README.md").strip() or "README.md"
        enabled = bool(item.get("enabled", True))

        if not name:
            errors.append(f"{prefix}: missing repo name")
        elif name in seen_names:
            errors.append(f"{prefix}: duplicate repository name '{name}'")
        else:
            seen_names.add(name)

        if not repo:
            errors.append(f"{prefix}: missing repo (expected owner/repository)")
        elif not REPO_PATTERN.fullmatch(repo):
            errors.append(
                f"{prefix}: invalid owner/repository format '{repo}' "
                "(expected 'owner/repository')"
            )
        elif repo.lower() in seen_repos:
            errors.append(f"{prefix}: duplicate repository entry '{repo}'")
        else:
            seen_repos.add(repo.lower())

        if not branch:
            errors.append(f"{prefix}: missing branch")

        if name and repo and branch and REPO_PATTERN.fullmatch(repo):
            repositories.append(
                RepositoryConfig(
                    name=name,
                    repo=repo,
                    branch=branch,
                    readme_path=readme_path,
                    enabled=enabled,
                )
            )

    filters_raw = raw.get("filters", {})
    if filters_raw is None:
        filters_raw = {}
    if not isinstance(filters_raw, dict):
        errors.append("filters must be a mapping")
        filters_raw = {}

    include_keywords = _as_string_list(
        filters_raw.get("include_keywords", []),
        "filters.include_keywords",
        errors,
    )
    exclude_keywords = _as_string_list(
        filters_raw.get("exclude_keywords", []),
        "filters.exclude_keywords",
        errors,
    )
    locations = _as_string_list(
        filters_raw.get("locations", []),
        "filters.locations",
        errors,
    )

    exclude_no_sponsorship = filters_raw.get("exclude_no_sponsorship", False)
    exclude_us_citizenship_required = filters_raw.get(
        "exclude_us_citizenship_required", False
    )
    if not isinstance(exclude_no_sponsorship, bool):
        errors.append("filters.exclude_no_sponsorship must be a boolean")
        exclude_no_sponsorship = False
    if not isinstance(exclude_us_citizenship_required, bool):
        errors.append("filters.exclude_us_citizenship_required must be a boolean")
        exclude_us_citizenship_required = False

    ai_raw = raw.get("ai", {})
    if ai_raw is None:
        ai_raw = {}
    if not isinstance(ai_raw, dict):
        errors.append("ai must be a mapping")
        ai_raw = {}

    ai_enabled = ai_raw.get("enabled", True)
    if not isinstance(ai_enabled, bool):
        errors.append("ai.enabled must be a boolean")
        ai_enabled = True

    provider = str(ai_raw.get("provider") or "github_models")
    model = str(ai_raw.get("model") or "openai/gpt-4.1-mini")

    min_confidence = ai_raw.get("minimum_rule_parser_confidence", 0.8)
    try:
        min_confidence_f = float(min_confidence)
        if not 0.0 <= min_confidence_f <= 1.0:
            raise ValueError
    except (TypeError, ValueError):
        errors.append(
            "ai.minimum_rule_parser_confidence must be a number between 0 and 1"
        )
        min_confidence_f = 0.8

    max_chars = ai_raw.get("max_input_characters", 50_000)
    try:
        max_chars_i = int(max_chars)
        if max_chars_i <= 0:
            raise ValueError
    except (TypeError, ValueError):
        errors.append("ai.max_input_characters must be a positive integer")
        max_chars_i = 50_000

    notifications_raw = raw.get("notifications", {})
    if notifications_raw is None:
        notifications_raw = {}
    if not isinstance(notifications_raw, dict):
        errors.append("notifications must be a mapping")
        notifications_raw = {}

    max_per_message = notifications_raw.get("max_jobs_per_message", 5)
    max_per_run = notifications_raw.get("max_jobs_per_run", 30)
    try:
        max_per_message_i = int(max_per_message)
        if max_per_message_i <= 0:
            raise ValueError
    except (TypeError, ValueError):
        errors.append("notifications.max_jobs_per_message must be a positive integer")
        max_per_message_i = 5
    try:
        max_per_run_i = int(max_per_run)
        if max_per_run_i <= 0:
            raise ValueError
    except (TypeError, ValueError):
        errors.append("notifications.max_jobs_per_run must be a positive integer")
        max_per_run_i = 30

    if errors:
        raise ConfigError("Invalid configuration:\n- " + "\n- ".join(errors))

    return AppConfig(
        repositories=repositories,
        filters=FilterConfig(
            include_keywords=include_keywords,
            exclude_keywords=exclude_keywords,
            locations=locations,
            exclude_no_sponsorship=exclude_no_sponsorship,
            exclude_us_citizenship_required=exclude_us_citizenship_required,
        ),
        ai=AIConfig(
            enabled=ai_enabled,
            provider=provider,
            model=model,
            minimum_rule_parser_confidence=min_confidence_f,
            max_input_characters=max_chars_i,
        ),
        notifications=NotificationConfig(
            max_jobs_per_message=max_per_message_i,
            max_jobs_per_run=max_per_run_i,
        ),
    )


def _as_string_list(value: Any, field_name: str, errors: list[str]) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        errors.append(f"{field_name} must be a list of strings")
        return []
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            errors.append(f"{field_name} must contain only strings")
            continue
        cleaned = item.strip()
        if cleaned:
            result.append(cleaned)
    return result
