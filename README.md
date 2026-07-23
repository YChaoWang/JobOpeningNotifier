# JobOpeningNotifier

**Goal:** let anyone add the job/internship GitHub lists they care about, combine many sources automatically, and get Discord alerts — **without changing this upstream project or the source listing repos**.

How that works:

1. **Fork** this repo (your copy is independent).  
2. In **your** fork, edit `config.yaml` to add/remove sources.  
3. Add **your** Discord webhook as a secret.  
4. GitHub Actions in **your** fork watches those READMEs and notifies **your** Discord.  

Your `config.yaml`, `data/` state, and webhook never write back to the original JobOpeningNotifier repo, and this bot only *reads* public listing READMEs (it does not modify SimplifyJobs, sndsh404, etc.).

**Starter sources** (change freely in your fork’s `config.yaml`):

- [SimplifyJobs/Summer2026-Internships](https://github.com/SimplifyJobs/Summer2026-Internships) (HTML tables)
- [sndsh404/summer-2027-internships](https://github.com/sndsh404/summer-2027-internships) (Markdown tables)

## Architecture

```
config.yaml
  → fetch README + SHA
  → skip unchanged repos (pending queue still drains)
  → Markdown / HTML parsers (GitHub Models AI fallback if needed)
  → normalize + filter + dedupe by application URL
  → Discord notify (batched, retried)
  → atomic state under data/
```

## Quick start

1. **Fork** this repository (do not push personal config/state to upstream)  
2. Create a Discord webhook (channel → Integrations → Webhooks)  
3. In *your fork*: Settings → Secrets → Actions → add `DISCORD_WEBHOOK_URL`  
4. Edit *your* `config.yaml` — add any public job-list repos you want  
5. Enable Actions → run **Internship Monitor** once  

**First run is silent by default:** existing jobs are stored as seen, Discord is not flooded. Later README changes notify only new matches.

To send the **current matching set** (first run or later, even after a silent baseline):

```bash
uv run python check_jobs.py --notify-existing
```

Or set the Actions input `notify_existing` to `true`. Batch limits still apply; overflow goes to `pending_jobs.json`.

## Behavior notes

| Topic | Behavior |
| --- | --- |
| Pending queue | Jobs beyond `max_jobs_per_run` go to `data/pending_jobs.json` and are sent on later runs |
| Partial Discord failure | Successfully accepted jobs are checkpointed; the rest stay pending (no duplicates) |
| Dedup | Same normalized apply URL = same job across sources |
| Fallback ID | Without a URL: company + role + location + source repo |
| Filters | Case-insensitive on company/role/location; closed never notified; keyword rejects are not permanently forgotten |
| SHA skip | Unchanged README skips parse; pending notifications still process |
| Public forks | `DISCORD_WEBHOOK_URL` stays encrypted as an Actions secret; committed `data/*.json` is visible on public forks |

## Local commands

```bash
uv sync --group dev
uv run pytest -q

export DISCORD_WEBHOOK_URL='https://discord.com/api/webhooks/...'
uv run python check_jobs.py --test-discord
uv run python check_jobs.py --dry-run
uv run python check_jobs.py --notify-existing
uv run python check_jobs.py
```

## Configuration

All normal customization is in **`config.yaml`**.  
Start from the commented template: **`config.example.yaml`**.

| What you want | Where |
| --- | --- |
| Add/remove internship lists | `repositories` in `config.yaml` |
| Role keywords / locations / sponsorship filters | `filters` |
| AI fallback on/off and model | `ai` |
| How many Discord posts per run | `notifications` |
| Discord webhook | GitHub secret / env `DISCORD_WEBHOOK_URL` (never in YAML) |
| Preview without posting | CLI: `uv run python check_jobs.py --dry-run` |
| Send / resend current matches | CLI/Actions: `--notify-existing` (works even after silent baseline) |

```bash
# After forking, optionally reset from the example:
cp config.example.yaml config.yaml
# then edit config.yaml
```

Example repository entry:

```yaml
repositories:
  - name: my-list
    repo: owner/repository
    branch: main
    readme_path: README.md
    enabled: true
```
## GitHub Actions

- Schedule: every 30 minutes + `workflow_dispatch`
- Inputs: `test_discord`, `notify_existing`
- Permissions: `contents: write`, `models: read`
- Secret: `DISCORD_WEBHOOK_URL`
- Commits state with `chore: update internship monitor state` only when files change

## Security

- Never commit webhook URLs or tokens  
- Logs redact webhook URLs  
- Arbitrary README formats and AI extraction are best-effort  

## License

[MIT](LICENSE)
