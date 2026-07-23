# JobOpeningNotifier

Open-source bot that watches public internship/job README lists on GitHub and posts **matching roles** to your Discord.

**Default sources** (edit in `config.yaml`):

- [SimplifyJobs/Summer2026-Internships](https://github.com/SimplifyJobs/Summer2026-Internships)
- [sndsh404/summer-2027-internships](https://github.com/sndsh404/summer-2027-internships)

## Use it (fork model)

Everyone runs their **own fork**. Your Discord webhook and `data/` state stay private to that fork — they are not shared with other users.

1. **Fork** this repository  
2. Create a Discord webhook (channel → **Integrations** → **Webhooks**)  
3. In *your fork*: **Settings → Secrets and variables → Actions** → add `DISCORD_WEBHOOK_URL`  
4. Enable Actions, then run **Internship Monitor** once (`workflow_dispatch`)  

On first run, matching jobs are sent to Discord. Later runs only send **new** ones. State is saved in your fork under `data/` and committed by Actions.

Upstream `data/*.json` files stay empty on purpose. Do not open PRs that include your personal `seen_jobs.json` / webhook secrets.

## How state works

| File | Role |
| --- | --- |
| `data/seen_jobs.json` | Jobs already handled (skip next time) |
| `data/repository_state.json` | Last README SHA per source repo |
| `data/pending_jobs.json` | Matches waiting when over the per-run limit |

Each run that sees a README change compares jobs to `seen_jobs.json`, notifies new matches, then updates these files **in your fork only**.

## Local development

Uses [uv](https://docs.astral.sh/uv/).

```bash
uv sync --group dev
uv run pytest -q

export DISCORD_WEBHOOK_URL='https://discord.com/api/webhooks/...'
uv run python check_jobs.py --test-discord
uv run python check_jobs.py
```

Optional: `GITHUB_TOKEN` for higher API limits and GitHub Models fallback.

## Config

```yaml
repositories:
  - name: my-list
    repo: owner/repository
    branch: main
    readme_path: README.md
    enabled: true
```

Also tune `filters`, `ai`, and `notifications` in `config.yaml`.

## Notes

- First run notifies all matching jobs; use `--baseline-only` for a silent seed  
- Never commit webhook URLs or tokens  
- License: [MIT](LICENSE)  
