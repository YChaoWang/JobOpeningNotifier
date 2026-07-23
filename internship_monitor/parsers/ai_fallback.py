"""GitHub Models AI fallback parser (OpenAI-compatible structured outputs)."""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Callable

from internship_monitor.models import (
    AIConfig,
    ParseResult,
    ParserName,
    RepositoryConfig,
)
from internship_monitor.normalization import validate_job_dict
from internship_monitor.schemas import AI_JOBS_RESPONSE_FORMAT

logger = logging.getLogger(__name__)

GITHUB_MODELS_BASE_URL = "https://models.github.ai/inference"

SYSTEM_PROMPT = """You extract internship/job listings from GitHub README content.

Rules:
- Extract only genuine job or internship listings.
- Do not summarize the README.
- Do not invent missing companies, roles, locations, dates, sponsorship status, or URLs.
- Preserve application URLs exactly when possible.
- Ignore navigation links, contribution links, Discord links, repository links, and informational sections.
- Return jobs as an empty array if there are no job listings.
- Mark closed jobs as closed=true.
- Follow the provided JSON Schema exactly.

Field guidance:
- company, role, location: strings (use "" only when the cell is blank)
- apply_url, added: string or null when unknown
- closed: boolean
- sponsorship: one of available, unavailable, citizenship_required, unknown
"""

SECTION_DROP_RE = re.compile(
    r"(?im)^#{1,6}\s.*(contribut|license|faq|legend|about|note from|scope|tracker).*$"
)
BADGE_RE = re.compile(r"!\[([^\]]*)\]\([^)]+\)")
HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


class AIFallbackParser:
    name = ParserName.AI.value

    def __init__(
        self,
        config: AIConfig,
        *,
        client_factory: Callable[..., Any] | None = None,
        token: str | None = None,
    ) -> None:
        self.config = config
        self._client_factory = client_factory
        self._token = token

    def parse(self, content: str, repository: RepositoryConfig) -> ParseResult:
        warnings: list[str] = []
        if not self.config.enabled:
            return ParseResult(
                jobs=[],
                parser=ParserName.AI,
                confidence=0.0,
                warnings=["AI fallback disabled in config"],
            )

        token = self._token if self._token is not None else os.getenv("GITHUB_TOKEN")
        if not token:
            warning = (
                "AI fallback needed but GITHUB_TOKEN is unavailable; "
                "skipping AI for this repository"
            )
            logger.warning("[%s] %s", repository.name, warning)
            return ParseResult(
                jobs=[],
                parser=ParserName.AI,
                confidence=0.0,
                warnings=[warning],
            )

        prepared = prepare_readme_for_ai(
            content, max_characters=self.config.max_input_characters
        )
        if not prepared.strip():
            return ParseResult(
                jobs=[],
                parser=ParserName.AI,
                confidence=0.0,
                warnings=["No usable content remained after AI preprocessing"],
            )

        logger.info(
            "[%s] Using GitHub Models AI fallback (model=%s, input_chars=%d)",
            repository.name,
            self.config.model,
            len(prepared),
        )

        try:
            raw_text = self._complete(prepared, token=token)
        except Exception as exc:  # noqa: BLE001 - network/SDK errors are expected
            warning = f"AI request failed: {exc.__class__.__name__}"
            logger.warning("[%s] %s", repository.name, warning)
            return ParseResult(
                jobs=[],
                parser=ParserName.AI,
                confidence=0.0,
                warnings=[warning],
            )

        jobs, parse_warnings = parse_ai_jobs(
            raw_text,
            source_repo=repository.repo,
            source_url=repository.source_url,
        )
        warnings.extend(parse_warnings)

        if parse_warnings and not jobs:
            return ParseResult(
                jobs=[],
                parser=ParserName.AI,
                confidence=0.0,
                warnings=warnings,
            )

        confidence = 0.7 if jobs else 0.3
        return ParseResult(
            jobs=jobs,
            parser=ParserName.AI,
            confidence=confidence,
            warnings=warnings,
            table_detected=True,
            columns_mapped=True,
        )

    def _complete(self, content: str, *, token: str) -> str:
        client = self._build_client(token)
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    "Extract job listings from this README content.\n\n"
                    f"{content}"
                ),
            },
        ]
        base_kwargs: dict[str, Any] = {
            "model": self.config.model,
            "temperature": 0,
            "messages": messages,
        }

        # Prefer JSON Schema Structured Outputs, then json_object, then unconstrained.
        response_formats: list[dict[str, Any] | None] = [
            AI_JOBS_RESPONSE_FORMAT,
            {"type": "json_object"},
            None,
        ]
        last_error: Exception | None = None
        for response_format in response_formats:
            kwargs = dict(base_kwargs)
            if response_format is not None:
                kwargs["response_format"] = response_format
            try:
                response = client.chat.completions.create(**kwargs)
                message = response.choices[0].message.content
                return message or ""
            except TypeError as exc:
                last_error = exc
                continue
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                logger.debug(
                    "AI completion with response_format=%s failed: %s",
                    (response_format or {}).get("type") if response_format else None,
                    exc.__class__.__name__,
                )
                continue

        raise RuntimeError(
            f"AI completion failed: {last_error.__class__.__name__ if last_error else 'unknown'}"
        )

    def _build_client(self, token: str) -> Any:
        if self._client_factory is not None:
            return self._client_factory(api_key=token, base_url=GITHUB_MODELS_BASE_URL)
        from openai import OpenAI

        return OpenAI(api_key=token, base_url=GITHUB_MODELS_BASE_URL)


