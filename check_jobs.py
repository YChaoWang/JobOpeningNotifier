#!/usr/bin/env python3
"""CLI entrypoint for the multi-repository internship monitor."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from internship_monitor.config import ConfigError, load_config
from internship_monitor.discord import DiscordNotifier, redact_webhook
from internship_monitor.github_client import GitHubClient
from internship_monitor.pipeline import MonitorPipeline
from internship_monitor.state import StateStore

DEFAULT_CONFIG = Path("config.yaml")
DEFAULT_DATA_DIR = Path("data")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Monitor GitHub internship/job README repositories and notify Discord."
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG),
        help="Path to YAML/JSON config (default: config.yaml)",
    )
    parser.add_argument(
        "--data-dir",
        default=str(DEFAULT_DATA_DIR),
        help="Directory for state JSON files (default: data)",
    )
    parser.add_argument(
        "--test-discord",
        action="store_true",
        help="Send one clearly labeled Discord test message and exit",
    )
    parser.add_argument(
        "--baseline-only",
        action="store_true",
        help=(
            "On first run only: store existing jobs as seen without Discord notifications. "
            "Default first-run behavior notifies all matching jobs."
        ),
    )
    parser.add_argument(
        "--notify-existing",
        action="store_true",
        help=argparse.SUPPRESS,  # deprecated alias; first run notifies by default
    )
    parser.add_argument(
        "--log-level",
        default=os.getenv("LOG_LEVEL", "INFO"),
        help="Logging level (default: INFO)",
    )
    return parser


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Reduce noisy third-party logs.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(args.log_level)
    logger = logging.getLogger("check_jobs")

    webhook = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    github_token = os.getenv("GITHUB_TOKEN", "").strip() or None

    if args.test_discord:
        try:
            notifier = DiscordNotifier(webhook)
        except ValueError as exc:
            logger.error("%s", exc)
            return 2
        try:
            notifier.send_test_message()
        except Exception as exc:  # noqa: BLE001 - surface Discord errors to CLI
            logger.error("Discord test failed: %s", exc)
            return 1
        logger.info(
            "Discord test completed via %s",
            redact_webhook(notifier.webhook_url),
        )
        return 0

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        logger.error("%s", exc)
        return 2

    state = StateStore(args.data_dir)
    state.load()

    discord = None
    if webhook:
        discord = DiscordNotifier(
            webhook,
            max_jobs_per_message=config.notifications.max_jobs_per_message,
        )
    else:
        logger.warning(
            "DISCORD_WEBHOOK_URL not set; matching jobs will remain pending/unnotified"
        )

    github = GitHubClient(token=github_token)
    pipeline = MonitorPipeline(
        config,
        state,
        github,
        discord,
        baseline_only=args.baseline_only,
    )
    summary = pipeline.run()

    # Non-zero if every enabled repository failed.
    enabled = [repo for repo in config.repositories if repo.enabled]
    if enabled and summary.repositories_failed == len(enabled):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
