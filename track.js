// Pocket Casts -> Airtable listening tracker.
//
// Runs hourly on GitHub Actions. Reads the cumulative "time listened" stat
// from the Pocket Casts private API and adds the increase since the previous
// run to today's row in Airtable. Using the cumulative stat means relistens
// are counted too.
//
// Required environment variables (set as GitHub Actions secrets):
//   PC_EMAIL        Pocket Casts account email
//   PC_PASSWORD     Pocket Casts account password
//   AIRTABLE_TOKEN  Airtable personal access token (data.records:read + :write)
//   AIRTABLE_BASE   Airtable base id (app...)
// Recommended:
//   TZ=Europe/London

const PC_BASE = "https://api.pocketcasts.com";
const AIRTABLE_BASE_URL = "https://api.airtable.com/v0";

const LISTENING_LOG = "Listening Log";
const SYNC_STATE = "Sync State";

// Safety cap: ignore any single-run jump larger than 6 hours of listening.
const MAX_DELTA_SECONDS = 6 * 60 * 60;
const SOURCE_LABEL = "Pocket Casts";

function requireEnv(name) {
  const value = process.env[name];
  if (!value) {
    throw new Error(`Missing required environment variable: ${name}`);
  }
  return value;
}

const PC_EMAIL = requireEnv("PC_EMAIL");
const PC_PASSWORD = requireEnv("PC_PASSWORD");
const AIRTABLE_TOKEN = requireEnv("AIRTABLE_TOKEN");
const AIRTABLE_BASE = requireEnv("AIRTABLE_BASE");

// ---------------------------------------------------------------------------
// Pocket Casts
// ---------------------------------------------------------------------------

// The login call is the most fragile part of this script. Try a JSON body
// with scope first, then fall back to dropping scope, then to a form-encoded
// body. The first variant that returns a token wins.
async function pocketCastsLogin() {
  const attempts = [
    {
      label: "json+scope",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email: PC_EMAIL, password: PC_PASSWORD, scope: "webplayer" }),
    },
    {
      label: "json-no-scope",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email: PC_EMAIL, password: PC_PASSWORD }),
    },
    {
      label: "form+scope",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ email: PC_EMAIL, password: PC_PASSWORD, scope: "webplayer" }).toString(),
    },
  ];

  let lastError;
  for (const attempt of attempts) {
    const res = await fetch(`${PC_BASE}/user/login`, {
      method: "POST",
      headers: attempt.headers,
      body: attempt.body,
    });

    if (res.ok) {
      const data = await res.json();
      if (data && data.token) {
        return data.token;
      }
      lastError = new Error(`Login (${attempt.label}) succeeded but no token in response`);
      continue;
    }

    const text = await res.text().catch(() => "");
    lastError = new Error(`Login (${attempt.label}) failed: HTTP ${res.status} ${text}`);
    // Only keep trying other variants on auth-style failures.
    if (res.status !== 401 && res.status !== 400) {
      break;
    }
  }

  throw lastError || new Error("Pocket Casts login failed");
}

async function pocketCastsTotalSeconds(token) {
  const res = await fetch(`${PC_BASE}/user/stats/summary`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
    },
    body: "{}",
  });

  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`stats/summary failed: HTTP ${res.status} ${text}`);
  }

  const data = await res.json();
  // timeListened is a string holding total seconds listened (cumulative).
  const total = Number(data.timeListened);
  if (!Number.isFinite(total)) {
    throw new Error(`Unexpected timeListened value: ${JSON.stringify(data.timeListened)}`);
  }
  return total;
}

// ---------------------------------------------------------------------------
// Airtable
// ---------------------------------------------------------------------------

function airtableUrl(table, query) {
  const url = new URL(`${AIRTABLE_BASE_URL}/${AIRTABLE_BASE}/${encodeURIComponent(table)}`);
  if (query) {
    for (const [key, value] of Object.entries(query)) {
      url.searchParams.set(key, value);
    }
  }
  return url.toString();
}

async function airtableFetch(url, options = {}) {
  const res = await fetch(url, {
    ...options,
    headers: {
      Authorization: `Bearer ${AIRTABLE_TOKEN}`,
      "Content-Type": "application/json",
      ...(options.headers || {}),
    },
  });

  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`Airtable request failed: ${options.method || "GET"} ${url} -> HTTP ${res.status} ${text}`);
  }
  return res.json();
}

