#!/usr/bin/env python3
"""
PT Practice App — European Portuguese Translation & Correction
==============================================================
Function 1: Translate English → European Portuguese, log to Google Sheets.
Function 2: Check a Portuguese sentence, log correct version + EN translation.

Usage:
    python pt_practice_app.py

Then open http://localhost:5001/ on your phone (same Wi-Fi network).
Or access via http://<your-mac-ip>:5001/ on your phone.
"""

import json
import os
from datetime import datetime
from functools import wraps

import anthropic
import gspread
from dotenv import load_dotenv
from flask import Flask, jsonify, request, render_template_string, Response
from google.oauth2.service_account import Credentials

load_dotenv()

app = Flask(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY       = os.getenv("ANTHROPIC_API_KEY", "")
GOOGLE_SHEET_ID         = os.getenv("GOOGLE_SHEET_ID", "")
GOOGLE_SHEET_TAB        = os.getenv("GOOGLE_SHEET_TAB", "PT Practice Log")
CLAUDE_MODEL            = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")
APP_PASSWORD            = os.getenv("APP_PASSWORD", "")   # set this in Render env vars

# ── Password protection ────────────────────────────────────────────────────────
def require_password(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not APP_PASSWORD:
            return f(*args, **kwargs)   # no password set → open (local dev)
        auth = request.authorization
        if auth and auth.password == APP_PASSWORD:
            return f(*args, **kwargs)
        return Response(
            "Authentication required.",
            401,
            {"WWW-Authenticate": 'Basic realm="PT Practice"'},
        )
    return decorated

# Credentials: prefer JSON string in env var (for cloud), fall back to file (for local)
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON", "")
GOOGLE_CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "google_credentials.json")

GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]

# ── Google Sheets ─────────────────────────────────────────────────────────────
_worksheet = None

def get_worksheet():
    global _worksheet
    if _worksheet is not None:
        return _worksheet

    if GOOGLE_CREDENTIALS_JSON:
        # Cloud deployment: credentials stored as a JSON string in env var
        creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
        creds = Credentials.from_service_account_info(creds_dict, scopes=GOOGLE_SCOPES)
    else:
        # Local development: credentials stored in a file
        creds = Credentials.from_service_account_file(GOOGLE_CREDENTIALS_FILE, scopes=GOOGLE_SCOPES)

    gc = gspread.authorize(creds)

    if GOOGLE_SHEET_ID:
        spreadsheet = gc.open_by_key(GOOGLE_SHEET_ID)
    else:
        # Fallback: open by name (sheet must already exist and be shared with service account)
        spreadsheet = gc.open(GOOGLE_SHEET_TAB)

    # Try to get existing tab or create it
    try:
        ws = spreadsheet.worksheet(GOOGLE_SHEET_TAB)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=GOOGLE_SHEET_TAB, rows=1000, cols=7)

    # Add headers if the sheet is empty
    if not ws.get_all_values():
        ws.append_row(["Timestamp", "Type", "English", "Portuguese", "Status", "Notes"])

    _worksheet = ws
    return ws


def log_to_sheet(type_: str, english: str, portuguese: str, status: str = "", notes: str = ""):
    try:
        ws = get_worksheet()
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ws.append_row([timestamp, type_, english, portuguese, status, notes])
    except Exception as exc:
        print(f"[Google Sheets error] {exc}")


# ── Claude helpers ─────────────────────────────────────────────────────────────
def claude_client():
    return anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def translate_to_portuguese(text: str) -> str:
    """Translate English text to European Portuguese."""
    resp = claude_client().messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": (
                "Translate the following English text to European Portuguese "
                "(Portugal dialect — not Brazilian Portuguese). "
                "Return only the translation, with no explanation or extra text.\n\n"
                f"English: {text}"
            ),
        }],
    )
    return resp.content[0].text.strip()


def check_portuguese(text: str) -> dict:
    """
    Check whether a Portuguese sentence is correct European Portuguese.
    Returns a dict with keys: is_correct, correct_portuguese, explanation, english_translation.
    """
    resp = claude_client().messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": (
                "You are an expert in European Portuguese (Portugal dialect). "
                "Analyse the sentence below for grammar, vocabulary, and idiom — "
                "specifically from a European Portuguese (not Brazilian) perspective.\n\n"
                "Return ONLY a valid JSON object with these exact keys (no markdown, no code fences):\n"
                "{\n"
                '  "is_correct": true or false,\n'
                '  "correct_portuguese": "the correct European Portuguese version '
                '(identical to input if already correct)",\n'
                '  "explanation": "brief explanation of any issues; empty string if correct",\n'
                '  "english_translation": "English translation of the correct version"\n'
                "}\n\n"
                f"Portuguese sentence: {text}"
            ),
        }],
    )
    raw = resp.content[0].text.strip()
    # Strip markdown fences if Claude wraps the JSON
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.lower().startswith("json"):
            raw = raw[4:]
    return json.loads(raw.strip())


# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/")
@require_password
def index():
    return render_template_string(PAGE)


@app.route("/api/translate", methods=["POST"])
@require_password
def api_translate():
    data = request.get_json(force=True) or {}
    english = (data.get("text") or "").strip()
    if not english:
        return jsonify({"error": "No text provided."}), 400

    try:
        portuguese = translate_to_portuguese(english)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    log_to_sheet("Translation", english, portuguese)
    return jsonify({"english": english, "portuguese": portuguese})


@app.route("/api/check", methods=["POST"])
@require_password
def api_check():
    data = request.get_json(force=True) or {}
    portuguese_input = (data.get("text") or "").strip()
    if not portuguese_input:
        return jsonify({"error": "No text provided."}), 400

    try:
        result = check_portuguese(portuguese_input)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    explanation = result.get("explanation", "")
    notes = f"You wrote: {portuguese_input}" + (f" — {explanation}" if explanation else "")
    log_to_sheet(
        "Correction",
        result.get("english_translation", ""),
        result.get("correct_portuguese", portuguese_input),
        "Correct" if result.get("is_correct") else "Incorrect",
        notes,
    )
    return jsonify(result)


# ── UI ────────────────────────────────────────────────────────────────────────
PAGE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
  <meta name="theme-color" content="#166534">
  <title>PT Practice</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

    :root {
      --green-dark:  #14532d;
      --green:       #166534;
      --green-mid:   #16a34a;
      --green-light: #dcfce7;
      --red-light:   #fee2e2;
      --red:         #dc2626;
      --amber-light: #fef9c3;
      --amber:       #ca8a04;
      --bg:          #f1f5f9;
      --surface:     #ffffff;
      --border:      #e2e8f0;
      --text:        #0f172a;
      --muted:       #64748b;
      --radius:      14px;
      --shadow:      0 1px 3px rgba(0,0,0,.07), 0 6px 20px rgba(0,0,0,.06);
    }

    body {
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
      background: var(--bg);
      color: var(--text);
      min-height: 100vh;
      padding-bottom: env(safe-area-inset-bottom);
    }

    /* ── Header ── */
    header {
      background: var(--green);
      padding: 0 20px;
      padding-top: env(safe-area-inset-top);
      display: flex;
      align-items: center;
      height: calc(56px + env(safe-area-inset-top));
      gap: 10px;
      position: sticky;
      top: 0;
      z-index: 10;
    }
    header h1 { color: white; font-size: 17px; font-weight: 700; }
    .flag { font-size: 22px; }

    /* ── Tabs ── */
    .tabs {
      display: flex;
      background: var(--green-dark);
      padding: 0 20px;
      gap: 4px;
    }
    .tab {
      flex: 1;
      padding: 12px 8px 10px;
      border: none;
      background: none;
      color: rgba(255,255,255,.6);
      font-family: inherit;
      font-size: 14px;
      font-weight: 600;
      cursor: pointer;
      border-bottom: 2px solid transparent;
      transition: color .15s, border-color .15s;
      text-align: center;
    }
    .tab.active {
      color: white;
      border-bottom-color: #4ade80;
    }

    /* ── Content ── */
    main { padding: 20px 16px 40px; max-width: 640px; margin: 0 auto; }

    .panel { display: none; }
    .panel.active { display: block; }

    /* ── Card ── */
    .card {
      background: var(--surface);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
      padding: 20px;
      margin-bottom: 16px;
    }

    label.field-label {
      display: block;
      font-size: 11px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: .07em;
      color: var(--muted);
      margin-bottom: 8px;
    }

    textarea {
      width: 100%;
      min-height: 80px;
      padding: 12px 14px;
      border: 1.5px solid var(--border);
      border-radius: 10px;
      font-family: inherit;
      font-size: 16px;
      color: var(--text);
      background: #fafafa;
      resize: vertical;
      outline: none;
      line-height: 1.5;
      transition: border-color .15s, box-shadow .15s;
    }
    textarea:focus {
      background: white;
      border-color: var(--green-mid);
      box-shadow: 0 0 0 3px rgba(22,163,74,.12);
    }
    textarea::placeholder { color: #94a3b8; }

    .btn {
      width: 100%;
      height: 50px;
      border: none;
      border-radius: 10px;
      font-family: inherit;
      font-size: 16px;
      font-weight: 700;
      cursor: pointer;
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      transition: background .15s, transform .1s, opacity .15s;
      margin-top: 12px;
    }
    .btn-primary { background: var(--green); color: white; }
    .btn-primary:hover { background: var(--green-dark); }
    .btn-primary:active { transform: scale(.98); }
    .btn:disabled { opacity: .5; cursor: not-allowed; transform: none; }

    /* ── Result cards ── */
    .result { display: none; margin-top: 16px; }
    .result.show { display: block; }

    .result-card {
      border-radius: var(--radius);
      padding: 18px;
      margin-bottom: 10px;
    }

    .result-card.translation {
      background: var(--green-light);
      border: 1.5px solid #86efac;
    }

    .result-card.correct {
      background: var(--green-light);
      border: 1.5px solid #86efac;
    }

    .result-card.incorrect {
      background: var(--red-light);
      border: 1.5px solid #fca5a5;
    }

    .result-card.explanation {
      background: var(--amber-light);
      border: 1.5px solid #fde047;
    }

    .result-card.english {
      background: #f0f9ff;
      border: 1.5px solid #bae6fd;
    }

    .result-label {
      font-size: 10.5px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: .07em;
      color: var(--muted);
      margin-bottom: 6px;
    }

    .result-text {
      font-size: 18px;
      font-weight: 600;
      line-height: 1.45;
      color: var(--text);
    }

    .result-text.muted { font-weight: 400; font-size: 15px; color: #374151; }

    .status-badge {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 4px 12px;
      border-radius: 99px;
      font-size: 13px;
      font-weight: 700;
      margin-bottom: 12px;
    }
    .status-badge.ok  { background: var(--green-light); color: var(--green); }
    .status-badge.err { background: var(--red-light); color: var(--red); }

    /* ── Spinner ── */
    .spinner {
      display: none;
      width: 20px;
      height: 20px;
      border: 2.5px solid rgba(255,255,255,.35);
      border-top-color: white;
      border-radius: 50%;
      animation: spin .6s linear infinite;
    }
    @keyframes spin { to { transform: rotate(360deg); } }

    .btn.loading .btn-label { display: none; }
    .btn.loading .spinner   { display: block; }

    /* ── Error ── */
    .error-msg {
      display: none;
      background: var(--red-light);
      border: 1.5px solid #fca5a5;
      border-radius: 10px;
      padding: 12px 16px;
      margin-top: 12px;
      font-size: 14px;
      color: var(--red);
    }
    .error-msg.show { display: block; }

    /* ── Logged chip ── */
    .logged-chip {
      display: none;
      align-items: center;
      gap: 6px;
      font-size: 12px;
      color: var(--green);
      margin-top: 10px;
    }
    .logged-chip.show { display: flex; }
  </style>
</head>
<body>

<header>
  <span class="flag">🇵🇹</span>
  <h1>PT Practice</h1>
</header>

<div class="tabs">
  <button class="tab active" onclick="switchTab('translate', this)">Translate</button>
  <button class="tab" onclick="switchTab('check', this)">Check My Portuguese</button>
</div>

<main>

  <!-- ── Translate Panel ── -->
  <div id="panel-translate" class="panel active">
    <div class="card">
      <label class="field-label" for="translateInput">English text</label>
      <textarea id="translateInput" placeholder="e.g. I would like a coffee, please." rows="3"></textarea>
      <button class="btn btn-primary" id="translateBtn" onclick="doTranslate()">
        <span class="btn-label">Translate to Portuguese</span>
        <div class="spinner"></div>
      </button>
      <div class="error-msg" id="translateError"></div>
    </div>

    <div class="result" id="translateResult">
      <div class="result-card translation">
        <div class="result-label">European Portuguese</div>
        <div class="result-text" id="translateOutput"></div>
      </div>
      <div class="result-card english">
        <div class="result-label">Your original</div>
        <div class="result-text muted" id="translateOriginal"></div>
      </div>
      <div class="logged-chip" id="translateLogged">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M20 6L9 17l-5-5"/></svg>
        Logged to Google Sheets
      </div>
    </div>
  </div>

  <!-- ── Check Panel ── -->
  <div id="panel-check" class="panel">
    <div class="card">
      <label class="field-label" for="checkInput">Your Portuguese sentence</label>
      <textarea id="checkInput" placeholder="e.g. Eu gosto muito de viajar." rows="3"></textarea>
      <button class="btn btn-primary" id="checkBtn" onclick="doCheck()">
        <span class="btn-label">Check sentence</span>
        <div class="spinner"></div>
      </button>
      <div class="error-msg" id="checkError"></div>
    </div>

    <div class="result" id="checkResult">
      <div id="statusBadge" class="status-badge"></div>

      <div class="result-card" id="correctCard">
        <div class="result-label" id="correctLabel">Correct Portuguese</div>
        <div class="result-text" id="correctOutput"></div>
      </div>

      <div class="result-card explanation" id="explanationCard">
        <div class="result-label">What to fix</div>
        <div class="result-text muted" id="explanationOutput"></div>
      </div>

      <div class="result-card english" id="englishCard">
        <div class="result-label">English translation</div>
        <div class="result-text muted" id="englishOutput"></div>
      </div>

      <div class="logged-chip" id="checkLogged">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M20 6L9 17l-5-5"/></svg>
        Logged to Google Sheets
      </div>
    </div>
  </div>

</main>

<script>
  // ── Tab switching ──────────────────────────────────────────────────────────
  function switchTab(name, el) {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
    el.classList.add('active');
    document.getElementById('panel-' + name).classList.add('active');
  }

  // ── Translation ────────────────────────────────────────────────────────────
  async function doTranslate() {
    const input = document.getElementById('translateInput').value.trim();
    if (!input) return;

    const btn = document.getElementById('translateBtn');
    const errEl = document.getElementById('translateError');
    const resultEl = document.getElementById('translateResult');

    setLoading(btn, true);
    errEl.classList.remove('show');
    resultEl.classList.remove('show');
    document.getElementById('translateLogged').classList.remove('show');

    try {
      const resp = await fetch('/api/translate', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({text: input}),
      });
      const data = await resp.json();

      if (data.error) throw new Error(data.error);

      document.getElementById('translateOutput').textContent = data.portuguese;
      document.getElementById('translateOriginal').textContent = data.english;
      resultEl.classList.add('show');
      document.getElementById('translateLogged').classList.add('show');
    } catch (e) {
      errEl.textContent = e.message || 'Something went wrong. Please try again.';
      errEl.classList.add('show');
    } finally {
      setLoading(btn, false);
    }
  }

  // ── Check ──────────────────────────────────────────────────────────────────
  async function doCheck() {
    const input = document.getElementById('checkInput').value.trim();
    if (!input) return;

    const btn = document.getElementById('checkBtn');
    const errEl = document.getElementById('checkError');
    const resultEl = document.getElementById('checkResult');

    setLoading(btn, true);
    errEl.classList.remove('show');
    resultEl.classList.remove('show');
    document.getElementById('checkLogged').classList.remove('show');

    try {
      const resp = await fetch('/api/check', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({text: input}),
      });
      const data = await resp.json();

      if (data.error) throw new Error(data.error);

      // Status badge
      const badge = document.getElementById('statusBadge');
      if (data.is_correct) {
        badge.className = 'status-badge ok';
        badge.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><path d="M20 6L9 17l-5-5"/></svg> Correct!';
      } else {
        badge.className = 'status-badge err';
        badge.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><path d="M18 6L6 18M6 6l12 12"/></svg> Needs correction';
      }

      // Correct version card
      const correctCard = document.getElementById('correctCard');
      document.getElementById('correctLabel').textContent = data.is_correct ? 'Your sentence' : 'Correct version';
      correctCard.className = 'result-card ' + (data.is_correct ? 'correct' : 'incorrect');
      document.getElementById('correctOutput').textContent = data.correct_portuguese;

      // Explanation (only if incorrect)
      const expCard = document.getElementById('explanationCard');
      if (data.is_correct || !data.explanation) {
        expCard.style.display = 'none';
      } else {
        expCard.style.display = 'block';
        document.getElementById('explanationOutput').textContent = data.explanation;
      }

      // English translation
      document.getElementById('englishOutput').textContent = data.english_translation;

      resultEl.classList.add('show');
      document.getElementById('checkLogged').classList.add('show');
    } catch (e) {
      errEl.textContent = e.message || 'Something went wrong. Please try again.';
      errEl.classList.add('show');
    } finally {
      setLoading(btn, false);
    }
  }

  // ── Helpers ────────────────────────────────────────────────────────────────
  function setLoading(btn, on) {
    btn.disabled = on;
    btn.classList.toggle('loading', on);
  }

  // Allow Enter (without Shift) to submit in textareas
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && e.target.tagName === 'TEXTAREA') {
      e.preventDefault();
      const panel = e.target.closest('.panel');
      panel.querySelector('.btn-primary').click();
    }
  });
</script>

</body>
</html>
"""

if __name__ == "__main__":
    host = os.getenv("FLASK_HOST", "0.0.0.0")
    port = int(os.getenv("FLASK_PORT", "5001"))
    print(f"\n  PT Practice app running at http://localhost:{port}/")
    print(f"  On your phone (same Wi-Fi): http://<your-mac-ip>:{port}/\n")
    app.run(host=host, port=port, debug=False)
