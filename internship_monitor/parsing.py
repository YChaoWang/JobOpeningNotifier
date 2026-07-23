"""README parsing coordinator (rule parsers + optional AI fallback)."""

from __future__ import annotations

import logging
from collections.abc import Sequence

from internship_monitor.models import AIConfig, ParseResult, RepositoryConfig
from internship_monitor.ports import ReadmeParser

logger = logging.getLogger(__name__)


class ParseCoordinator:
    """Open/closed: add rule parsers without changing orchestration."""

    def __init__(
        self,
        rule_parsers: Sequence[ReadmeParser],
        *,
        ai_parser: ReadmeParser | None,
        ai_config: AIConfig,
    ) -> None:
        self._rule_parsers = list(rule_parsers)
        self._ai_parser = ai_parser
        self._ai_config = ai_config

    def parse(
        self, content: str, repository: RepositoryConfig
    ) -> tuple[ParseResult, int, bool]:
        """Return (best_result, rejected_estimate, parse_ok)."""
        rule_results = [parser.parse(content, repository) for parser in self._rule_parsers]
        rule_result = _best_result(rule_results)
        rejected_estimate = _estimate_rejected(rule_result)

        if not self._needs_ai(content, rule_result):
            return rule_result, rejected_estimate, True

        if self._ai_parser is None:
            return rule_result, rejected_estimate, bool(rule_result.jobs)

        logger.info(
            "[%s] Rule parser insufficient (parser=%s confidence=%.3f); "
            "attempting AI fallback",
            repository.name,
            rule_result.parser.value,
            rule_result.confidence,
        )
        ai_result = self._ai_parser.parse(content, repository)
        if ai_result.jobs:
            return ai_result, 0, True

        merged = ParseResult(
            jobs=rule_result.jobs,
            parser=rule_result.parser,
            confidence=rule_result.confidence,
            warnings=list(rule_result.warnings) + list(ai_result.warnings),
            table_detected=rule_result.table_detected,
            columns_mapped=rule_result.columns_mapped,
        )
        if merged.jobs:
            return merged, rejected_estimate, True
        return merged, rejected_estimate, False

    def _needs_ai(self, content: str, result: ParseResult) -> bool:
        if not self._ai_config.enabled or self._ai_parser is None:
            return False
        threshold = self._ai_config.minimum_rule_parser_confidence
        if not result.table_detected:
            return _looks_like_job_listing(content)
        if not result.columns_mapped:
            return True
        if result.confidence < threshold:
            return True
        if not result.jobs and _looks_like_job_listing(content):
            return True
        return False


def _best_result(results: Sequence[ParseResult]) -> ParseResult:
    if not results:
        raise ValueError("At least one parser result is required")

    def key(result: ParseResult) -> tuple[float, int, int]:
        return (
            result.confidence,
            len(result.jobs),
            1 if result.columns_mapped else 0,
        )

    best = results[0]
    for candidate in results[1:]:
        if key(candidate) > key(best):
            best = candidate
    return best


def _estimate_rejected(result: ParseResult) -> int:
    return sum(
        1 for warning in result.warnings if warning.lower().startswith("rejected")
    )


def _looks_like_job_listing(content: str) -> bool:
    lower = content.lower()
    signals = sum(
        1
        for token in (
            "internship",
            "intern",
            "software engineer",
            "apply",
            "company",
            "location",
            "<table",
            "| company",
        )
        if token in lower
    )
    return signals >= 3