async function getFirstRecord(table) {
  const data = await airtableFetch(airtableUrl(table, { maxRecords: "1", pageSize: "1" }));
  return data.records && data.records.length ? data.records[0] : null;
}

// --- Sync State -------------------------------------------------------------

async function readLastTotal() {
  const record = await getFirstRecord(SYNC_STATE);
  if (!record) return { record: null, total: null };

  const raw = record.fields && record.fields.snapshot;
  if (!raw) return { record, total: null };

  try {
    const parsed = JSON.parse(raw);
    const total = Number(parsed.total);
    return { record, total: Number.isFinite(total) ? total : null };
  } catch {
    return { record, total: null };
  }
}

async function writeLastTotal(existingRecord, total) {
  const fields = {
    snapshot: JSON.stringify({ total }),
    updatedAt: new Date().toISOString(),
  };

  if (existingRecord) {
    await airtableFetch(airtableUrl(SYNC_STATE), {
      method: "PATCH",
      body: JSON.stringify({ records: [{ id: existingRecord.id, fields }] }),
    });
  } else {
    await airtableFetch(airtableUrl(SYNC_STATE), {
      method: "POST",
      body: JSON.stringify({ records: [{ fields }] }),
    });
  }
}

// --- Listening Log ----------------------------------------------------------

// Today's date as YYYY-MM-DD in Europe/London, independent of process TZ.
function londonDate() {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Europe/London",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date());
  return parts; // en-CA formats as YYYY-MM-DD
}

async function getTodayRow(dateStr) {
  const formula = `DATESTR({Date})='${dateStr}'`;
  const data = await airtableFetch(
    airtableUrl(LISTENING_LOG, { filterByFormula: formula, maxRecords: "1" })
  );
  return data.records && data.records.length ? data.records[0] : null;
}

async function addSeconds(dateStr, deltaSeconds) {
  const existing = await getTodayRow(dateStr);
  const prevSeconds = existing && Number(existing.fields.Seconds) ? Number(existing.fields.Seconds) : 0;
  const newSeconds = prevSeconds + deltaSeconds;
  const newMinutes = Math.round(newSeconds / 60);

  const fields = {
    Date: dateStr,
    Seconds: newSeconds,
    Minutes: newMinutes,
    Source: SOURCE_LABEL,
  };

  if (existing) {
    await airtableFetch(airtableUrl(LISTENING_LOG), {
      method: "PATCH",
      body: JSON.stringify({ records: [{ id: existing.id, fields }] }),
    });
  } else {
    await airtableFetch(airtableUrl(LISTENING_LOG), {
      method: "POST",
      body: JSON.stringify({ records: [{ fields }] }),
    });
  }

  return { prevSeconds, newSeconds, newMinutes };
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

async function main() {
  const token = await pocketCastsLogin();
  const currentTotal = await pocketCastsTotalSeconds(token);
  console.log(`Pocket Casts cumulative time listened: ${currentTotal}s`);

  const { record: stateRecord, total: lastTotal } = await readLastTotal();

  if (lastTotal === null) {
    // First run (or unreadable state): seed the baseline and log nothing.
    await writeLastTotal(stateRecord, currentTotal);
    console.log(`First run: seeded baseline at ${currentTotal}s. No minutes logged.`);
    return;
  }

  let delta = currentTotal - lastTotal;
  if (delta < 0) {
    console.log(`Counter went backwards (${lastTotal} -> ${currentTotal}); treating delta as 0.`);
    delta = 0;
  }
  if (delta > MAX_DELTA_SECONDS) {
    console.log(`Delta ${delta}s exceeds cap; clamping to ${MAX_DELTA_SECONDS}s.`);
    delta = MAX_DELTA_SECONDS;
  }

  if (delta > 0) {
    const dateStr = londonDate();
    const { newSeconds, newMinutes } = await addSeconds(dateStr, delta);
    console.log(`Added ${delta}s to ${dateStr}. Day total now ${newSeconds}s (${newMinutes} min).`);
  } else {
    console.log("No new listening since last run; nothing to log.");
  }

  // Always advance the stored cumulative total.
  await writeLastTotal(stateRecord, currentTotal);
  console.log(`Stored new cumulative total: ${currentTotal}s.`);
}

main().catch((err) => {
  console.error(err.message || err);
  process.exit(1);
});
