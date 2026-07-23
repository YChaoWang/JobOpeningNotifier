"""Shared parser helpers and column alias maps."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from urllib.parse import urljoin

from internship_monitor.models import ParseResult, ParserName
from internship_monitor.normalization import (
    build_job,
    detect_closed,
    detect_sponsorship,
    is_repeated_company_marker,
    normalize_text,
)

COMPANY_ALIASES = {"company", "employer", "organization", "org"}
ROLE_ALIASES = {"role", "position", "title", "internship", "job"}
LOCATION_ALIASES = {"location", "locations", "office", "offices"}
APPLY_ALIASES = {
    "apply",
    "application",
    "link",
    "application link",
    "applications",
    "url",
}
ADDED_ALIASES = {"added", "date", "age", "posted", "posting date"}

MD_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)]+)\)")
HTML_A_RE = re.compile(
    r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
HTML_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
TAG_RE = re.compile(r"<[^>]+>")
IMG_ALT_RE = re.compile(r'<img\b[^>]*alt=["\']([^"\']*)["\'][^>]*>', re.IGNORECASE)


@dataclass(slots=True)
class ColumnMap:
    company: int | None = None
    role: int | None = None
    location: int | None = None
    apply: int | None = None
    added: int | None = None

    @property
    def required_mapped(self) -> bool:
        return self.company is not None and self.role is not None


def normalize_header(value: str) -> str:
    text = TAG_RE.sub(" ", value or "")
    text = html.unescape(text)
    text = normalize_text(text)
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def map_columns(headers: list[str]) -> ColumnMap:
    mapping = ColumnMap()
    for index, header in enumerate(headers):
        key = normalize_header(header)
        if mapping.company is None and key in COMPANY_ALIASES:
            mapping.company = index
        elif mapping.role is None and key in ROLE_ALIASES:
            mapping.role = index
        elif mapping.location is None and key in LOCATION_ALIASES:
            mapping.location = index
        elif mapping.apply is None and key in APPLY_ALIASES:
            mapping.apply = index
        elif mapping.added is None and key in ADDED_ALIASES:
            mapping.added = index
    return mapping


def cell_at(cells: list[str], index: int | None) -> str:
    if index is None or index < 0 or index >= len(cells):
        return ""
    return cells[index]


def extract_links(cell: str, *, base_url: str | None = None) -> list[str]:
    links: list[str] = []
    for match in HTML_A_RE.finditer(cell or ""):
        href = html.unescape(match.group(1).strip())
        if href and not href.startswith("#"):
            links.append(_absolutize(href, base_url))
    for match in MD_LINK_RE.finditer(cell or ""):
        href = match.group(2).strip()
        if href and not href.startswith("#"):
            links.append(_absolutize(href, base_url))
    # Prefer non-image / non-simplify tracker links when possible.
    return links


def choose_apply_url(cell: str, *, base_url: str | None = None) -> str | None:
    links = extract_links(cell, base_url=base_url)
    if not links:
        return None

    def score(url: str) -> tuple[int, int]:
        lowered = url.lower()
        # Lower is better. Prefer direct ATS links over Simplify tracker/company pages.
        penalty = 0
        if "simplify.jobs/" in lowered:
            penalty += 20
        if "imgur.com" in lowered or lowered.endswith((".png", ".jpg", ".svg", ".gif")):
            penalty += 50
        if any(
            token in lowered
            for token in (
                "ashbyhq.com",
                "greenhouse.io",
                "lever.co",
                "myworkdayjobs.com",
                "boards.greenhouse",
                "jobs.ashby",
            )
        ):
            penalty -= 10
        if any(token in lowered for token in ("/application", "/apply", "/job/", "/jobs/")):
            penalty -= 3
        return (penalty, len(url))

    return sorted(links, key=score)[0]


def clean_cell_text(cell: str) -> str:
    text = cell or ""
    text = HTML_BR_RE.sub("\n", text)
    text = IMG_ALT_RE.sub(lambda m: f" {m.group(1)} ", text)
    text = HTML_A_RE.sub(lambda m: f" {m.group(2)} ", text)
    text = MD_LINK_RE.sub(lambda m: f" {m.group(1)} ", text)
    text = TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = text.replace("\u200b", " ")
    # Keep newlines from <br> as separators, collapse other whitespace per line.
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    lines = [line for line in lines if line]
    return "\n".join(lines).strip()


def location_from_cell(cell: str) -> str:
    text = clean_cell_text(cell)
    parts = [part.strip() for part in re.split(r"[\n|/]+", text) if part.strip()]
    # Rejoin with "; " for multi-location readability while preserving content.
    return "; ".join(parts) if parts else text


def compute_confidence(
    *,
    table_detected: bool,
    columns_mapped: bool,
    row_count: int,
    valid_jobs: int,
    jobs_with_urls: int,
) -> float:
    if not table_detected:
        return 0.0
    if not columns_mapped:
        return 0.15
    if row_count <= 0:
        return 0.2

    valid_ratio = valid_jobs / row_count
    url_ratio = (jobs_with_urls / valid_jobs) if valid_jobs else 0.0
    confidence = 0.35 + (0.45 * valid_ratio) + (0.20 * url_ratio)
    if valid_jobs == 0:
        confidence = min(confidence, 0.25)
    return round(min(1.0, max(0.0, confidence)), 3)


def rows_to_jobs(
    rows: list[list[str]],
    columns: ColumnMap,
    *,
    source_repo: str,
    source_url: str,
    parser: ParserName,
    base_url: str | None = None,
) -> tuple[list, int, list[str]]:
    jobs = []
    rejected = 0
    warnings: list[str] = []
    previous_company = ""

    for row_index, cells in enumerate(rows):
        if not any(clean_cell_text(cell) for cell in cells):
            continue

        company_raw = cell_at(cells, columns.company)
        role_raw = cell_at(cells, columns.role)
        location_raw = cell_at(cells, columns.location)
        apply_raw = cell_at(cells, columns.apply)
        added_raw = cell_at(cells, columns.added)

        company_text = clean_cell_text(company_raw)
        if is_repeated_company_marker(company_text):
            company = previous_company
        else:
            company = company_text
            previous_company = company

        role = clean_cell_text(role_raw)
        location = location_from_cell(location_raw)
        added = clean_cell_text(added_raw)
        if added in {"-", "—", "n/a", "N/A"}:
            added = ""

        apply_url = choose_apply_url(apply_raw, base_url=base_url)
        closed = detect_closed(company_raw, role_raw, location_raw, apply_raw, added_raw)
        sponsorship = detect_sponsorship(
            company_raw, role_raw, location_raw, apply_raw, added_raw
        )

        job = build_job(
            company=company,
            role=role,
            location=location,
            apply_url=apply_url,
            added=added or None,
            closed=closed,
            sponsorship=sponsorship,
            source_repo=source_repo,
            source_url=source_url,
            parser=parser,
        )
        if job is None:
            rejected += 1
            warnings.append(f"Rejected unusable row {row_index + 1}")
            continue
        jobs.append(job)

    return jobs, rejected, warnings


def empty_result(parser: ParserName, *warnings: str) -> ParseResult:
    return ParseResult(
        jobs=[],
        parser=parser,
        confidence=0.0,
        warnings=list(warnings),
        table_detected=False,
        columns_mapped=False,
    )


def _absolutize(url: str, base_url: str | None) -> str:
    if not base_url:
        return url
    if url.startswith(("http://", "https://", "mailto:")):
        return url
    return urljoin(base_url if base_url.endswith("/") else base_url + "/", url)
