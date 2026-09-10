# JobOpeningNotifier

Watch public internship/job lists on GitHub and post new matches to **your** Discord.

It only **reads** listing READMEs. Your fork keeps its own config and webhook — nothing is written back to this repo or to the source lists.

**Default sources**

- [SimplifyJobs/Summer2026-Internships](https://github.com/SimplifyJobs/Summer2026-Internships)
- [sndsh404/summer-2027-internships](https://github.com/sndsh404/summer-2027-internships)

## Setup (fork)

1. **Fork** this repository
2. Create a Discord webhook (channel → Integrations → Webhooks) and copy the URL
3. In your fork: **Settings → Secrets and variables → Actions** → add `DISCORD_WEBHOOK_URL`
4. Edit `config.yaml` (repos, keywords, locations — see [`config.example.yaml`](config.example.yaml))
5. Open **Actions**, enable workflows if asked, then run **Internship Monitor**

The first run is **silent** (it remembers what’s already listed so you don’t get flooded). Later runs only notify **new** matches.

To notify everything currently matching, run the workflow with `notify_existing = true`.

## Config

| What | Where |
| --- | --- |
| Job lists | `config.yaml` → `repositories` |
| Keywords / locations | `config.yaml` → `filters` |
| Discord webhook | Actions secret `DISCORD_WEBHOOK_URL` |

Never commit webhook URLs.

## Local use

Needs [uv](https://docs.astral.sh/uv/) and Python 3.11+.

```bash
uv sync --group dev
export DISCORD_WEBHOOK_URL='https://discord.com/api/webhooks/...'

uv run python check_jobs.py --test-discord   # connectivity check
uv run python check_jobs.py --dry-run        # preview, no Discord
uv run python check_jobs.py --notify-existing
uv run python check_jobs.py
```

```bash
uv run pytest -q
```

## Notes

- Runs every **30 minutes** on Actions (or manually)
- State lives in `data/` locally and in the Actions **cache** — not in git
- If the cache is cleared, the next run baselines again (silent)
- Closed roles are never notified

## License

[MIT](LICENSE) · [Contributing](CONTRIBUTING.md)
