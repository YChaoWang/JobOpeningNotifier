"""Deterministic HTML job-table parser."""

from __future__ import annotations

from bs4 import BeautifulSoup, NavigableString, Tag

from internship_monitor.models import ParseResult, ParserName, RepositoryConfig
from internship_monitor.parsers import (
    compute_confidence,
    empty_result,
    map_columns,
    rows_to_jobs,
)


class HtmlTableParser:
    name = ParserName.HTML_TABLE.value

    def parse(self, content: str, repository: RepositoryConfig) -> ParseResult:
        if "<table" not in content.lower():
            return empty_result(ParserName.HTML_TABLE, "No HTML table detected")

        soup = BeautifulSoup(content, "html.parser")
        tables = soup.find_all("table")
        if not tables:
            return empty_result(ParserName.HTML_TABLE, "No HTML table detected")

        best: ParseResult | None = None
        base_url = f"https://github.com/{repository.repo}/blob/{repository.branch}/"

        for table in tables:
            headers, rows = _table_to_matrix(table)
            if not headers:
                continue

            columns = map_columns(headers)
            if not columns.required_mapped:
                candidate = ParseResult(
                    jobs=[],
                    parser=ParserName.HTML_TABLE,
                    confidence=0.15,
                    warnings=["HTML table found but required columns missing"],
                    table_detected=True,
                    columns_mapped=False,
                )
            else:
                jobs, rejected, warnings = rows_to_jobs(
                    rows,
                    columns,
                    source_repo=repository.repo,
                    source_url=repository.source_url,
                    parser=ParserName.HTML_TABLE,
                    base_url=base_url,
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
                    parser=ParserName.HTML_TABLE,
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

        if best is None:
            return empty_result(
                ParserName.HTML_TABLE, "HTML tables present but none usable"
            )
        return best


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
