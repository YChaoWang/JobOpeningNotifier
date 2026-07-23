"""Discord webhook notifications with retries and batching."""

from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import urlsplit

import requests

from internship_monitor.models import Job, Sponsorship

logger = logging.getLogger(__name__)

MAX_EMBEDS_PER_MESSAGE = 10
MAX_RETRIES = 3


class DiscordError(Exception):
    """Raised when Discord rejects a notification permanently."""


def validate_webhook_url(webhook_url: str) -> str:
    """Return a cleaned webhook URL or raise ValueError with a clear message."""
    url = (webhook_url or "").strip().strip('"').strip("'")
    if not url:
        raise ValueError("DISCORD_WEBHOOK_URL is required")

    placeholder_markers = (
        "...",
        "YOUR_WEBHOOK_URL",
        "NEW_ID",
        "NEW_TOKEN",
        "<id>",
        "<token>",
        "YOUR_ID",
        "YOUR_TOKEN",
    )
    if any(marker in url for marker in placeholder_markers):
        raise ValueError(
            "DISCORD_WEBHOOK_URL still looks like a placeholder example. "
            "Paste the real URL from Discord → channel settings → Integrations → "
            "Webhooks (it looks like https://discord.com/api/webhooks/123456.../abcdef...)."
        )

    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError(
            "DISCORD_WEBHOOK_URL must be a full URL starting with https:// "
            f"(got {redact_webhook(url)!r})"
        )
    if "discord.com" not in parts.netloc and "discordapp.com" not in parts.netloc:
        raise ValueError(
            "DISCORD_WEBHOOK_URL host should be discord.com or discordapp.com "
            f"(got {parts.netloc!r})"
        )
    if "/api/webhooks/" not in parts.path:
        raise ValueError(
            "DISCORD_WEBHOOK_URL path should contain /api/webhooks/"
        )

    path_parts = [p for p in parts.path.split("/") if p]
    # Expect: api, webhooks, <snowflake id>, <token>
    if len(path_parts) < 4 or path_parts[0] != "api" or path_parts[1] != "webhooks":
        raise ValueError("DISCORD_WEBHOOK_URL path is malformed")
    webhook_id = path_parts[2]
    if not webhook_id.isdigit():
        raise ValueError(
            "DISCORD_WEBHOOK_URL webhook id must be numeric. "
            "You may still be using a placeholder instead of the real URL from Discord."
        )
    return url


class DiscordNotifier:
    def __init__(
        self,
        webhook_url: str,
        *,
        session: requests.Session | None = None,
        timeout: float = 30.0,
        max_jobs_per_message: int = 5,
    ) -> None:
        self.webhook_url = validate_webhook_url(webhook_url)
        self.session = session or requests.Session()
        self.timeout = timeout
        self.max_jobs_per_message = max(1, min(max_jobs_per_message, MAX_EMBEDS_PER_MESSAGE))

    def send_test_message(self) -> None:
        payload = {
            "content": (
                "**JobOpeningNotifier test message**\n"
                "Discord webhook connectivity is working. "
                "No repositories were parsed for this test."
            )
        }
        self._post(payload)
        logger.info("Sent Discord test message to webhook %s", redact_webhook(self.webhook_url))

    def send_jobs(self, jobs: list[Job]) -> list[Job]:
        """Send jobs in batches. Returns the jobs successfully accepted by Discord."""
        sent: list[Job] = []
        for start in range(0, len(jobs), self.max_jobs_per_message):
            batch = jobs[start : start + self.max_jobs_per_message]
            embeds = [build_job_embed(job) for job in batch]
            payload: dict[str, Any] = {"embeds": embeds}
            self._post(payload)
            sent.extend(batch)
            logger.info(
                "Notified Discord of %d job(s) via webhook %s",
                len(batch),
                redact_webhook(self.webhook_url),
            )
        return sent

    def _post(self, payload: dict[str, Any]) -> None:
        last_error: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = self.session.post(
                    self.webhook_url,
                    json=payload,
                    timeout=self.timeout,
                )
            except requests.Timeout as exc:
                last_error = exc
                time.sleep(min(2 ** attempt, 8))
                continue
            except requests.RequestException as exc:
                last_error = exc
                time.sleep(min(2 ** attempt, 8))
                continue

            if response.status_code in {200, 204}:
                return

            if response.status_code == 429:
                retry_after = _retry_after(response)
                logger.warning(
                    "Discord rate limited; sleeping %.1fs (attempt %d/%d)",
                    retry_after,
                    attempt,
                    MAX_RETRIES,
                )
                time.sleep(retry_after)
                continue

            if 500 <= response.status_code < 600:
                logger.warning(
                    "Discord HTTP %s; retrying (attempt %d/%d)",
                    response.status_code,
                    attempt,
                    MAX_RETRIES,
                )
                time.sleep(min(2 ** attempt, 8))
                continue

            # Permanent 4xx (other than 429)
            detail = _safe_response_detail(response)
            raise DiscordError(
                f"Discord webhook rejected payload with HTTP {response.status_code}"
                + (f": {detail}" if detail else "")
            )

        raise DiscordError(
            f"Discord webhook failed after retries: "
            f"{last_error.__class__.__name__ if last_error else 'unknown'}"
        )


def build_job_embed(job: Job) -> dict[str, Any]:
    fields = [
        {"name": "Company", "value": _clip(job.company or "Unknown", 256), "inline": True},
        {"name": "Role", "value": _clip(job.role or "Unknown", 256), "inline": True},
        {
            "name": "Location",
            "value": _clip(job.location or "Not specified", 1024),
            "inline": False,
        },
    ]
    if job.added:
        fields.append({"name": "Added", "value": _clip(job.added, 256), "inline": True})
    if job.sponsorship != Sponsorship.UNKNOWN:
        fields.append(
            {
                "name": "Sponsorship",
                "value": job.sponsorship.value.replace("_", " "),
                "inline": True,
            }
        )
    fields.append(
        {
            "name": "Source",
            "value": _clip(job.source_repo or "unknown", 256),
            "inline": True,
        }
    )
    if job.apply_url:
        fields.append(
            {
                "name": "Apply",
                "value": _clip(job.apply_url, 1024),
                "inline": False,
            }
        )

    embed: dict[str, Any] = {
        "title": "New Internship",
        "color": 0x2ECC71,
        "fields": fields,
    }
    if job.apply_url:
        embed["url"] = job.apply_url
    if job.source_url:
        embed["footer"] = {"text": _clip(job.source_url, 2048)}
    return embed


def redact_webhook(url: str) -> str:
    try:
        parts = urlsplit(url)
        path = parts.path.rstrip("/").split("/")
        if len(path) >= 2:
            return f"{parts.scheme}://{parts.netloc}/api/webhooks/***/***"
        return f"{parts.scheme}://{parts.netloc}/***"
    except Exception:  # noqa: BLE001
        return "***"


def _clip(value: str, limit: int) -> str:
    text = value or ""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def _retry_after(response: requests.Response) -> float:
    try:
        payload = response.json()
        if isinstance(payload, dict) and "retry_after" in payload:
            return float(payload["retry_after"])
    except ValueError:
        pass
    header = response.headers.get("Retry-After")
    if header:
        try:
            return float(header)
        except ValueError:
            pass
    return 2.0


def _safe_response_detail(response: requests.Response) -> str:
    text = (response.text or "").strip()
    if not text:
        return ""
    return _clip(text.replace("\n", " "), 300)
