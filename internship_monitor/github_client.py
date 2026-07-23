"""GitHub README fetching with SHA metadata and error handling."""

from __future__ import annotations

import base64
import logging
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import requests

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
DEFAULT_TIMEOUT = 30
MAX_RETRIES = 3


class GitHubClientError(Exception):
    """Raised for non-retryable GitHub API failures."""


@dataclass(slots=True)
class ReadmeDocument:
    content: str
    sha: str
    path: str
    html_url: str | None = None


class GitHubClient:
    def __init__(
        self,
        token: str | None = None,
        *,
        session: requests.Session | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.token = token
        self.session = session or requests.Session()
        self.timeout = timeout

    def fetch_readme(
        self,
        owner: str,
        repo: str,
        *,
        path: str = "README.md",
        branch: str | None = None,
    ) -> ReadmeDocument:
        api_path = quote(path.lstrip("/"))
        url = f"{GITHUB_API}/repos/{owner}/{repo}/contents/{api_path}"
        params: dict[str, str] = {}
        if branch:
            params["ref"] = branch

        payload = self._request_json("GET", url, params=params)
        if not isinstance(payload, dict):
            raise GitHubClientError("Unexpected GitHub contents response type")

        encoding = payload.get("encoding")
        content_b64 = payload.get("content")
        sha = payload.get("sha")
        if not sha or not isinstance(sha, str):
            raise GitHubClientError("GitHub contents response missing SHA")

        if encoding == "base64" and isinstance(content_b64, str):
            try:
                content = base64.b64decode(content_b64).decode("utf-8")
            except (ValueError, UnicodeDecodeError) as exc:
                raise GitHubClientError("Failed to decode README content") from exc
        elif isinstance(payload.get("download_url"), str):
            content = self._request_text(payload["download_url"])
        else:
            raise GitHubClientError("README content unavailable from GitHub API")

        return ReadmeDocument(
            content=content,
            sha=sha,
            path=str(payload.get("path") or path),
            html_url=payload.get("html_url"),
        )

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "JobOpeningNotifier/2.0",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _request_json(
        self, method: str, url: str, *, params: dict[str, str] | None = None
    ) -> Any:
        response = self._request(method, url, params=params)
        try:
            return response.json()
        except ValueError as exc:
            raise GitHubClientError("GitHub returned invalid JSON") from exc

    def _request_text(self, url: str) -> str:
        response = self._request("GET", url)
        return response.text

    def _request(
        self, method: str, url: str, *, params: dict[str, str] | None = None
    ) -> requests.Response:
        last_error: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = self.session.request(
                    method,
                    url,
                    headers=self._headers(),
                    params=params,
                    timeout=self.timeout,
                )
            except requests.Timeout as exc:
                last_error = exc
                logger.warning("GitHub request timeout (attempt %d/%d)", attempt, MAX_RETRIES)
                time.sleep(min(2 ** attempt, 8))
                continue
            except requests.RequestException as exc:
                last_error = exc
                logger.warning(
                    "GitHub request error %s (attempt %d/%d)",
                    exc.__class__.__name__,
                    attempt,
                    MAX_RETRIES,
                )
                time.sleep(min(2 ** attempt, 8))
                continue

            if response.status_code == 404:
                raise GitHubClientError(
                    "README or repository not found (check repo name, branch, and path)"
                )
            if response.status_code == 403 and "rate limit" in response.text.lower():
                retry_after = _retry_after_seconds(response)
                logger.warning(
                    "GitHub rate limited; sleeping %.1fs (attempt %d/%d)",
                    retry_after,
                    attempt,
                    MAX_RETRIES,
                )
                time.sleep(retry_after)
                continue
            if response.status_code in {429, 500, 502, 503, 504}:
                retry_after = _retry_after_seconds(response)
                logger.warning(
                    "GitHub HTTP %s; retrying in %.1fs (attempt %d/%d)",
                    response.status_code,
                    retry_after,
                    attempt,
                    MAX_RETRIES,
                )
                time.sleep(retry_after)
                continue
            if response.status_code >= 400:
                raise GitHubClientError(
                    f"GitHub HTTP {response.status_code} for contents request"
                )
            return response

        raise GitHubClientError(
            f"GitHub request failed after retries: {last_error.__class__.__name__ if last_error else 'unknown'}"
        )


def _retry_after_seconds(response: requests.Response) -> float:
    header = response.headers.get("Retry-After")
    if header:
        try:
            return float(header)
        except ValueError:
            pass
    reset = response.headers.get("X-RateLimit-Reset")
    if reset:
        try:
            delay = int(reset) - int(time.time())
            return float(max(1, min(delay, 60)))
        except ValueError:
            pass
    return 2.0
