# JobOpeningNotifier

[![Internship Monitor](https://github.com/YChaoWang/JobOpeningNotifier/actions/workflows/monitor.yml/badge.svg)](https://github.com/YChaoWang/JobOpeningNotifier/actions/workflows/monitor.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![GitHub Clones](https://img.shields.io/badge/dynamic/json?color=success&label=Clone&query=count&url=https://gist.githubusercontent.com/YChaoWang/8c2454cf079a01a467efa9974926d12e/raw/clone.json&logo=github)](https://github.com/MShawon/github-clone-count-badge)
![Built for Discord](https://img.shields.io/badge/Discord-Webhook-%235865F2.svg?style=for-the-badge&logo=discord&logoColor=white)

Watch public internship/job lists on GitHub and post new matches to **your** Discord. It only reads listing READMEs — nothing is written to this repo or the source lists.

**Sources:** [SimplifyJobs/Summer2027-Internships](https://github.com/SimplifyJobs/Summer2027-Internships) · [sndsh404/summer-2027-internships](https://github.com/sndsh404/summer-2027-internships)

## Setup

1. Fork this repo
2. Discord → channel → Integrations → Webhooks → copy URL
3. Fork → **Settings → Secrets → Actions** → `DISCORD_WEBHOOK_URL`
4. **Actions** → enable workflows → run **Internship Monitor**

First run is **silent** (baselines existing jobs). Later runs notify only **new** matches. To post the current list once, run with `notify_existing = true`.

### Customize

Optional — edit [`config.yaml`](config.yaml) if you want changes. Full comments: [`config.example.yaml`](config.example.yaml).

- **Sources** — add/remove repos under `repositories`
- **Keywords** — `include_keywords` / `exclude_keywords` (empty include = all roles)
- **Locations** — limit to places you care about
- **Batch size** — `max_jobs_per_run` (extras wait for the next run)
- **AI fallback** — `ai.enabled` (used only if table parsing fails)

## How it works

```text
config → fetch READMEs → parse tables → filter → skip seen → Discord
```

Bot memory lives in local `data/` or, on Actions, the **`monitor-state`** branch (not `main`). Each fork has its own.

| Goal | Action |
| --- | --- |
| Repost current matches | Run workflow with `notify_existing = true` |
| Full reset | Delete the `monitor-state` branch, then run again |

## Local

Needs [uv](https://docs.astral.sh/uv/) and Python 3.11+.

```bash
uv sync --group dev
export DISCORD_WEBHOOK_URL='https://discord.com/api/webhooks/...'
uv run python check_jobs.py --dry-run
uv run pytest -q
```

## License

[MIT](LICENSE) · [Contributing](CONTRIBUTING.md)
