"""Job identity, URL normalization, and record validation."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from internship_monitor.models import Job, ParserName, Sponsorship

TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_content",
    "utm_term",
    "ref",
    "referral",
    "gh_src",
    "source",
}

WHITESPACE_RE = re.compile(r"\s+")
EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001F9FF"
    "\U00002600-\U000027BF"
    "\U0001F1E0-\U0001F1FF"
    "\U00002702-\U000027B0"
    "\U000024C2-\U0001F251"
    "]+",
    flags=re.UNICODE,
)
LEGACY_COMPANY_MARKERS = {"↳", "^", "└", "└─", "┗", "->"}


def normalize_text(value: str | None) -> str:
    if not value:
        return ""
    text = EMOJI_RE.sub(" ", value)
    text = text.replace("\u200b", " ").replace("\xa0", " ")
    text = WHITESPACE_RE.sub(" ", text).strip().lower()
    return text


def is_repeated_company_marker(value: str | None) -> bool:
    if value is None:
        return False
    cleaned = normalize_text(value)
    if not cleaned:
        return True
    stripped = value.strip()
    if stripped in LEGACY_COMPANY_MARKERS:
        return True
    # Common markdown/HTML continuation markers after emoji cleanup.
    return cleaned in {"", "↳", "^"} or stripped.startswith("↳")


def normalize_url(url: str | None) -> str | None:
    if not url:
        return None
    candidate = url.strip()
    if not candidate:
        return None
    if candidate.startswith("//"):
        candidate = "https:" + candidate

    parts = urlsplit(candidate)
    if not parts.scheme or not parts.netloc:
        return None

    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"}:
        return None

    hostname = parts.hostname.lower() if parts.hostname else ""
    if not hostname:
        return None

    netloc = hostname
    if parts.port:
        netloc = f"{hostname}:{parts.port}"

    path = parts.path or ""
    if path != "/" and path.endswith("/"):
        path = path[:-1]

    query_items = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in TRACKING_PARAMS
    ]
    query_items.sort(key=lambda item: (item[0].lower(), item[1]))
    query = urlencode(query_items, doseq=True)

    return urlunsplit((scheme, netloc, path, query, ""))


def make_job_id(
    *,
    apply_url: str | None,
    company: str,
    role: str,
    location: str,
    source_repo: str,
) -> str:
    normalized_url = normalize_url(apply_url)
    if normalized_url:
        identity = f"url:{normalized_url}"
    else:
        identity = "|".join(
            [
                "fallback",
                normalize_text(company),
                normalize_text(role),
                normalize_text(location),
                normalize_text(source_repo),
            ]
        )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


_AGE_RE = re.compile(r"^(\d+)\s*(d|mo|w|h|m|y)s?$", re.IGNORECASE)
_ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")
_SLASH_DATE_RE = re.compile(r"^(\d{4})/(\d{2})/(\d{2})")


def parse_added_datetime(
    added: str | None, *, now: datetime | None = None
) -> datetime | None:
    """Parse listing ``added`` / last-update text into a timezone-aware datetime."""
    if not added:
        return None
    text = added.strip()
    if not text or text in {"-", "—", "n/a", "N/A"}:
        return None

    ref = now or datetime.now(timezone.utc)

    iso = _ISO_DATE_RE.match(text)
    if iso:
        return datetime(
            int(iso.group(1)),
            int(iso.group(2)),
            int(iso.group(3)),
            tzinfo=timezone.utc,
        )

    slash = _SLASH_DATE_RE.match(text)
    if slash:
        return datetime(
            int(slash.group(1)),
            int(slash.group(2)),
            int(slash.group(3)),
            tzinfo=timezone.utc,
        )

    for fmt in ("%m/%d/%Y", "%m-%d-%Y", "%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue

    age = _AGE_RE.match(text.replace(" ", ""))
    if age:
        amount = int(age.group(1))
        unit = age.group(2).lower()
        deltas = {
            "h": timedelta(hours=amount),
            "d": timedelta(days=amount),
            "w": timedelta(weeks=amount),
            "mo": timedelta(days=30 * amount),
            "m": timedelta(days=30 * amount),
            "y": timedelta(days=365 * amount),
        }
        delta = deltas.get(unit)
        if delta is None:
            return None
        return ref - delta

    return None


def format_added_date(added: str | None, *, now: datetime | None = None) -> str | None:
    """Format listing dates as ``YYYY/MM/DD`` for Discord display."""
    parsed = parse_added_datetime(added, now=now)
    if parsed is None:
        return None
    return parsed.strftime("%Y/%m/%d")


def order_jobs_by_last_update(
    jobs: list[Job],
    *,
    newest_first: bool = True,
    now: datetime | None = None,
) -> list[Job]:
    """Order jobs by listing last-update date (``added`` field).

    Newest-first is used for Discord so recently updated roles pop first.
    Jobs without a parseable date keep their relative input order at the end.
    """
    if len(jobs) <= 1:
        return list(jobs)

    ref = now or datetime.now(timezone.utc)
    dated: list[tuple[datetime, int, Job]] = []
    undated: list[tuple[int, Job]] = []
    for index, job in enumerate(jobs):
        parsed = parse_added_datetime(job.added, now=ref)
        if parsed is None:
            undated.append((index, job))
        else:
            dated.append((parsed, index, job))

    dated.sort(key=lambda item: (item[0], item[1]), reverse=newest_first)
    ordered = [job for _, _, job in dated]
    ordered.extend(job for _, job in undated)
    return ordered


def order_jobs_oldest_first(jobs: list[Job]) -> list[Job]:
    """Order jobs oldest → newest by last update (compat helper)."""
    return order_jobs_by_last_update(jobs, newest_first=False)


def detect_sponsorship(*texts: str) -> Sponsorship:
    combined = " ".join(texts)
    if "🇺🇸" in combined or "u.s. citizenship" in combined.lower() or "us citizenship" in combined.lower():
        return Sponsorship.CITIZENSHIP_REQUIRED
    if "🛂" in combined or "does not offer sponsorship" in combined.lower() or "no sponsorship" in combined.lower():
        return Sponsorship.UNAVAILABLE
    return Sponsorship.UNKNOWN


def detect_closed(*texts: str) -> bool:
    for text in texts:
        if not text:
            continue
        if "🔒" in text:
            return True
        lowered = text.lower()
        if "closed" in lowered and ("application" in lowered or "role" in lowered):
            return True
    return False


def is_usable_job(
    *,
    company: str,
    role: str,
    apply_url: str | None,
) -> bool:
    has_identity = bool(company.strip() and role.strip())
    has_url = normalize_url(apply_url) is not None
    return has_identity or has_url


def build_job(
    *,
    company: str,
    role: str,
    location: str,
    apply_url: str | None,
    added: str | None,
    closed: bool,
    sponsorship: Sponsorship,
    source_repo: str,
    source_url: str,
    parser: ParserName,
) -> Job | None:
    company = (company or "").strip()
    role = (role or "").strip()
    location = (location or "").strip()
    added_clean = (added or "").strip() or None

    raw_url = (apply_url or "").strip() or None
    normalized = normalize_url(raw_url) if raw_url else None
    # Keep the original URL for Discord/user-facing output; identity uses normalization.
    display_url = raw_url if normalized else None

    if not is_usable_job(company=company, role=role, apply_url=normalized or raw_url):
        return None

    job_id = make_job_id(
        apply_url=raw_url,
        company=company,
        role=role,
        location=location,
        source_repo=source_repo,
    )
    return Job(
        company=company,
        role=role,
        location=location,
        apply_url=display_url,
        added=added_clean,
        closed=closed,
        sponsorship=sponsorship,
        source_repo=source_repo,
        source_url=source_url,
        parser=parser,
        job_id=job_id,
    )


def validate_job_dict(data: dict, *, source_repo: str, source_url: str, parser: ParserName) -> Job | None:
    """Validate a loosely-typed job dict (e.g. AI output) into a Job."""
    if not isinstance(data, dict):
        return None

    company = str(data.get("company") or "").strip()
    role = str(data.get("role") or "").strip()
    location = str(data.get("location") or "").strip()
    apply_url = data.get("apply_url")
    if apply_url is not None:
        apply_url = str(apply_url).strip() or None

    added = data.get("added")
    if added is not None:
        added = str(added).strip() or None

    closed = bool(data.get("closed", False))
    if detect_closed(company, role, location, str(data.get("notes") or "")):
        closed = True

    sponsorship_raw = data.get("sponsorship", Sponsorship.UNKNOWN.value)
    try:
        sponsorship = Sponsorship(str(sponsorship_raw))
    except ValueError:
        sponsorship = detect_sponsorship(company, role, location)

    return build_job(
        company=company,
        role=role,
        location=location,
        apply_url=apply_url,
        added=added,
        closed=closed,
        sponsorship=sponsorship,
        source_repo=source_repo,
        source_url=source_url,
        parser=parser,
    )
