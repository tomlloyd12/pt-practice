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

import csv
import io
import json
import os
import smtplib
import uuid
import zipfile
from datetime import datetime
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from functools import wraps

import anthropic
import gspread
import psycopg2
import resend
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv
from flask import Flask, jsonify, request, render_template_string, Response, send_file
from google.oauth2.service_account import Credentials
from gtts import gTTS

load_dotenv()

app = Flask(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY       = os.getenv("ANTHROPIC_API_KEY", "")
GOOGLE_SHEET_ID         = os.getenv("GOOGLE_SHEET_ID", "")
GOOGLE_SHEET_TAB        = os.getenv("GOOGLE_SHEET_TAB", "PT Practice Log")
CLAUDE_MODEL            = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")
APP_PASSWORD            = os.getenv("APP_PASSWORD", "")
EMAIL_ADDRESS           = os.getenv("EMAIL_ADDRESS", "tomlloyd12@gmail.com")
EMAIL_APP_PASSWORD      = os.getenv("EMAIL_APP_PASSWORD", "")
RESEND_API_KEY          = os.getenv("RESEND_API_KEY", "")
DATABASE_URL            = os.getenv("DATABASE_URL", "")

# ── Database ──────────────────────────────────────────────────────────────────

def get_db():
    return psycopg2.connect(DATABASE_URL)

def init_db():
    if not DATABASE_URL:
        return
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS logs (
                        id SERIAL PRIMARY KEY,
                        timestamp TIMESTAMP DEFAULT NOW(),
                        type VARCHAR(20),
                        english TEXT,
                        portuguese TEXT,
                        status VARCHAR(20),
                        notes TEXT
                    )
                """)
            conn.commit()
    except Exception as exc:
        print(f"[DB init error] {exc}")

def log_to_db(type_, english, portuguese, status="", notes=""):
    if not DATABASE_URL:
        return
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO logs (type, english, portuguese, status, notes) VALUES (%s, %s, %s, %s, %s)",
                    (type_, english, portuguese, status, notes)
                )
            conn.commit()
    except Exception as exc:
        print(f"[DB log error] {exc}")

init_db()

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

    log_to_db("Translation", english, portuguese)
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
    status = "Correct" if result.get("is_correct") else "Incorrect"
    english = result.get("english_translation", "")
    portuguese = result.get("correct_portuguese", portuguese_input)
    log_to_db("Correction", english, portuguese, status, notes)
    log_to_sheet("Correction", english, portuguese, status, notes)
    return jsonify(result)


# ── Flashcard helpers ─────────────────────────────────────────────────────────

def get_flashcard_entries():
    """Fetch translations and incorrect corrections from the database."""
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set — database not configured.")
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT id, timestamp, type, english, portuguese, status, notes
                FROM logs
                WHERE type = 'Translation' OR status = 'Incorrect'
                ORDER BY timestamp DESC
            """)
            rows = cur.fetchall()

    entries = []
    for row in rows:
        notes = row["notes"] or ""
        original = ""
        explanation = notes
        if notes.startswith("You wrote: "):
            parts = notes[len("You wrote: "):].split(" — ", 1)
            original = parts[0]
            explanation = parts[1] if len(parts) > 1 else ""
        entries.append({
            "id": str(row["id"]),
            "timestamp": str(row["timestamp"])[:16],
            "type": row["type"] or "",
            "english": row["english"] or "",
            "portuguese": row["portuguese"] or "",
            "original": original,
            "explanation": explanation,
        })
    return entries


def generate_flashcard_zip(cards):
    """Generate a ZIP containing flashcards.csv + MP3 audio files."""
    csv_buf = io.StringIO()
    writer = csv.writer(csv_buf)
    writer.writerow(["English", "Correct Portuguese", "You wrote", "Sound"])

    audio_files = {}
    for card in cards:
        pt = card.get("portuguese", "")
        filename = ""
        if pt:
            try:
                mp3_buf = io.BytesIO()
                gTTS(text=pt, lang="pt", tld="pt").write_to_fp(mp3_buf)
                filename = f"{uuid.uuid4().hex[:8]}.mp3"
                audio_files[filename] = mp3_buf.getvalue()
            except Exception as exc:
                print(f"[Audio error] {exc}")
        writer.writerow([
            card.get("english", ""),
            pt,
            card.get("original", ""),
            f"[sound:{filename}]" if filename else "",
        ])

    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("flashcards.csv", csv_buf.getvalue())
        for name, data in audio_files.items():
            zf.writestr(name, data)
    zip_buf.seek(0)
    return zip_buf.read()


