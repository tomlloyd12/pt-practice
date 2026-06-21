# Pocket Casts → Airtable listening tracker

Automatically logs how many minutes of podcast you listen to each day and
tracks it against a target of **45 minutes/day**. No timer to tap — a GitHub
Action runs every 2 hours, reads your cumulative "time listened" from the Pocket
Casts private API, and adds the increase since the last run to today's Airtable
row.

Because it uses the cumulative lifetime stat, relistens are counted too.

## How it works

Each run (`node track.js`):

1. Logs in to Pocket Casts (`POST /user/login`) and reads the cumulative total
   seconds (`POST /user/stats/summary` → `timeListened`).
2. Reads the last stored cumulative total from the **Sync State** table.
   - **First run:** stores the current total as the baseline and logs nothing.
3. `delta = current − last`. Negative deltas (counter reset) are treated as `0`;
   deltas are capped at **6 hours** as a safety limit.
4. If `delta > 0`, adds it to today's row in **Listening Log** (today = date in
   `Europe/London`), keeping a precise `Seconds` total and `Minutes =
   round(Seconds / 60)`.
5. Stores the new cumulative total back to **Sync State**.

## Airtable schema

Base: **Portuguese Listening**

**Listening Log**
| Field   | Type                                      |
|---------|-------------------------------------------|
| Date    | Date (European format, D/M/YYYY) — primary |
| Minutes | Number (0 dp)                             |
| Seconds | Number (0 dp)                             |
| Source  | Single select — option `Pocket Casts`     |

**Sync State**
| Field     | Type                  |
|-----------|-----------------------|
| snapshot  | Long text — primary (last cumulative total as JSON) |
| updatedAt | Single line text      |

## Setup

1. **Create the Airtable base** with the two tables above and copy the base ID
   (the `app...` part of the base URL).
2. **Create an Airtable personal access token** with `data.records:read` and
   `data.records:write` scoped to this base.
3. **Add the four GitHub repository secrets** (Settings → Secrets and variables
   → Actions → New repository secret):
   - `PC_EMAIL` — Pocket Casts account email
   - `PC_PASSWORD` — Pocket Casts account password
   - `AIRTABLE_TOKEN` — the personal access token from step 2
   - `AIRTABLE_BASE` — the `app...` base ID from step 1
4. **Run the workflow once** (Actions → *Pocket Casts listening tracker* → Run
   workflow). The first run only seeds the baseline and logs nothing; logging
   starts from the second run onward.

After that it runs automatically every 2 hours. `workflow_dispatch` lets you
trigger it manually any time. The workflow sets `TZ=Europe/London`.

The 2-hour cadence (rather than hourly) and the skip-write-when-idle behaviour
keep the script well within Airtable free-tier monthly API-call limits.

## Local test

```bash
PC_EMAIL=... PC_PASSWORD=... AIRTABLE_TOKEN=... AIRTABLE_BASE=app... \
  TZ=Europe/London node track.js
```

Requires Node 18+ (uses the built-in global `fetch`; no dependencies to install).

## Notes

- The login call is the most fragile part. `track.js` tries a JSON body with
  `scope: "webplayer"` first, then a JSON body without scope, then a
  form-encoded body — the first that returns a token wins.
- Secrets are never printed or committed.