def prepare_readme_for_ai(content: str, *, max_characters: int) -> str:
    text = HTML_COMMENT_RE.sub("", content or "")
    text = BADGE_RE.sub("", text)

    lines = text.splitlines()
    kept: list[str] = []
    skipping = False
    for line in lines:
        if SECTION_DROP_RE.match(line):
            skipping = True
            continue
        if skipping and re.match(r"(?m)^#{1,6}\s+", line):
            skipping = False
        if skipping:
            continue
        kept.append(line)
    text = "\n".join(kept)

    if "<table" in text.lower() or "|" in text:
        start_candidates = []
        lower = text.lower()
        for token in ("<table", "| company", "| employer", "## "):
            idx = lower.find(token)
            if idx >= 0:
                start_candidates.append(idx)
        if start_candidates:
            start = max(0, min(start_candidates) - 200)
            text = text[start:]

    text = text.strip()
    if len(text) > max_characters:
        text = text[:max_characters]
    return text


def parse_ai_jobs(
    raw_text: str,
    *,
    source_repo: str,
    source_url: str,
) -> tuple[list, list[str]]:
    warnings: list[str] = []
    payload = _load_json_payload(raw_text)
    if payload is None:
        return [], ["AI returned invalid JSON; ignoring output"]

    jobs_raw = _extract_jobs_array(payload)
    if jobs_raw is None:
        return [], ["AI JSON did not match structured output schema (missing jobs array)"]

    jobs = []
    rejected = 0
    for item in jobs_raw:
        job = validate_job_dict(
            item if isinstance(item, dict) else {},
            source_repo=source_repo,
            source_url=source_url,
            parser=ParserName.AI,
        )
        if job is None:
            rejected += 1
            continue
        jobs.append(job)

    if rejected:
        warnings.append(f"Rejected {rejected} invalid/incomplete AI job records")
    return jobs, warnings


def _extract_jobs_array(payload: Any) -> list[Any] | None:
    """Accept structured-output object `{jobs:[...]}` or legacy bare arrays."""
    if isinstance(payload, dict):
        jobs = payload.get("jobs")
        if isinstance(jobs, list):
            return jobs
        for key in ("items", "listings", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
        return None
    if isinstance(payload, list):
        return payload
    return None


def _load_json_payload(raw_text: str) -> Any | None:
    text = (raw_text or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"(\[.*\]|\{.*\})", text, re.DOTALL)
        if not match:
            return None
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            return None
