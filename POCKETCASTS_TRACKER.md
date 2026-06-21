# Pocket Casts → Airtable listening tracker

Automatically logs how many minutes of podcast you listen to each day and
tracks it against a target of **45 minutes/day**. No timer to tap — a GitHub
Action runs hourly, reads your cumulative "time listened" from the Pocket
Casts private API, and adds the increase since the previous run to today's
Airtable row.

Because it uses the cumulative stat (not a per-episode count), relistens are
counted too.

## Files

- `track.js` — the tracker (Node 18+, uses built-in `fetch`, no npm installs).
- `.github/workflows/track.yml` — hourly schedule + manual `workflow_dispatch`.

## How it works each run

1. Log in to Pocket Casts (`POST /user/login`) and read the cumulative
   `timeListened` (`POST /user/stats/summary`).
2. Read the last stored cumulative total from the **Sync State** table.
   - If there's none, this is the **first run**: store the current total and
     log nothing (seed the baseline).
3. `delta = current − last`. If negative (counter reset) treat as `0`. Cap at
   6 hours as a safety limit.
4. If `delta > 0`, add it to today's row (date in **Europe/London**), keeping a
   precise `Seconds` running total and `Minutes = round(Seconds / 60)`.
5. Store the new cumulative total back to Sync State.

## Airtable schema (base "Portuguese Listening")

**Listening Log**
| Field   | Type                                   |
|---------|----------------------------------------|
| Date    | Date (European format, D/M/YYYY)       |
| Minutes | Number (0 dp)                          |
| Seconds | Number (0 dp)                          |
| Source  | Single select — option "Pocket Casts"  |

**Sync State**
| Field     | Type                                          |
|-----------|-----------------------------------------------|
| snapshot  | Long text (last cumulative total as JSON)     |
| updatedAt | Single line text (ISO timestamp)              |

## Configuration (GitHub Actions secrets)

| Secret           | Value                                                              |
|------------------|-------------------------------------------------------------------|
| `PC_EMAIL`       | Pocket Casts account email                                        |
| `PC_PASSWORD`    | Pocket Casts account password                                     |
| `AIRTABLE_TOKEN` | Airtable PAT with `data.records:read` + `data.records:write`      |
| `AIRTABLE_BASE`  | Airtable base id (`app...`)                                       |

`TZ=Europe/London` is set in the workflow.

### Setting the secrets

In the GitHub repo: **Settings → Secrets and variables → Actions → New
repository secret**, add each of the four above.

Or with the GitHub CLI (you'll be prompted for each value — nothing is echoed):

```sh
gh secret set PC_EMAIL
gh secret set PC_PASSWORD
gh secret set AIRTABLE_TOKEN
gh secret set AIRTABLE_BASE
```

## Running it

- Hourly automatically (cron). GitHub may delay scheduled runs under load.
- Manually: **Actions → "Pocket Casts listening tracker" → Run workflow**, or
  `gh workflow run track.yml`.

> **The first run seeds the baseline and logs nothing.** It records your
> current cumulative total so later runs can measure the increase. Minutes
> start accruing from the second run onward.

## Local test (optional)

```sh
PC_EMAIL=... PC_PASSWORD=... AIRTABLE_TOKEN=... AIRTABLE_BASE=app... \
  TZ=Europe/London node track.js
```