def send_flashcard_email(zip_data, card_count):
    """Email the flashcard ZIP via Resend."""
    resend.api_key = RESEND_API_KEY
    resend.Emails.send({
        "from": "PT Practice <onboarding@resend.dev>",
        "to": [EMAIL_ADDRESS],
        "subject": f"PT Flashcards — {card_count} card{'s' if card_count != 1 else ''}",
        "html": (
            f"<p>Your {card_count} Portuguese flashcard{'s are' if card_count != 1 else ' is'} attached.</p>"
            "<p>Import <strong>flashcards.csv</strong> into Anki and put the MP3 files in your Anki media folder.</p>"
        ),
        "attachments": [{"filename": "flashcards.zip", "content": list(zip_data)}],
    })


# ── Flashcard routes ───────────────────────────────────────────────────────────

@app.route("/flashcards")
@require_password
def flashcards_page():
    try:
        entries = get_flashcard_entries()
        error = None
    except Exception as exc:
        entries = []
        error = str(exc)
    return render_template_string(FLASHCARDS_PAGE, mistakes=entries, error=error)


@app.route("/api/log/<int:log_id>", methods=["DELETE"])
@require_password
def delete_log(log_id):
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM logs WHERE id = %s", (log_id,))
            conn.commit()
        return jsonify({"success": True})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/generate-flashcards", methods=["POST"])
@require_password
def api_generate_flashcards():
    try:
        data = request.get_json(force=True) or {}
        cards = data.get("cards", [])
        if not cards:
            return jsonify({"error": "No cards selected."}), 400

        zip_data = generate_flashcard_zip(cards)

        if RESEND_API_KEY:
            send_flashcard_email(zip_data, len(cards))
            return jsonify({"success": True, "message": f"Emailed {len(cards)} flashcard{'s' if len(cards) != 1 else ''} to {EMAIL_ADDRESS}"})
        else:
            return jsonify({"error": "No RESEND_API_KEY set — please add it in Render environment variables."}), 500

    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


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
  <a href="/flashcards" style="margin-left:auto;color:rgba(255,255,255,.85);font-size:13px;font-weight:600;text-decoration:none;background:rgba(255,255,255,.15);padding:5px 12px;border-radius:99px;">📚 Flashcards</a>
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

