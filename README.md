# JobOpeningNotifier

Monitor public GitHub repositories that list internships or jobs in their README files, parse new listings with deterministic rule-based parsers (and GitHub Models as a fallback), and send Discord notifications for newly matching roles.

## Project overview

This project watches one or more configured repositories on a schedule (GitHub Actions every 30 minutes), detects README changes via content SHA, parses job tables into a normalized schema, filters by keywords/location/sponsorship rules, and posts only **new** matching jobs to Discord.

Initial repositories:

- [SimplifyJobs/Summer2026-Internships](https://github.com/SimplifyJobs/Summer2026-Internships) (HTML tables, `dev` branch)
- [sndsh404/summer-2027-internships](https://github.com/sndsh404/summer-2027-internships) (Markdown tables, `main` branch)

## Architecture

```
Repository configuration (config.yaml)
  → Fetch README + SHA (GitHub API)
  → Compare SHA to persisted state
  → Skip unchanged repositories
  → Try Markdown table parser
  → Try HTML table parser
  → Validate confidence
  → GitHub Models AI fallback (only when needed)
  → Validate + normalize jobs
  → Filter jobs
  → Deduplicate by normalized apply URL / identity
  → Discord webhook notifications
  → Persist repository state, seen IDs, pending queue
```

Modules live under `internship_monitor/`. The CLI entrypoint is `check_jobs.py`.

## Supported README formats

**Rule-based (preferred):**

1. **Markdown tables** — common `| Company | Role | Location | Apply | Added |` layouts, including alternate header names, Markdown/HTML links, `<br>` locations, emojis/flags, escaped pipes, closed markers, and repeated-company rows (`↳`, `^`, or empty company cell).
2. **HTML tables** — GitHub-flavored README `<table>` blocks (as used by SimplifyJobs), including nested anchors/images, `<br>` locations, relative links, and repeated-company indicators.

**AI fallback:** GitHub Models (`openai` SDK against `https://models.github.ai/inference`) when rule parsers cannot reliably extract listings.

Arbitrary README formats are **best-effort**. AI extraction is helpful but **does not guarantee** perfect results. Invalid AI JSON or incomplete records are rejected.

## Why rule parsing before AI

- Deterministic, free, and fast for the two dominant listing formats
- Avoids spending GitHub Models quota on every unchanged or well-structured README
- Produces stable field mapping and sponsorship/closed detection from known markers (`🛂`, `🇺🇸`, `🔒`)

AI runs only when:

- no supported table is detected (but the README still looks like a job list)
- required columns cannot be mapped
- rule-parser confidence is below `ai.minimum_rule_parser_confidence`
- parsing yields zero usable jobs from a README that appears to contain listings

## GitHub Models fallback

- Provider: GitHub Models OpenAI-compatible API
- Auth: `GITHUB_TOKEN` environment variable (never stored in config or logs)
- Default model: `openai/gpt-4.1-mini` (configurable)
- Temperature: `0`
- Input is trimmed (badges/contribution/license sections removed when safe) and capped by `ai.max_input_characters`

### Free-tier and rate-limit caveats

GitHub Models availability, model IDs, and free-tier limits can change. Rate limits, quota exhaustion, or model deprecation may cause AI fallback to fail. When that happens the monitor logs a warning, does **not** invent jobs, and avoids permanently skipping a README SHA if parsing was required and produced nothing usable.

## How to add another repository

Edit `config.yaml`:

```yaml
repositories:
  - name: my-unique-name
    repo: owner/repository
    branch: main
    readme_path: README.md
    enabled: true
```

No application code changes are required for additional repos that use supported Markdown/HTML table formats.

## Discord setup

### Create a webhook

1. Open your Discord server → channel settings → **Integrations** → **Webhooks**
2. Create a webhook and copy the URL

### Configure the secret

In the GitHub repository: **Settings → Secrets and variables → Actions**

Create:

- `DISCORD_WEBHOOK_URL` — full webhook URL

Locally you can export:

```bash
export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
export GITHUB_TOKEN="ghp_..."   # optional locally; useful for higher API limits + Models
```

Never commit webhook URLs or tokens.

## GitHub Actions permissions

The workflow requests:

```yaml
permissions:
  contents: write   # commit updated state JSON
  models: read      # GitHub Models inference
```

Also provide repository secret `DISCORD_WEBHOOK_URL`. `GITHUB_TOKEN` is provided automatically by Actions.

The workflow:

- runs every 30 minutes and via `workflow_dispatch`
- supports inputs `test_discord` and `notify_existing`
- installs dependencies, runs tests, then monitors
- commits state only when `data/*.json` files changed, with message `chore: update internship monitor state`
- does **not** listen to `push`, so state commits cannot loop the monitor

## First-run behavior

On the first successful processing of each repository:

- all current jobs are stored as seen
- **no** Discord notifications are sent for that baseline
- only jobs added in later README updates are notified

Initialization is tracked **per repository**. Adding a new repo does not reset others.

## Manual test instructions

```bash
# Discord connectivity only (no parsing)
uv run python check_jobs.py --test-discord

# Force notify existing matching jobs (testing only; not default)
uv run python check_jobs.py --notify-existing
```

## Local development commands

This project uses [uv](https://docs.astral.sh/uv/) for the Python environment and lockfile.

```bash
# Install uv: https://docs.astral.sh/uv/getting-started/installation/
uv sync --group dev

# Run tests
uv run pytest -q

# Syntax check
uv run python -m compileall check_jobs.py internship_monitor

# Dry monitoring run (needs network + optional secrets)
uv run python check_jobs.py --log-level DEBUG
```

`uv sync` creates `.venv/` from `pyproject.toml` + `uv.lock`. Prefer `uv run ...` so the managed environment is used automatically.

`requirements.txt` is generated via `uv export` for compatibility; edit dependencies in `pyproject.toml`, then re-lock with `uv lock` / `uv sync`.

## Example Discord notification

Embed title: **New Internship**

| Field | Example |
| --- | --- |
| Company | Google |
| Role | Software Engineering Intern |
| Location | Mountain View, CA |
| Added | 2026-07-20 |
| Sponsorship | unavailable (only when known) |
| Source | sndsh404/summer-2027-internships |
| Apply | `https://...` |

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| No Discord messages on first Actions run | Expected — baseline seeding |
| `README or repository not found` | `repo`, `branch`, and `readme_path` in `config.yaml` |
| AI warnings about missing token | Ensure Actions has `models: read` and `GITHUB_TOKEN` is available |
| Jobs not matching | Adjust `filters.include_keywords` / `exclude_keywords` |
| Too many messages truncated | Overflow goes to `data/pending_jobs.json` for the next run |
| State not committing | Workflow only commits when JSON files changed |

## Security notes

- Never log `GITHUB_TOKEN`, full Discord webhook URLs, or sensitive headers
- Do not put secrets in `config.yaml` or source control
- Webhook URLs in logs are redacted
- AI payloads are not logged unless you add an explicit local debug mode

## Assumptions and limitations

- Optimized for Markdown/HTML internship tables similar to the two seed repositories
- Cross-repo deduplication uses normalized application URLs when present
- Sponsorship filters only exclude when the README explicitly marks restrictions; `unknown` is not treated as unavailable
- Closed roles are never notified
- GitHub Models quality and quotas are outside this project's control
