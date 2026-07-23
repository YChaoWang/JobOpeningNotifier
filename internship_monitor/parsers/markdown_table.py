"""Deterministic Markdown job-table parser."""

from __future__ import annotations

import re

from internship_monitor.models import ParseResult, ParserName, RepositoryConfig
from internship_monitor.parsers import (
    compute_confidence,
    empty_result,
    map_columns,
    rows_to_jobs,
)

SEPARATOR_RE = re.compile(r"^\s*:?-{3,}:?\s*$")


class MarkdownTableParser:
    name = ParserName.MARKDOWN_TABLE.value

    def parse(self, content: str, repository: RepositoryConfig) -> ParseResult:
        tables = _extract_markdown_tables(content)
        if not tables:
            return empty_result(
                ParserName.MARKDOWN_TABLE, "No Markdown job table detected"
            )

        best: ParseResult | None = None
        for headers, rows in tables:
            columns = map_columns(headers)
            if not columns.required_mapped:
                candidate = ParseResult(
                    jobs=[],
                    parser=ParserName.MARKDOWN_TABLE,
                    confidence=0.15,
                    warnings=["Markdown table found but required columns missing"],
                    table_detected=True,
                    columns_mapped=False,
                )
            else:
                jobs, rejected, warnings = rows_to_jobs(
                    rows,
                    columns,
                    source_repo=repository.repo,
                    source_url=repository.source_url,
                    parser=ParserName.MARKDOWN_TABLE,
                    base_url=f"https://github.com/{repository.repo}/blob/{repository.branch}/",
                )
                jobs_with_urls = sum(1 for job in jobs if job.apply_url)
                confidence = compute_confidence(
                    table_detected=True,
                    columns_mapped=True,
                    row_count=len(rows),
                    valid_jobs=len(jobs),
                    jobs_with_urls=jobs_with_urls,
                )
                if rejected:
                    warnings.append(f"Rejected {rejected} malformed/unusable rows")
                candidate = ParseResult(
                    jobs=jobs,
                    parser=ParserName.MARKDOWN_TABLE,
                    confidence=confidence,
                    warnings=warnings,
                    table_detected=True,
                    columns_mapped=True,
                )

            if best is None or candidate.confidence > best.confidence or (
                candidate.confidence == best.confidence
                and len(candidate.jobs) > len(best.jobs)
            ):
                best = candidate

        assert best is not None
        return best


def _extract_markdown_tables(content: str) -> list[tuple[list[str], list[list[str]]]]:
    lines = content.splitlines()
    tables: list[tuple[list[str], list[list[str]]]] = []
    index = 0
    while index < len(lines) - 1:
        header_line = lines[index]
        sep_line = lines[index + 1]
        if not _looks_like_header(header_line) or not _looks_like_separator(sep_line):
            index += 1
            continue

        headers = _split_row(header_line)
        if len(headers) < 2:
            index += 1
            continue

        rows: list[list[str]] = []
        index += 2
        while index < len(lines):
            row_line = lines[index]
            if not row_line.strip() or not row_line.strip().startswith("|"):
                # Allow continuation without leading pipe for some loose tables.
                if "|" not in row_line or _looks_like_separator(row_line):
                    break
            cells = _split_row(row_line)
            if cells:
                # Pad / trim to header width for resilience.
                if len(cells) < len(headers):
                    cells = cells + [""] * (len(headers) - len(cells))
                elif len(cells) > len(headers):
                    cells = cells[: len(headers)]
                rows.append(cells)
            index += 1
        tables.append((headers, rows))
    return tables


def _looks_like_header(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("|") and stripped.count("|") >= 3


def _looks_like_separator(line: str) -> bool:
    stripped = line.strip().strip("|").strip()
    if not stripped or "|" not in line:
        return False
    parts = [part.strip() for part in stripped.split("|")]
    return bool(parts) and all(SEPARATOR_RE.match(part or "---") for part in parts)


def _split_row(line: str) -> list[str]:
    """Split a Markdown table row, respecting escaped pipes."""
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]

    cells: list[str] = []
    current: list[str] = []
    escaped = False
    for char in stripped:
        if escaped:
            current.append(char)
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == "|":
            cells.append("".join(current).strip())
            current = []
            continue
        current.append(char)
    cells.append("".join(current).strip())
    return cells