FLASHCARDS_PAGE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="theme-color" content="#166534">
  <title>PT Flashcards</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    :root {
      --green-dark: #14532d; --green: #166534; --green-mid: #16a34a;
      --green-light: #dcfce7; --red-light: #fee2e2; --bg: #f1f5f9;
      --surface: #fff; --border: #e2e8f0; --text: #0f172a; --muted: #64748b;
      --shadow: 0 1px 3px rgba(0,0,0,.07), 0 6px 20px rgba(0,0,0,.06);
    }
    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: var(--bg); color: var(--text); min-height: 100vh; }
    header { background: var(--green); padding: 0 24px; display: flex; align-items: center; height: 56px; gap: 12px; }
    header h1 { color: white; font-size: 17px; font-weight: 700; }
    .back { color: rgba(255,255,255,.85); font-size: 13px; font-weight: 600; text-decoration: none; background: rgba(255,255,255,.15); padding: 5px 12px; border-radius: 99px; }
    main { max-width: 900px; margin: 0 auto; padding: 28px 20px 60px; }
    h2 { font-size: 20px; font-weight: 700; margin-bottom: 6px; }
    .subtitle { font-size: 14px; color: var(--muted); margin-bottom: 24px; }
    .toolbar { display: flex; gap: 10px; align-items: center; margin-bottom: 16px; flex-wrap: wrap; }
    .btn { height: 40px; padding: 0 20px; border: none; border-radius: 8px; font-family: inherit; font-size: 14px; font-weight: 600; cursor: pointer; display: inline-flex; align-items: center; gap: 7px; transition: background .15s, opacity .15s; }
    .btn-primary { background: var(--green); color: white; }
    .btn-primary:hover { background: var(--green-dark); }
    .btn-primary:disabled { opacity: .4; cursor: not-allowed; }
    .btn-outline { background: white; color: var(--text); border: 1.5px solid var(--border); }
    .btn-outline:hover { background: var(--bg); }
    .count-chip { background: var(--green-light); color: var(--green); font-size: 13px; font-weight: 700; padding: 4px 12px; border-radius: 99px; margin-left: auto; }
    .card-wrap { background: var(--surface); border-radius: 14px; box-shadow: var(--shadow); overflow: hidden; }
    table { width: 100%; border-collapse: collapse; font-size: 14px; }
    thead { background: #f8fafc; border-bottom: 1.5px solid var(--border); }
    th { padding: 10px 14px; text-align: left; font-size: 10.5px; font-weight: 700; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); white-space: nowrap; }
    td { padding: 14px; border-bottom: 1px solid var(--border); vertical-align: top; line-height: 1.5; }
    tr:last-child td { border-bottom: none; }
    tr:hover td { background: #f8fafc; }
    .cb { width: 17px; height: 17px; cursor: pointer; accent-color: var(--green); }
    .pt-text { font-weight: 600; color: var(--green-dark); }
    .wrong-text { color: var(--muted); font-size: 13px; }
    .expl { font-size: 12.5px; color: var(--muted); margin-top: 4px; font-style: italic; }
    .ts { font-size: 12px; color: var(--muted); }
    .type-badge { display: inline-block; font-size: 11px; font-weight: 700; padding: 2px 8px; border-radius: 99px; }
    .type-badge.translation { background: #dbeafe; color: #1d4ed8; }
    .type-badge.correction  { background: var(--red-light); color: #dc2626; }
    .delete-btn { background: none; border: none; cursor: pointer; color: var(--muted); padding: 5px 7px; border-radius: 6px; line-height: 1; transition: color .15s, background .15s; }
    .delete-btn:hover { color: #dc2626; background: var(--red-light); }
    .modal-overlay { display: none; position: fixed; inset: 0; background: rgba(0,0,0,.45); z-index: 100; align-items: center; justify-content: center; padding: 20px; }
    .modal-overlay.show { display: flex; }
    .modal { background: white; border-radius: 16px; padding: 28px 24px; max-width: 360px; width: 100%; box-shadow: 0 20px 60px rgba(0,0,0,.25); }
    .modal h3 { font-size: 17px; font-weight: 700; margin-bottom: 8px; }
    .modal p { font-size: 14px; color: var(--muted); margin-bottom: 22px; line-height: 1.6; }
    .modal-actions { display: flex; gap: 10px; }
    .btn-danger { background: #dc2626; color: white; }
    .btn-danger:hover { background: #b91c1c; }
    .empty { text-align: center; padding: 60px 20px; color: var(--muted); }
    .empty-icon { font-size: 40px; margin-bottom: 12px; display: block; }
    .spinner { display: inline-block; width: 16px; height: 16px; border: 2.5px solid rgba(255,255,255,.35); border-top-color: white; border-radius: 50%; animation: spin .6s linear infinite; }
    @keyframes spin { to { transform: rotate(360deg); } }
    .toast { display: none; position: fixed; bottom: 28px; left: 50%; transform: translateX(-50%); padding: 14px 22px; border-radius: 12px; font-size: 14px; font-weight: 600; box-shadow: 0 8px 30px rgba(0,0,0,.18); z-index: 99; white-space: nowrap; }
    .toast.success { background: var(--green); color: white; }
    .toast.error   { background: #dc2626; color: white; }
    .toast.show { display: block; animation: slideUp .25s ease; }
    @keyframes slideUp { from { transform: translateX(-50%) translateY(12px); opacity: 0; } to { transform: translateX(-50%) translateY(0); opacity: 1; } }
    .error-banner { background: var(--red-light); border: 1.5px solid #fca5a5; border-radius: 10px; padding: 14px 16px; margin-bottom: 20px; font-size: 14px; color: #dc2626; }
  </style>
</head>
<body>
<header>
  <span style="font-size:20px">🇵🇹</span>
  <h1>Mistake Flashcards</h1>
  <a href="/" class="back" style="margin-left:auto">← Back to Practice</a>
</header>

<main>
  <h2>Your mistakes</h2>
  <p class="subtitle">Select the sentences you want to drill, then generate Anki flashcards with audio.</p>

  {% if error %}
  <div class="error-banner">⚠️ Could not load mistakes: {{ error }}</div>
  {% endif %}

  {% if mistakes %}
  <div class="toolbar">
    <button class="btn btn-outline" onclick="selectAll()">Select all</button>
    <button class="btn btn-outline" onclick="selectNone()">Deselect all</button>
    <span class="count-chip" id="countChip">0 selected</span>
    <button class="btn btn-primary" id="generateBtn" onclick="generate()" disabled>
      <span class="btn-label">Generate &amp; Email Flashcards</span>
      <span class="spinner" style="display:none"></span>
    </button>
  </div>

  <div class="card-wrap">
    <table>
      <thead>
        <tr>
          <th style="width:36px"></th>
          <th style="width:90px">Type</th>
          <th>Portuguese</th>
          <th>English</th>
          <th>You wrote</th>
          <th>Date</th>
          <th style="width:36px"></th>
        </tr>
      </thead>
      <tbody>
        {% for m in mistakes %}
        <tr>
          <td><input type="checkbox" class="cb" onchange="updateCount()"
            data-id="{{ m.id }}"
            data-english="{{ m.english }}"
            data-portuguese="{{ m.portuguese }}"
            data-original="{{ m.original }}"
          ></td>
          <td>
            {% if m.type == 'Translation' %}
              <span class="type-badge translation">Translate</span>
            {% else %}
              <span class="type-badge correction">Mistake</span>
            {% endif %}
          </td>
          <td class="pt-text">{{ m.portuguese }}</td>
          <td>{{ m.english }}</td>
          <td>
            {% if m.original %}
              <span class="wrong-text">{{ m.original }}</span>
              {% if m.explanation %}<div class="expl">{{ m.explanation }}</div>{% endif %}
            {% else %}
              <span class="ts">—</span>
            {% endif %}
          </td>
          <td class="ts">{{ m.timestamp[:10] if m.timestamp else '' }}</td>
          <td><button class="delete-btn" onclick="deleteEntry('{{ m.id }}', this)" title="Delete">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14H6L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/><path d="M9 6V4h6v2"/></svg>
          </button></td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>

  {% else %}
  <div class="card-wrap">
    <div class="empty">
      <span class="empty-icon">🎉</span>
      <p>No mistakes logged yet.<br>Use the Check tab to practise and your errors will appear here.</p>
    </div>
  </div>
  {% endif %}
</main>

<div class="toast" id="toast"></div>

<!-- Delete confirmation modal -->
<div class="modal-overlay" id="deleteModal">
  <div class="modal">
    <h3>Delete from log?</h3>
    <p>Remove these <strong id="deleteCount">0</strong> entries from your practice log?<br>
    <small style="color:#16a34a">✓ They'll stay in Google Sheets.</small></p>
    <div class="modal-actions">
      <button class="btn btn-danger" style="flex:1" onclick="confirmDelete()">Yes, delete</button>
      <button class="btn btn-outline" style="flex:1" onclick="cancelDelete()">Keep them</button>
    </div>
  </div>
</div>

<script>
  let pendingDeleteIds = [];

  function updateCount() {
    const checked = document.querySelectorAll('.cb:checked').length;
    document.getElementById('countChip').textContent = checked + ' selected';
    document.getElementById('generateBtn').disabled = checked === 0;
  }

  function selectAll()  { document.querySelectorAll('.cb').forEach(c => c.checked = true);  updateCount(); }
  function selectNone() { document.querySelectorAll('.cb').forEach(c => c.checked = false); updateCount(); }

  async function deleteEntry(id, btn) {
    const row = btn.closest('tr');
    row.style.opacity = '0.4';
    try {
      const resp = await fetch('/api/log/' + id, { method: 'DELETE' });
      const data = await resp.json();
      if (data.error) throw new Error(data.error);
      row.remove();
      updateCount();
      showToast('Entry deleted', 'success');
    } catch (e) {
      row.style.opacity = '1';
      showToast(e.message || 'Delete failed', 'error');
    }
  }

  async function generate() {
    const checked = [...document.querySelectorAll('.cb:checked')];
    if (!checked.length) return;

    const cards = checked.map(c => ({
      english:    c.dataset.english,
      portuguese: c.dataset.portuguese,
      original:   c.dataset.original,
    }));

    const btn     = document.getElementById('generateBtn');
    const label   = btn.querySelector('.btn-label');
    const spinner = btn.querySelector('.spinner');
    btn.disabled   = true;
    label.style.display  = 'none';
    spinner.style.display = 'inline-block';

    try {
      const resp = await fetch('/api/generate-flashcards', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({cards}),
      });
      const data = await resp.json();
      if (data.error) throw new Error(data.error);
      showToast(data.message, 'success');
      // Ask if they want to delete the sent entries
      pendingDeleteIds = checked.map(c => c.dataset.id).filter(Boolean);
      document.getElementById('deleteCount').textContent = pendingDeleteIds.length;
      setTimeout(() => document.getElementById('deleteModal').classList.add('show'), 600);
    } catch (e) {
      showToast(e.message || 'Something went wrong', 'error');
    } finally {
      btn.disabled = false;
      label.style.display  = 'inline';
      spinner.style.display = 'none';
      updateCount();
    }
  }

  async function confirmDelete() {
    document.getElementById('deleteModal').classList.remove('show');
    for (const id of pendingDeleteIds) {
      try { await fetch('/api/log/' + id, { method: 'DELETE' }); } catch (_) {}
      const cb = document.querySelector('.cb[data-id="' + id + '"]');
      if (cb) cb.closest('tr').remove();
    }
    pendingDeleteIds = [];
    updateCount();
    showToast('Entries deleted from log', 'success');
  }

  function cancelDelete() {
    document.getElementById('deleteModal').classList.remove('show');
    pendingDeleteIds = [];
  }

  function showToast(msg, type) {
    const t = document.getElementById('toast');
    t.textContent = msg;
    t.className = 'toast ' + type + ' show';
    setTimeout(() => t.classList.remove('show'), 4000);
  }
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
