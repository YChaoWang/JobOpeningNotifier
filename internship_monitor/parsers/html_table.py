"""Deterministic HTML job-table parser."""

from __future__ import annotations

import logging

from bs4 import BeautifulSoup, NavigableString, Tag

from internship_monitor.jobs import dedupe_jobs
from internship_monitor.models import ParseResult, ParserName, RepositoryConfig
from internship_monitor.parsers import (
    compute_confidence,
    empty_result,
    map_columns,
    rows_to_jobs,
)

logger = logging.getLogger(__name__)

# Bumped when HTML parsing coverage expands (e.g. single → all compatible tables).
HTML_TABLE_COVERAGE_VERSION = "html-all-tables-v1"


class HtmlTableParser:
    name = ParserName.HTML_TABLE.value
    coverage_version = HTML_TABLE_COVERAGE_VERSION

    def parse(self, content: str, repository: RepositoryConfig) -> ParseResult:
        if "<table" not in content.lower():
            return empty_result(ParserName.HTML_TABLE, "No HTML table detected")

        soup = BeautifulSoup(content, "html.parser")
        tables = soup.find_all("table")
        if not tables:
            return empty_result(ParserName.HTML_TABLE, "No HTML table detected")

        base_url = f"https://github.com/{repository.repo}/blob/{repository.branch}/"
        all_jobs = []
        warnings: list[str] = []
        total_rejected = 0
        total_rows = 0
        compatible = 0
        skipped_unmapped = 0

        for index, table in enumerate(tables, start=1):
            try:
                headers, rows = _table_to_matrix(table)
            except Exception as exc:  # noqa: BLE001 - one bad table must not fail parse
                warnings.append(f"Skipped malformed HTML table #{index}: {exc}")
                continue

            if not headers:
                continue

            columns = map_columns(headers)
            if not columns.required_mapped:
                skipped_unmapped += 1
                continue

            compatible += 1
            total_rows += len(rows)
            jobs, rejected, table_warnings = rows_to_jobs(
                rows,
                columns,
                source_repo=repository.repo,
                source_url=repository.source_url,
                parser=ParserName.HTML_TABLE,
                base_url=base_url,
            )
            all_jobs.extend(jobs)
            total_rejected += rejected
            warnings.extend(table_warnings)

        raw_count = len(all_jobs)
        unique_jobs = dedupe_jobs(all_jobs)

        logger.info(
            "[%s] HTML tables=%d compatible=%d raw_jobs=%d unique_jobs=%d",
            repository.name,
            len(tables),
            compatible,
            raw_count,
            len(unique_jobs),
        )

        if compatible == 0:
            message = "HTML tables present but none with required job columns"
            if skipped_unmapped:
                message = (
                    f"{message} (skipped {skipped_unmapped} unrelated/unmapped table(s))"
                )
            return ParseResult(
                jobs=[],
                parser=ParserName.HTML_TABLE,
                confidence=0.15,
                warnings=[message, *warnings],
                table_detected=True,
                columns_mapped=False,
            )

        jobs_with_urls = sum(1 for job in unique_jobs if job.apply_url)
        confidence = compute_confidence(
            table_detected=True,
            columns_mapped=True,
            row_count=total_rows,
            valid_jobs=len(unique_jobs),
            jobs_with_urls=jobs_with_urls,
        )
        if total_rejected:
            warnings.append(f"Rejected {total_rejected} malformed/unusable rows")
        if skipped_unmapped:
            warnings.append(
                f"Ignored {skipped_unmapped} unrelated/unmapped HTML table(s)"
            )
        if raw_count != len(unique_jobs):
            warnings.append(
                f"Deduped {raw_count - len(unique_jobs)} duplicate job(s) across tables"
            )

        return ParseResult(
            jobs=unique_jobs,
            parser=ParserName.HTML_TABLE,
            confidence=confidence,
            warnings=warnings,
            table_detected=True,
            columns_mapped=True,
        )


def _table_to_matrix(table: Tag) -> tuple[list[str], list[list[str]]]:
    header_cells = _extract_header_cells(table)
    body_rows = _extract_body_rows(table)

    if not header_cells and body_rows:
        # Some READMEs omit thead; use first row as header.
        header_cells = body_rows[0]
        body_rows = body_rows[1:]

    headers = [_cell_html(cell) for cell in header_cells]
    width = max([len(headers)] + [len(row) for row in body_rows], default=0)
    if width == 0:
        return [], []

    # Expand rowspans practically across subsequent rows.
    active_rowspans: dict[int, tuple[str, int]] = {}
    matrix: list[list[str]] = []

    for raw_cells in body_rows:
        row: list[str] = [""] * width
        col = 0
        cell_index = 0

        while col < width:
            if col in active_rowspans:
                html_value, remaining = active_rowspans[col]
                row[col] = html_value
                if remaining <= 1:
                    del active_rowspans[col]
                else:
                    active_rowspans[col] = (html_value, remaining - 1)
                col += 1
                continue

            if cell_index >= len(raw_cells):
                col += 1
                continue

            cell = raw_cells[cell_index]
            cell_index += 1
            colspan = _span(cell, "colspan")
            rowspan = _span(cell, "rowspan")
            html_value = _cell_html(cell)

            for offset in range(colspan):
                target = col + offset
                if target >= width:
                    break
                row[target] = html_value
                if rowspan > 1 and offset == 0:
                    active_rowspans[target] = (html_value, rowspan - 1)
            col += colspan

        matrix.append(row)

    return headers, matrix


def _extract_header_cells(table: Tag) -> list[Tag]:
    thead = table.find("thead")
    if thead:
        row = thead.find("tr")
        if row:
            return row.find_all(["th", "td"], recursive=False)
    # Fallback: first header row in table.
    for row in table.find_all("tr"):
        cells = row.find_all(["th", "td"], recursive=False)
        if cells and all(cell.name == "th" for cell in cells):
            return cells
        if cells and row.find("th"):
            return cells
        break
    return []


def _extract_body_rows(table: Tag) -> list[list[Tag]]:
    rows: list[list[Tag]] = []
    bodies = table.find_all("tbody")
    candidates = []
    if bodies:
        for body in bodies:
            candidates.extend(body.find_all("tr"))
    else:
        candidates = table.find_all("tr")

    header_seen = False
    for row in candidates:
        cells = row.find_all(["th", "td"], recursive=False)
        if not cells:
            continue
        # Skip pure header rows once.
        if not header_seen and all(cell.name == "th" for cell in cells):
            header_seen = True
            continue
        if not header_seen and row.parent and row.parent.name == "thead":
            header_seen = True
            continue
        rows.append(cells)
    return rows


def _span(cell: Tag, attr: str) -> int:
    try:
        value = int(cell.get(attr, 1))
    except (TypeError, ValueError):
        return 1
    return max(1, value)


def _cell_html(cell: Tag | NavigableString | None) -> str:
    if cell is None:
        return ""
    if isinstance(cell, NavigableString):
        return str(cell)
    # Preserve anchors and <br> for downstream link/location parsing.
    return "".join(str(child) for child in cell.contents)
