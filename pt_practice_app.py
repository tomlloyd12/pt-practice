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
import re
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
import requests
import resend
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, request, render_template_string, Response, send_file
from google.oauth2.service_account import Credentials
from gtts import gTTS

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", os.urandom(32).hex())

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
    return psycopg2.connect(DATABASE_URL, connect_timeout=10)

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

# ── Password protection (cookie-based session) ────────────────────────────────
from flask import session

LOGIN_PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<meta name="theme-color" content="#166534">
<title>PT Practice \u2014 Login</title>
<style>
  *,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
  body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;background:#f1f5f9;display:flex;align-items:center;justify-content:center;min-height:100vh;padding:20px;}
  .login{background:white;border-radius:16px;box-shadow:0 4px 24px rgba(0,0,0,.1);padding:36px 28px;width:100%;max-width:340px;text-align:center;}
  .login h1{font-size:28px;margin-bottom:4px;}
  .login p{font-size:14px;color:#64748b;margin-bottom:24px;}
  input[type=password]{width:100%;padding:12px 14px;border:1.5px solid #e2e8f0;border-radius:10px;font-size:16px;font-family:inherit;outline:none;margin-bottom:14px;transition:border-color .15s;}
  input[type=password]:focus{border-color:#16a34a;box-shadow:0 0 0 3px rgba(22,163,74,.12);}
  button{width:100%;padding:14px;background:#166534;color:white;border:none;border-radius:10px;font-size:16px;font-weight:700;cursor:pointer;font-family:inherit;}
  button:hover{background:#14532d;}
  .err{color:#dc2626;font-size:13px;margin-top:10px;}
</style></head><body>
<form class="login" method="post" action="/login">
  <h1>\U0001f1f5\U0001f1f9</h1>
  <p>Enter password to continue</p>
  <input type="password" name="password" placeholder="Password" autofocus autocomplete="current-password">
  <button type="submit">Log in</button>
  {% if error %}<div class="err">{{ error }}</div>{% endif %}
</form></body></html>"""


def require_password(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not APP_PASSWORD:
            return f(*args, **kwargs)
        if session.get("authed"):
            return f(*args, **kwargs)
        # For API/AJAX calls, return 401 JSON so JS can redirect
        if request.is_json or request.headers.get("X-Requested-With"):
            return jsonify({"error": "Session expired \u2014 please refresh the page."}), 401
        return redirect("/login?next=" + request.path)
    return decorated


@app.route("/login", methods=["GET", "POST"])
def login():
    if not APP_PASSWORD:
        return redirect("/")
    error = None
    if request.method == "POST":
        if request.form.get("password") == APP_PASSWORD:
            session["authed"] = True
            session.permanent = True
            app.permanent_session_lifetime = __import__("datetime").timedelta(days=90)
            return redirect(request.args.get("next", "/"))
        error = "Wrong password"
    return render_template_string(LOGIN_PAGE, error=error)

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
import time as _time

def claude_client():
    return anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


class _UserError(Exception):
    """An error with a user-friendly message."""
    pass


def _call_claude(max_tokens: int, prompt: str, retries: int = 2) -> str:
    """Call the Claude API with automatic retry on transient failures.

    Returns the text content of the response.
    Raises _UserError with a friendly message on permanent failure.
    """
    last_exc = None
    for attempt in range(1 + retries):
        try:
            resp = claude_client().messages.create(
                model=CLAUDE_MODEL,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.content[0].text.strip()
        except anthropic.APIConnectionError as exc:
            last_exc = exc
        except anthropic.RateLimitError as exc:
            last_exc = exc
        except anthropic.APIStatusError as exc:
            if exc.status_code >= 500:
                last_exc = exc
            else:
                # 4xx errors (bad request, auth) won't fix with a retry
                raise _UserError("Something went wrong — please try again.")
        except Exception as exc:
            raise _UserError("Something went wrong — please try again.")
        # Wait briefly before retry
        if attempt < retries:
            _time.sleep(1.5 * (attempt + 1))
    # All retries exhausted
    print(f"[Claude API error after {1 + retries} attempts] {last_exc}")
    raise _UserError("Could not reach the translation service — please check your connection and try again.")


def _parse_json_response(raw: str):
    """Parse a JSON response from Claude, stripping markdown fences if present."""
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.lower().startswith("json"):
            raw = raw[4:]
    try:
        return json.loads(raw.strip())
    except json.JSONDecodeError:
        raise _UserError("Got an unexpected response — please try again.")


def translate_to_portuguese(text: str) -> str:
    """Translate English text to European Portuguese."""
    return _call_claude(
        1024,
        "Translate the following English text to European Portuguese "
        "(Portugal dialect — not Brazilian Portuguese). "
        "Return only the translation, with no explanation or extra text.\n\n"
        f"English: {text}",
    )


def check_portuguese(text: str) -> dict:
    """
    Check whether a Portuguese sentence is correct European Portuguese.
    Returns a dict with keys: is_correct, correct_portuguese, explanation, english_translation.
    """
    raw = _call_claude(
        1024,
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
        f"Portuguese sentence: {text}",
    )
    return _parse_json_response(raw)


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
    except _UserError as exc:
        return jsonify({"error": str(exc)}), 500
    except Exception:
        return jsonify({"error": "Something went wrong — please try again."}), 500

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
    except _UserError as exc:
        return jsonify({"error": str(exc)}), 500
    except Exception:
        return jsonify({"error": "Something went wrong — please try again."}), 500

    explanation = result.get("explanation", "")
    notes = f"You wrote: {portuguese_input}" + (f" — {explanation}" if explanation else "")
    status = "Correct" if result.get("is_correct") else "Incorrect"
    english = result.get("english_translation", "")
    portuguese = result.get("correct_portuguese", portuguese_input)
    log_to_db("Correction", english, portuguese, status, notes)
    log_to_sheet("Correction", english, portuguese, status, notes)
    return jsonify(result)


# ── Translation Practice ──────────────────────────────────────────────────────

practice_state = {"sentences": [], "current": 0, "results": []}


def split_sentences(text: str) -> list:
    parts = re.split(r'(?<=[.!?])\s+', text.strip())
    return [s.strip() for s in parts if len(s.strip()) > 3]


def grade_translation_practice(english: str, user_pt: str) -> dict:
    prompt = (
        f'English sentence: "{english}"\n'
        f'Student\'s European Portuguese: "{user_pt}"\n\n'
        "Grade this translation. Reply with a JSON object with exactly these keys:\n"
        "- \"correct_translation\": the ideal European Portuguese translation (not Brazilian). REQUIRED — always provide this.\n"
        "- \"score\": \"correct\", \"partial\", or \"wrong\"\n"
        "- \"feedback\": 1 sentence overall summary. REQUIRED — always provide this.\n"
        "- \"mistakes\": array of mistake objects. REQUIRED for 'wrong' or 'partial' — always include at least one entry explaining the key error(s). Empty array only when score is 'correct'. Each object has:\n"
        "  - \"pt_key_phrase\": the correct Portuguese word/phrase (what it should have been)\n"
        "  - \"en_key_phrase\": English meaning/gloss of that phrase\n"
        "  - \"feedback\": 1 sentence explaining this specific error\n"
        "  - \"worth_flashcard\": true if useful to study\n"
        "Return only the JSON object, nothing else."
    )
    raw = _call_claude(700, prompt)
    return _parse_json_response(raw)


def generate_practice_sentence(pt_key_phrase: str) -> str:
    raw = _call_claude(
        80,
        f"Write one short, natural European Portuguese sentence (not Brazilian, max 12 words) "
        f"that uses the word or phrase \"{pt_key_phrase}\". "
        "Return only the Portuguese sentence, nothing else.",
    )
    return raw.strip('"')


_DIFF_MAP = {
    "b1": "simple, clear sentences with common vocabulary and straightforward grammar (B1 level)",
    "b2": "natural conversational language with idiomatic expressions and some complex structures (B2 level)",
    "c1": "sophisticated language with nuance, colloquialisms, and complex sentence structures (C1 level)",
    "c2": "native-level language with slang, cultural references, ambiguity, and subtle register shifts (C2 level)",
}


def generate_conversation(difficulty: str = "b1") -> str:
    """Use Claude to generate a realistic conversation/interview/podcast transcript."""
    diff_desc = _DIFF_MAP.get(difficulty, _DIFF_MAP["b1"])
    prompt = (
        "Write a short, realistic excerpt from a conversation in English (4-6 lines) "
        "for Portuguese translation practice. "
        "Pick a RANDOM format each time — e.g. a podcast interview, a casual chat between friends, "
        "a phone call, a WhatsApp voice note, two colleagues at lunch, an overheard conversation "
        "on a train, a radio call-in, a vlog monologue, a flatmate argument, catching up at a party, etc. "
        "Pick a different format and topic every time — be creative and varied. "
        "Use 2-3 speakers with names (e.g. 'Ana:', 'Host:', 'Mark:'). "
        f"Use {diff_desc}. "
        "Make it sound like how people ACTUALLY talk — with contractions, fillers, "
        "incomplete thoughts, natural rhythm. Not formal or literary. "
        "Return only the conversation, nothing else."
    )
    return _call_claude(300, prompt)


def generate_practice_paragraph(difficulty: str = "b1") -> str:
    """Use Claude to generate a conversational English snippet for translation practice."""
    diff_desc = _DIFF_MAP.get(difficulty, _DIFF_MAP["b1"])
    prompt = (
        "Write a short, natural conversational English passage (4-5 sentences) for Portuguese translation practice. "
        "Pick a RANDOM everyday scenario — e.g. ordering at a cafe, asking for directions, "
        "chatting with a friend about weekend plans, a phone call to book an appointment, "
        "small talk with a neighbour, haggling at a market, catching up after a trip, "
        "texting about dinner plans, complaining about the weather, etc. "
        "Pick a different scenario every time — be creative and varied. "
        f"Use {diff_desc}. "
        "Write it as natural dialogue or narration that someone might actually say or hear in real life. "
        "Return only the passage, nothing else."
    )
    return _call_claude(250, prompt)


# ── Practice routes ────────────────────────────────────────────────────────────

@app.route("/practice/")
@require_password
def practice_home():
    practice_state.update({"sentences": [], "current": 0, "results": []})
    return render_template_string(PRACTICE_START_PAGE)


@app.route("/practice/start", methods=["POST"])
@require_password
def practice_start():
    text = request.form.get("text", "").strip()
    sentences = split_sentences(text) if text else []
    if not sentences:
        return redirect("/practice/")
    practice_state.update({"sentences": sentences, "current": 0, "results": []})
    return redirect("/practice/go")


@app.route("/practice/go")
@require_password
def practice_go():
    if not practice_state["sentences"]:
        return redirect("/practice/")
    if practice_state["current"] >= len(practice_state["sentences"]):
        return redirect("/practice/summary")
    total   = len(practice_state["sentences"])
    current = practice_state["current"] + 1
    return render_template_string(
        PRACTICE_SENTENCE_PAGE,
        sentence=practice_state["sentences"][practice_state["current"]],
        current=current, total=total,
        progress=int((practice_state["current"] / total) * 100),
    )


@app.route("/practice/grade", methods=["POST"])
@require_password
def practice_grade():
    data = request.get_json(force=True) or {}
    try:
        result = grade_translation_practice(data.get("english", ""), data.get("user_pt", ""))
    except _UserError as exc:
        return jsonify({"error": str(exc)}), 500
    except Exception:
        return jsonify({"error": "Something went wrong — please try again."}), 500
    return jsonify(result)


@app.route("/practice/ask", methods=["POST"])
@require_password
def practice_ask():
    """Answer a follow-up question about a graded translation."""
    data = request.get_json(force=True) or {}
    question = (data.get("question") or "").strip()
    if not question:
        return jsonify({"error": "No question provided"}), 400
    # Build context from the grading result
    english = data.get("english", "")
    user_pt = data.get("user_translation", "")
    correct_pt = data.get("correct_translation", "")
    score = data.get("score", "")
    feedback = data.get("feedback", "")
    mistakes = data.get("mistakes", [])
    mistakes_text = ""
    for m in mistakes:
        mistakes_text += f"\n- {m.get('pt_key_phrase','')}: {m.get('feedback','')}"
    prompt = (
        "You are a European Portuguese language tutor. "
        "A student just translated a sentence and received feedback. "
        "Now they have a follow-up question. Answer it clearly and concisely.\n\n"
        f"English sentence: \"{english}\"\n"
        f"Student wrote: \"{user_pt}\"\n"
        f"Correct translation: \"{correct_pt}\"\n"
        f"Score: {score}\n"
        f"Feedback: {feedback}\n"
    )
    if mistakes_text:
        prompt += f"Specific mistakes:{mistakes_text}\n"
    prompt += f"\nStudent's question: \"{question}\"\n\nAnswer briefly (2-4 sentences). Use European Portuguese examples."
    try:
        answer = _call_claude(300, prompt)
        return jsonify({"answer": answer})
    except _UserError as exc:
        return jsonify({"error": str(exc)}), 500
    except Exception:
        return jsonify({"error": "Something went wrong — please try again."}), 500


@app.route("/practice/advance", methods=["POST"])
@require_password
def practice_advance():
    data = request.get_json(force=True) or {}
    practice_state["results"].append(data)
    practice_state["current"] += 1
    return jsonify({"ok": True})


@app.route("/practice/summary")
@require_password
def practice_summary():
    if not practice_state["results"] and not practice_state["sentences"]:
        return redirect("/practice/")
    return render_template_string(
        PRACTICE_SUMMARY_PAGE,
        results=practice_state["results"],
        total_sentences=len(practice_state["sentences"]),
    )


@app.route("/practice/generate-sentence", methods=["POST"])
@require_password
def practice_generate_sentence():
    data = request.get_json(force=True) or {}
    pt_key = data.get("pt_key_phrase", "").strip()
    try:
        sentence = generate_practice_sentence(pt_key) if pt_key else ""
    except _UserError as exc:
        return jsonify({"error": str(exc)}), 500
    except Exception:
        return jsonify({"error": "Something went wrong — please try again."}), 500
    return jsonify({"sentence": sentence})


@app.route("/practice/get-paragraph")
@require_password
def practice_get_paragraph():
    source     = request.args.get("source", "ai")
    difficulty = request.args.get("difficulty", "b1")
    try:
        if source == "conversation":
            text = generate_conversation(difficulty)
        else:
            text = generate_practice_paragraph(difficulty)
        src_name = None
        return jsonify({"text": text, "source": src_name})
    except _UserError as e:
        return jsonify({"error": str(e)}), 500
    except ValueError as e:
        # fetch_article_paragraph raises ValueError for expected issues (no article, too short)
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": "Could not fetch text — please try again."}), 500


@app.route("/practice/add-to-flashcards", methods=["POST"])
@require_password
def practice_add_to_flashcards():
    data = request.get_json(force=True) or {}
    items = data.get("items", [])
    count = 0
    for item in items:
        english    = item.get("english", "")    # full English sentence
        portuguese = item.get("portuguese", "") # specific PT phrase (or full sentence)
        en_phrase  = item.get("en_phrase", "").strip()  # English gloss of the phrase
        user_wrote = item.get("user_wrote", "")
        feedback   = item.get("feedback", "")
        # Flashcard front: the specific English phrase if we have one, else the full sentence
        fc_english = en_phrase if en_phrase else english
        # Build notes with full context
        notes_parts = []
        if user_wrote:
            notes_parts.append(f"You wrote: {user_wrote}")
        if feedback:
            notes_parts.append(feedback)
        if en_phrase and english:
            notes_parts.append(f"From: \"{english}\"")
        notes = " — ".join(notes_parts)
        log_to_db("Practice", fc_english, portuguese, "Incorrect", notes)
        count += 1
    return jsonify({"ok": True, "count": count})


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


@app.route("/api/suggest-flashcards", methods=["POST"])
@require_password
def api_suggest_flashcards():
    """Use Claude to generate flashcard suggestions from a word, phrase, or concept."""
    data = request.get_json(force=True) or {}
    user_input = (data.get("input") or "").strip()
    if not user_input:
        return jsonify({"error": "Please enter a word, phrase, or concept."}), 400
    prompt = (
        "You are a European Portuguese language tutor creating flashcards.\n\n"
        f"The student wants to learn: \"{user_input}\"\n\n"
        "Generate flashcard(s) for this. The input could be:\n"
        "- An English word/phrase → create Portuguese translation card(s)\n"
        "- A Portuguese word/phrase → create English meaning card(s)\n"
        "- A concept or grammar instruction → create several cards illustrating the point\n\n"
        "Return a JSON array of objects. Each object has:\n"
        "- \"english\": the English side (word, phrase, or short sentence)\n"
        "- \"portuguese\": the European Portuguese side\n"
        "- \"notes\": optional brief usage note or context (1 sentence max, or empty string)\n\n"
        "Rules:\n"
        "- Use European Portuguese (not Brazilian)\n"
        "- For single words, include the article (o/a) for nouns\n"
        "- For grammar concepts, create 2-5 example cards that illustrate different uses\n"
        "- Keep it practical and conversational\n"
        "- Return ONLY the JSON array, nothing else"
    )
    try:
        raw = _call_claude(600, prompt)
        cards = _parse_json_response(raw)
        if not isinstance(cards, list):
            cards = [cards]
        return jsonify({"cards": cards})
    except _UserError as exc:
        return jsonify({"error": str(exc)}), 500
    except Exception:
        return jsonify({"error": "Something went wrong — please try again."}), 500


@app.route("/api/parse-notes", methods=["POST"])
@require_password
def api_parse_notes():
    """Parse lesson notes and generate flashcard suggestions with example sentences."""
    data = request.get_json(force=True) or {}
    notes = (data.get("notes") or "").strip()
    if not notes:
        return jsonify({"error": "Please paste your lesson notes."}), 400
    prompt = (
        "You are a European Portuguese language tutor. A student has shared their lesson notes below.\n\n"
        "NOTES:\n" + notes + "\n\n"
        "Parse these notes and create flashcard suggestions. The notes may contain:\n"
        "- Vocabulary words (sometimes with translations, sometimes without)\n"
        "- Sentences or phrases (corrections, examples from class)\n"
        "- Grammar patterns or expressions\n"
        "- Bullet points, asterisks, dashes, or free-form text\n\n"
        "For EACH distinct item in the notes, create a flashcard object.\n"
        "Return a JSON array of objects. Each object has:\n"
        "- \"portuguese\": the Portuguese word, phrase, or key expression\n"
        "- \"english\": the English translation or meaning\n"
        "- \"notes\": brief usage note, grammar context, or explanation (1 sentence, or empty string)\n"
        "- \"example_sentence\": a short, natural European Portuguese example sentence using the word/phrase (max 12 words)\n\n"
        "Rules:\n"
        "- Use European Portuguese (not Brazilian)\n"
        "- For nouns, include the article (o/a)\n"
        "- If the notes already contain example sentences, you can adapt them for the example_sentence\n"
        "- If a note line IS a full sentence (like a correction from class), the portuguese field should be the key phrase/word being taught, and the example_sentence can be the full sentence or a variation\n"
        "- Create one card per distinct concept — don't merge unrelated items\n"
        "- Return ONLY the JSON array, nothing else"
    )
    try:
        raw = _call_claude(1200, prompt)
        cards = _parse_json_response(raw)
        if not isinstance(cards, list):
            cards = [cards]
        return jsonify({"cards": cards})
    except _UserError as exc:
        return jsonify({"error": str(exc)}), 500
    except Exception:
        return jsonify({"error": "Something went wrong — please try again."}), 500


@app.route("/api/regen-sentence", methods=["POST"])
@require_password
def api_regen_sentence():
    """Regenerate the example sentence for a flashcard."""
    data = request.get_json(force=True) or {}
    portuguese = (data.get("portuguese") or "").strip()
    if not portuguese:
        return jsonify({"error": "No phrase provided."}), 400
    try:
        sentence = generate_practice_sentence(portuguese)
        return jsonify({"sentence": sentence})
    except _UserError as exc:
        return jsonify({"error": str(exc)}), 500
    except Exception:
        return jsonify({"error": "Could not generate sentence — please try again."}), 500


@app.route("/api/save-flashcards", methods=["POST"])
@require_password
def api_save_flashcards():
    """Save selected generated flashcards to the database."""
    data = request.get_json(force=True) or {}
    items = data.get("items", [])
    if not items:
        return jsonify({"error": "No cards selected."}), 400
    count = 0
    for item in items:
        english = (item.get("english") or "").strip()
        portuguese = (item.get("portuguese") or "").strip()
        notes = (item.get("notes") or "").strip()
        if english and portuguese:
            log_to_db("Generated", english, portuguese, "", notes)
            count += 1
    return jsonify({"count": count})


@app.route("/api/explain", methods=["POST"])
@require_password
def api_explain():
    """Explain a Portuguese concept and suggest flashcards."""
    data = request.get_json(force=True) or {}
    question = (data.get("question") or "").strip()
    if not question:
        return jsonify({"error": "Please describe what you want to understand."}), 400
    prompt = (
        "You are a European Portuguese language tutor. A student has a question or "
        "wants to understand a concept. Explain it clearly and concisely, then suggest "
        "flashcards to help them learn the relevant vocabulary/grammar.\n\n"
        f"Student says: \"{question}\"\n\n"
        "Reply with a JSON object with exactly these keys:\n"
        "- \"explanation\": a clear, helpful explanation (2-5 sentences, use European Portuguese examples with English translations in parentheses)\n"
        "- \"cards\": an array of flashcard suggestions, each with:\n"
        "  - \"portuguese\": the Portuguese word, phrase, or expression\n"
        "  - \"english\": the English meaning\n"
        "  - \"notes\": brief usage note or context (1 sentence)\n"
        "  - \"example_sentence\": a short European Portuguese example sentence (max 12 words)\n\n"
        "Rules:\n"
        "- Use European Portuguese (not Brazilian)\n"
        "- For nouns, include the article (o/a)\n"
        "- Generate 2-6 cards depending on how many concepts are involved\n"
        "- Return ONLY the JSON object, nothing else"
    )
    try:
        raw = _call_claude(1200, prompt)
        result = _parse_json_response(raw)
        if not isinstance(result, dict):
            return jsonify({"error": "Unexpected response format."}), 500
        return jsonify(result)
    except _UserError as exc:
        return jsonify({"error": str(exc)}), 500
    except Exception:
        return jsonify({"error": "Something went wrong \u2014 please try again."}), 500


# ── Shared bottom navigation ──────────────────────────────────────────────────
_BOTTOM_NAV_CSS = """
    .bottom-nav {
      position: fixed; bottom: 0; left: 0; right: 0;
      height: calc(60px + env(safe-area-inset-bottom));
      background: white; border-top: 1px solid #e2e8f0;
      display: flex; z-index: 100;
      padding-bottom: env(safe-area-inset-bottom);
    }
    .nav-item {
      flex: 1; display: flex; flex-direction: column;
      align-items: center; justify-content: center; gap: 2px;
      border: none; background: none; cursor: pointer;
      color: #94a3b8; font-size: 10px; font-weight: 600;
      text-decoration: none; padding: 6px 4px; font-family: inherit;
      transition: color .15s; -webkit-tap-highlight-color: transparent;
    }
    .nav-item .nav-icon { font-size: 22px; line-height: 1; }
    .nav-item.active { color: #166534; }
"""

def _bottom_nav_html(active):
    """Return bottom nav HTML with the given tab highlighted."""
    items = [
        ("🔄", "Translate", "/?tab=translate", "translate"),
        ("✓",  "Check",     "/?tab=check",     "check"),
        ("⚡", "Generate",  "/?tab=generate",   "generate"),
        ("💡", "Explain",   "/?tab=explain",    "explain"),
        ("📝", "Practice",  "/practice/",       "practice"),
        ("📚", "Cards",     "/flashcards",      "cards"),
    ]
    nav = '<nav class="bottom-nav">'
    for icon, label, href, key in items:
        cls = "nav-item active" if key == active else "nav-item"
        nav += f'<a href="{href}" class="{cls}"><span class="nav-icon">{icon}</span><span>{label}</span></a>'
    nav += '</nav>'
    return nav


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
      --blue-light:  #eff6ff;
      --bg:          #f1f5f9;
      --surface:     #ffffff;
      --border:      #e2e8f0;
      --text:        #0f172a;
      --muted:       #64748b;
      --radius:      16px;
      --shadow:      0 1px 3px rgba(0,0,0,.07), 0 6px 24px rgba(0,0,0,.07);
    }
    body {
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
      background: var(--bg);
      color: var(--text);
      min-height: 100vh;
      padding-bottom: calc(68px + env(safe-area-inset-bottom));
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
      z-index: 50;
    }
    header h1 { color: white; font-size: 18px; font-weight: 800; letter-spacing: -.3px; }
    .flag { font-size: 24px; }

    /* ── Bottom Navigation ── */
    .bottom-nav {
      position: fixed;
      bottom: 0; left: 0; right: 0;
      height: calc(60px + env(safe-area-inset-bottom));
      background: white;
      border-top: 1px solid var(--border);
      display: flex;
      z-index: 100;
      padding-bottom: env(safe-area-inset-bottom);
    }
    .nav-item {
      flex: 1;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      gap: 2px;
      border: none;
      background: none;
      cursor: pointer;
      color: #94a3b8;
      font-size: 10px;
      font-weight: 600;
      text-decoration: none;
      padding: 6px 4px;
      font-family: inherit;
      transition: color .15s;
      -webkit-tap-highlight-color: transparent;
    }
    .nav-item .nav-icon { font-size: 22px; line-height: 1; }
    .nav-item.active { color: var(--green); }

    /* ── Main ── */
    main { padding: 20px 16px; max-width: 640px; margin: 0 auto; }
    .panel { display: none; }
    .panel.active { display: block; }

    /* Panel titles */
    .panel-title { font-size: 24px; font-weight: 800; letter-spacing: -.5px; margin-bottom: 4px; }
    .panel-sub { font-size: 14px; color: var(--muted); margin-bottom: 20px; line-height: 1.5; }

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
      min-height: 88px;
      padding: 14px;
      border: 1.5px solid var(--border);
      border-radius: 12px;
      font-family: inherit;
      font-size: 16px;
      color: var(--text);
      background: #fafafa;
      resize: none;
      outline: none;
      line-height: 1.55;
      transition: border-color .15s, box-shadow .15s, background .15s;
      -webkit-appearance: none;
    }
    textarea:focus {
      background: white;
      border-color: var(--green-mid);
      box-shadow: 0 0 0 3px rgba(22,163,74,.12);
    }
    textarea::placeholder { color: #b0bec5; }

    /* ── Buttons ── */
    .btn {
      width: 100%;
      height: 52px;
      border: none;
      border-radius: 13px;
      font-family: inherit;
      font-size: 17px;
      font-weight: 700;
      cursor: pointer;
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      transition: background .15s, transform .1s, opacity .15s;
      margin-top: 14px;
      -webkit-tap-highlight-color: transparent;
    }
    .btn-primary { background: var(--green); color: white; }
    .btn-primary:hover { background: var(--green-dark); }
    .btn-primary:active { transform: scale(.98); }
    .btn:disabled { opacity: .45; cursor: not-allowed; transform: none; }

    /* ── Result section ── */
    .result { display: none; margin-top: 0; }
    .result.show { display: block; }
    .result-card {
      border-radius: 14px;
      padding: 18px 20px;
      margin-bottom: 10px;
      position: relative;
    }
    .result-card.translation { background: var(--green-light); border: 1.5px solid #86efac; }
    .result-card.correct     { background: var(--green-light); border: 1.5px solid #86efac; }
    .result-card.incorrect   { background: var(--red-light);   border: 1.5px solid #fca5a5; }
    .result-card.explanation { background: var(--amber-light); border: 1.5px solid #fde047; }
    .result-card.english     { background: var(--blue-light);  border: 1.5px solid #bfdbfe; }
    .result-label {
      font-size: 10.5px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: .07em;
      color: var(--muted);
      margin-bottom: 6px;
    }
    .result-text { font-size: 19px; font-weight: 600; line-height: 1.45; color: var(--text); }
    .result-text.muted { font-weight: 400; font-size: 16px; color: #374151; }

    /* Copy button */
    .copy-btn {
      position: absolute;
      top: 14px; right: 14px;
      background: rgba(0,0,0,.07);
      border: none;
      border-radius: 7px;
      padding: 5px 10px;
      font-size: 12px;
      font-weight: 600;
      color: var(--muted);
      cursor: pointer;
      font-family: inherit;
      transition: background .15s, color .15s;
      -webkit-tap-highlight-color: transparent;
    }
    .copy-btn:active { background: rgba(0,0,0,.14); }
    .copy-btn.copied { background: var(--green-light); color: var(--green-dark); }

    .status-badge {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 6px 14px;
      border-radius: 99px;
      font-size: 14px;
      font-weight: 700;
      margin-bottom: 14px;
    }
    .status-badge.ok  { background: var(--green-light); color: var(--green-dark); }
    .status-badge.err { background: var(--red-light); color: var(--red); }

    /* ── Spinner ── */
    .spinner {
      display: none;
      width: 22px; height: 22px;
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
      border-radius: 12px;
      padding: 14px 16px;
      margin-top: 12px;
      font-size: 14px;
      color: var(--red);
      line-height: 1.5;
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

    /* ── Generate flashcard cards ── */
    .gen-card {
      background: var(--surface);
      border-radius: 12px;
      box-shadow: var(--shadow);
      padding: 14px 16px;
      margin-bottom: 10px;
      border: 1.5px solid transparent;
      transition: border-color .15s, background .15s;
    }
    .gen-card:has(.gen-cb:checked) {
      border-color: var(--green-mid);
      background: #f0fdf4;
    }
    .gen-card label {
      display: flex;
      align-items: flex-start;
      gap: 10px;
      cursor: pointer;
    }
    .gen-cb {
      margin-top: 3px;
      width: 18px;
      height: 18px;
      accent-color: var(--green);
      flex-shrink: 0;
      cursor: pointer;
    }
    .gen-card .gc-pt { font-size: 17px; font-weight: 700; margin-bottom: 2px; }
    .gen-card .gc-en { font-size: 14px; color: var(--muted); margin-bottom: 2px; }
    .gen-card .gc-notes { font-size: 13px; color: #64748b; line-height: 1.5; margin-top: 4px; font-style: italic; }
    .gen-count {
      font-size: 13px;
      font-weight: 700;
      color: var(--green);
      background: var(--green-light);
      padding: 3px 10px;
      border-radius: 99px;
    }
    .gen-tabs { display: flex; gap: 6px; margin-bottom: 14px; }
    .gen-tab {
      flex: 1; text-align: center; padding: 9px 6px; border-radius: 10px;
      border: 1.5px solid var(--border); font-size: 13px; font-weight: 600;
      cursor: pointer; background: white; color: var(--muted); transition: all .15s;
      font-family: inherit;
    }
    .gen-tab.active { background: var(--green); color: white; border-color: var(--green); }
    .gen-mode { display: none; }
    .gen-mode.active { display: block; }
    .gc-example {
      background: #f0fdf4; border-left: 3px solid var(--green-mid);
      border-radius: 0 6px 6px 0; padding: 8px 12px; margin-top: 8px;
      font-size: 14px; color: var(--green-dark); font-weight: 500;
      display: flex; align-items: center; gap: 8px;
    }
    .gc-example .gc-sent { flex: 1; }
    .gc-regen {
      background: none; border: 1.5px solid var(--border); border-radius: 6px;
      font-size: 11px; padding: 3px 8px; cursor: pointer; color: var(--muted);
      font-family: inherit; font-weight: 500; flex-shrink: 0; transition: all .15s;
    }
    .gc-regen:hover { border-color: var(--green-mid); color: var(--green); }
    .gc-regen:disabled { opacity: .5; cursor: not-allowed; }
  </style>
</head>
<body>

<header>
  <span class="flag">🇵🇹</span>
  <h1>PT Practice</h1>
</header>

<main>

  <!-- ── Translate Panel ── -->
  <div id="panel-translate" class="panel active">
    <p class="panel-title">Translate</p>
    <p class="panel-sub">Type English and get European Portuguese.</p>
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
        <button class="copy-btn" onclick="copyText('translateOutput', this)">Copy</button>
      </div>
      <div class="result-card english">
        <div class="result-label">Your original</div>
        <div class="result-text muted" id="translateOriginal"></div>
      </div>
      <div class="logged-chip" id="translateLogged">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M20 6L9 17l-5-5"/></svg>
        Logged
      </div>
    </div>
  </div>

  <!-- ── Generate Flashcards Panel ── -->
  <div id="panel-generate" class="panel">
    <p class="panel-title">Generate</p>
    <p class="panel-sub">Create flashcards from a word, concept, or lesson notes.</p>

    <!-- Sub-mode tabs -->
    <div class="gen-tabs">
      <button type="button" class="gen-tab active" onclick="setGenMode('quick')">Quick lookup</button>
      <button type="button" class="gen-tab" onclick="setGenMode('notes')">Lesson notes</button>
    </div>

    <!-- Quick lookup mode -->
    <div id="genMode-quick" class="gen-mode active">
      <div class="card">
        <label class="field-label" for="genInput">What do you want to learn?</label>
        <textarea id="genInput" placeholder="e.g. saudade, to run, how to express speculative desire in Portuguese…" rows="2"></textarea>
        <button class="btn btn-primary" id="genBtn" onclick="doGenerate()">
          <span class="btn-label">Generate flashcards</span>
          <div class="spinner"></div>
        </button>
        <div class="error-msg" id="genError"></div>
      </div>
    </div>

    <!-- Lesson notes mode -->
    <div id="genMode-notes" class="gen-mode">
      <div class="card">
        <label class="field-label" for="notesInput">Paste your lesson notes</label>
        <textarea id="notesInput" placeholder="Paste vocabulary, sentences, corrections from your lesson…" rows="6"></textarea>
        <button class="btn btn-primary" id="notesBtn" onclick="doParseNotes()">
          <span class="btn-label">Generate flashcards from notes</span>
          <div class="spinner"></div>
        </button>
        <div class="error-msg" id="notesError"></div>
      </div>
    </div>

    <!-- Shared results area -->
    <div id="genResults" style="display:none;">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px;">
        <span class="field-label" style="margin:0;">Suggested flashcards</span>
        <span class="gen-count" id="genCount">0 selected</span>
      </div>
      <div id="genCards"></div>
      <button class="btn btn-primary" id="saveGenBtn" onclick="saveGenCards()" disabled style="width:100%;justify-content:center;margin-top:8px;">
        <span class="btn-label">Save to flashcards</span>
        <div class="spinner"></div>
      </button>
      <div id="genSaved" style="display:none;margin-top:10px;text-align:center;font-size:14px;font-weight:600;color:var(--green);">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" style="vertical-align:-2px"><path d="M20 6L9 17l-5-5"/></svg>
        <span id="genSavedText"></span>
      </div>
    </div>
  </div>

  <!-- ── Check Panel ── -->
  <div id="panel-check" class="panel">
    <p class="panel-title">Check My Portuguese</p>
    <p class="panel-sub">Write a sentence and get instant feedback.</p>
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
        Logged
      </div>
    </div>
  </div>

  <!-- ── Explain Panel ── -->
  <div id="panel-explain" class="panel">
    <p class="panel-title">Explain</p>
    <p class="panel-sub">Describe something you don't understand and get an explanation with flashcards.</p>
    <div class="card">
      <label class="field-label" for="explainInput">What do you want to understand?</label>
      <textarea id="explainInput" placeholder="e.g. When do I use ser vs estar? What's the difference between por and para? How does the subjunctive work?" rows="3"></textarea>
      <button class="btn btn-primary" id="explainBtn" onclick="doExplain()">
        <span class="btn-label">Explain</span>
        <div class="spinner"></div>
      </button>
      <div class="error-msg" id="explainError"></div>
    </div>
    <div id="explainResult" style="display:none;">
      <div class="card" style="border-left:3px solid var(--green-mid);">
        <div class="field-label" style="margin-bottom:6px;">Explanation</div>
        <div id="explainText" style="font-size:15px;line-height:1.6;color:var(--text);"></div>
      </div>
      <div style="display:flex;align-items:center;justify-content:space-between;margin:14px 0 10px;">
        <span class="field-label" style="margin:0;">Suggested flashcards</span>
        <span class="gen-count" id="explainCount">0 selected</span>
      </div>
      <div id="explainCards"></div>
      <button class="btn btn-primary" id="saveExplainBtn" onclick="saveExplainCards()" disabled style="width:100%;justify-content:center;margin-top:8px;">
        <span class="btn-label">Save to flashcards</span>
        <div class="spinner"></div>
      </button>
      <div id="explainSaved" style="display:none;margin-top:10px;text-align:center;font-size:14px;font-weight:600;color:var(--green);">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" style="vertical-align:-2px"><path d="M20 6L9 17l-5-5"/></svg>
        <span id="explainSavedText"></span>
      </div>
    </div>
  </div>

</main>

<!-- ── Bottom Navigation ── -->
<nav class="bottom-nav">
  <button class="nav-item active" onclick="switchTab('translate', this)">
    <span class="nav-icon">🔄</span>
    <span>Translate</span>
  </button>
  <button class="nav-item" onclick="switchTab('check', this)">
    <span class="nav-icon">✓</span>
    <span>Check</span>
  </button>
  <button class="nav-item" onclick="switchTab('generate', this)">
    <span class="nav-icon">⚡</span>
    <span>Generate</span>
  </button>
  <button class="nav-item" onclick="switchTab('explain', this)">
    <span class="nav-icon">💡</span>
    <span>Explain</span>
  </button>
  <a href="/practice/" class="nav-item">
    <span class="nav-icon">📝</span>
    <span>Practice</span>
  </a>
  <a href="/flashcards" class="nav-item">
    <span class="nav-icon">📚</span>
    <span>Cards</span>
  </a>
</nav>

<script>
  function switchTab(name, el) {
    document.querySelectorAll('.nav-item').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
    el.classList.add('active');
    document.getElementById('panel-' + name).classList.add('active');
  }

  // Open correct tab if arriving via ?tab=check etc.
  (function() {
    const tab = new URLSearchParams(location.search).get('tab');
    if (tab && document.getElementById('panel-' + tab)) {
      const navBtns = document.querySelectorAll('button.nav-item');
      const map = {translate: 0, check: 1, generate: 2, explain: 3};
      if (map[tab] !== undefined && navBtns[map[tab]]) {
        switchTab(tab, navBtns[map[tab]]);
      }
    }
  })();

  async function copyText(elId, btn) {
    const text = document.getElementById(elId).textContent.trim();
    await navigator.clipboard.writeText(text).catch(() => {});
    btn.textContent = 'Copied!';
    btn.classList.add('copied');
    setTimeout(() => { btn.textContent = 'Copy'; btn.classList.remove('copied'); }, 2000);
  }

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
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({text: input}),
      });
      const data = await resp.json();
      if (data.error) throw new Error(data.error);
      document.getElementById('translateOutput').textContent = data.portuguese;
      document.getElementById('translateOriginal').textContent = data.english;
      resultEl.classList.add('show');
      document.getElementById('translateLogged').classList.add('show');
    } catch (e) {
      showError(errEl, e.message, doTranslate);
    } finally {
      setLoading(btn, false);
    }
  }

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
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({text: input}),
      });
      const data = await resp.json();
      if (data.error) throw new Error(data.error);
      const badge = document.getElementById('statusBadge');
      if (data.is_correct) {
        badge.className = 'status-badge ok';
        badge.innerHTML = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><path d="M20 6L9 17l-5-5"/></svg> Correct!';
      } else {
        badge.className = 'status-badge err';
        badge.innerHTML = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><path d="M18 6L6 18M6 6l12 12"/></svg> Needs correction';
      }
      const correctCard = document.getElementById('correctCard');
      document.getElementById('correctLabel').textContent = data.is_correct ? 'Your sentence' : 'Correct version';
      correctCard.className = 'result-card ' + (data.is_correct ? 'correct' : 'incorrect');
      document.getElementById('correctOutput').textContent = data.correct_portuguese;
      const expCard = document.getElementById('explanationCard');
      if (data.is_correct || !data.explanation) {
        expCard.style.display = 'none';
      } else {
        expCard.style.display = 'block';
        document.getElementById('explanationOutput').textContent = data.explanation;
      }
      document.getElementById('englishOutput').textContent = data.english_translation;
      resultEl.classList.add('show');
      document.getElementById('checkLogged').classList.add('show');
    } catch (e) {
      showError(errEl, e.message, doCheck);
    } finally {
      setLoading(btn, false);
    }
  }

  // ── Generate flashcards ──
  let genCardsData = [];

  function setGenMode(mode) {
    document.querySelectorAll('.gen-tab').forEach((t, i) =>
      t.classList.toggle('active', (i === 0 ? 'quick' : 'notes') === mode));
    document.querySelectorAll('.gen-mode').forEach(m => m.classList.remove('active'));
    document.getElementById('genMode-' + mode).classList.add('active');
    document.getElementById('genResults').style.display = 'none';
  }

  function renderGenCards(cards) {
    genCardsData = cards;
    let html = '';
    cards.forEach((c, i) => {
      const hasSent = c.example_sentence;
      html += `<div class="gen-card" id="gc-${i}"><label>
        <input type="checkbox" class="gen-cb" data-idx="${i}" checked onchange="updateGenCount()">
        <div style="flex:1">
          <div class="gc-pt">${esc(c.portuguese)}</div>
          <div class="gc-en">${esc(c.english)}</div>
          ${c.notes ? '<div class="gc-notes">' + esc(c.notes) + '</div>' : ''}
        </div>
      </label>`;
      if (hasSent) {
        html += `<div class="gc-example" id="gc-ex-${i}">
          <span class="gc-sent">${esc(c.example_sentence)}</span>
          <button class="gc-regen" onclick="regenSentence(${i})">↻</button>
        </div>`;
      }
      html += `</div>`;
    });
    document.getElementById('genCards').innerHTML = html;
    document.getElementById('genResults').style.display = 'block';
    document.getElementById('genSaved').style.display = 'none';
    updateGenCount();
  }

  async function doGenerate() {
    const input = document.getElementById('genInput').value.trim();
    if (!input) return;
    const btn = document.getElementById('genBtn');
    const errEl = document.getElementById('genError');
    setLoading(btn, true);
    errEl.classList.remove('show');
    document.getElementById('genResults').style.display = 'none';
    try {
      const resp = await fetch('/api/suggest-flashcards', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({input}),
      });
      const data = await resp.json();
      if (data.error) throw new Error(data.error);
      renderGenCards(data.cards);
    } catch (e) {
      showError(errEl, e.message, doGenerate);
    } finally {
      setLoading(btn, false);
    }
  }

  async function doParseNotes() {
    const notes = document.getElementById('notesInput').value.trim();
    if (!notes) return;
    const btn = document.getElementById('notesBtn');
    const errEl = document.getElementById('notesError');
    setLoading(btn, true);
    errEl.classList.remove('show');
    document.getElementById('genResults').style.display = 'none';
    try {
      const resp = await fetch('/api/parse-notes', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({notes}),
      });
      const data = await resp.json();
      if (data.error) throw new Error(data.error);
      renderGenCards(data.cards);
    } catch (e) {
      showError(errEl, e.message, doParseNotes);
    } finally {
      setLoading(btn, false);
    }
  }

  async function regenSentence(idx) {
    const card = genCardsData[idx];
    if (!card) return;
    const exEl = document.getElementById('gc-ex-' + idx);
    const btn = exEl.querySelector('.gc-regen');
    btn.disabled = true;
    btn.textContent = '…';
    try {
      const resp = await fetch('/api/regen-sentence', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({portuguese: card.portuguese}),
      });
      const data = await resp.json();
      if (data.error) throw new Error(data.error);
      card.example_sentence = data.sentence;
      exEl.querySelector('.gc-sent').textContent = data.sentence;
    } catch(e) {
      // silently keep old sentence
    } finally {
      btn.disabled = false;
      btn.textContent = '↻';
    }
  }

  function esc(s) {
    const d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML;
  }

  function updateGenCount() {
    const checked = document.querySelectorAll('.gen-cb:checked').length;
    document.getElementById('genCount').textContent = checked + ' selected';
    document.getElementById('saveGenBtn').disabled = checked === 0;
  }

  async function saveGenCards() {
    const checked = [...document.querySelectorAll('.gen-cb:checked')];
    if (!checked.length) return;
    const items = checked.map(cb => {
      const c = genCardsData[cb.dataset.idx];
      return {
        english: c.english,
        portuguese: c.portuguese,
        notes: [c.notes, c.example_sentence].filter(Boolean).join(' — '),
      };
    });
    const btn = document.getElementById('saveGenBtn');
    setLoading(btn, true);
    try {
      const resp = await fetch('/api/save-flashcards', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({items}),
      });
      const data = await resp.json();
      if (data.error) throw new Error(data.error);
      document.getElementById('genSavedText').textContent = data.count + ' card' + (data.count === 1 ? '' : 's') + ' saved!';
      document.getElementById('genSaved').style.display = 'block';
      checked.forEach(cb => {
        cb.checked = false;
        cb.disabled = true;
        cb.closest('.gen-card').style.opacity = '.45';
      });
      updateGenCount();
    } catch (e) {
      showError(document.getElementById('genError'), e.message, saveGenCards);
    } finally {
      setLoading(btn, false);
    }
  }

  function showError(el, msg, retryFn) {
    el.textContent = msg || 'Something went wrong.';
    const btn = document.createElement('button');
    btn.textContent = ' Retry';
    btn.style.cssText = 'background:none;border:none;color:var(--green);font-weight:700;font-size:inherit;cursor:pointer;text-decoration:underline;font-family:inherit;padding:0;margin-left:4px;';
    btn.onclick = function() { el.classList.remove('show'); retryFn(); };
    el.appendChild(btn);
    el.classList.add('show');
  }

  function setLoading(btn, on) {
    btn.disabled = on;
    btn.classList.toggle('loading', on);
  }

  // ── Explain tab ──
  let explainCardsData = [];

  async function doExplain() {
    const input = document.getElementById('explainInput').value.trim();
    if (!input) return;
    const btn = document.getElementById('explainBtn');
    const errEl = document.getElementById('explainError');
    setLoading(btn, true);
    errEl.classList.remove('show');
    document.getElementById('explainResult').style.display = 'none';
    try {
      const resp = await fetch('/api/explain', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({question: input}),
      });
      const data = await resp.json();
      if (data.error) throw new Error(data.error);
      document.getElementById('explainText').textContent = data.explanation;
      explainCardsData = data.cards || [];
      renderExplainCards(explainCardsData);
      document.getElementById('explainResult').style.display = 'block';
    } catch (e) {
      showError(errEl, e.message, doExplain);
    } finally {
      setLoading(btn, false);
    }
  }

  function renderExplainCards(cards) {
    let html = '';
    cards.forEach(function(c, i) {
      var hasSent = c.example_sentence;
      html += '<div class="gen-card" id="ec-' + i + '"><label>';
      html += '<input type="checkbox" class="explain-cb" data-idx="' + i + '" checked onchange="updateExplainCount()">';
      html += '<div style="flex:1">';
      html += '<div class="gc-pt">' + esc(c.portuguese) + '</div>';
      html += '<div class="gc-en">' + esc(c.english) + '</div>';
      if (c.notes) html += '<div class="gc-notes">' + esc(c.notes) + '</div>';
      html += '</div></label>';
      if (hasSent) {
        html += '<div class="gc-example" id="ec-ex-' + i + '">';
        html += '<span class="gc-sent">' + esc(c.example_sentence) + '</span>';
        html += '<button class="gc-regen" data-eidx="' + i + '">&#8635;</button>';
        html += '</div>';
      }
      html += '</div>';
    });
    document.getElementById('explainCards').innerHTML = html;
    document.getElementById('explainSaved').style.display = 'none';
    updateExplainCount();
  }

  document.addEventListener('click', function(e) {
    var btn = e.target.closest('.gc-regen[data-eidx]');
    if (!btn) return;
    var idx = Number(btn.dataset.eidx);
    var card = explainCardsData[idx];
    if (!card) return;
    var exEl = document.getElementById('ec-ex-' + idx);
    btn.disabled = true;
    btn.textContent = '\u2026';
    fetch('/api/regen-sentence', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({portuguese: card.portuguese}),
    }).then(function(r) { return r.json(); }).then(function(data) {
      if (data.sentence) {
        card.example_sentence = data.sentence;
        exEl.querySelector('.gc-sent').textContent = data.sentence;
      }
    }).finally(function() { btn.disabled = false; btn.textContent = '\u21bb'; });
  });

  function updateExplainCount() {
    var checked = document.querySelectorAll('.explain-cb:checked').length;
    document.getElementById('explainCount').textContent = checked + ' selected';
    document.getElementById('saveExplainBtn').disabled = checked === 0;
  }

  async function saveExplainCards() {
    var checked = [...document.querySelectorAll('.explain-cb:checked')];
    if (!checked.length) return;
    var items = checked.map(function(cb) {
      var c = explainCardsData[cb.dataset.idx];
      return {
        english: c.english,
        portuguese: c.portuguese,
        notes: [c.notes, c.example_sentence].filter(Boolean).join(' \u2014 '),
      };
    });
    var btn = document.getElementById('saveExplainBtn');
    setLoading(btn, true);
    try {
      var resp = await fetch('/api/save-flashcards', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({items: items}),
      });
      var data = await resp.json();
      if (data.error) throw new Error(data.error);
      document.getElementById('explainSavedText').textContent = data.count + ' card' + (data.count === 1 ? '' : 's') + ' saved!';
      document.getElementById('explainSaved').style.display = 'block';
      checked.forEach(function(cb) {
        cb.checked = false;
        cb.disabled = true;
        cb.closest('.gen-card').style.opacity = '.45';
      });
      updateExplainCount();
    } catch (e) {
      showError(document.getElementById('explainError'), e.message, saveExplainCards);
    } finally {
      setLoading(btn, false);
    }
  }

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
  <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="theme-color" content="#166534">
  <title>PT Flashcards</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    :root {
      --green-dark: #14532d; --green: #166534; --green-mid: #16a34a;
      --green-light: #dcfce7; --red-light: #fee2e2; --amber-light: #fef9c3;
      --bg: #f1f5f9; --surface: #fff; --border: #e2e8f0;
      --text: #0f172a; --muted: #64748b;
      --shadow: 0 1px 3px rgba(0,0,0,.07), 0 4px 16px rgba(0,0,0,.06);
    }
    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: var(--bg); color: var(--text); min-height: 100vh; padding-bottom: calc(68px + env(safe-area-inset-bottom)); }

    /* ── Header ── */
    header { background: var(--green); padding: 0 20px; padding-top: env(safe-area-inset-top); display: flex; align-items: center; height: calc(56px + env(safe-area-inset-top)); gap: 10px; position: sticky; top: 0; z-index: 50; }
    header h1 { color: white; font-size: 18px; font-weight: 800; }""" + _BOTTOM_NAV_CSS + """

    /* ── Sticky toolbar ── */
    .toolbar-wrap {
      position: sticky;
      top: calc(56px + env(safe-area-inset-top));
      background: var(--bg);
      z-index: 40;
      padding: 12px 16px 8px;
      border-bottom: 1px solid var(--border);
    }
    .toolbar { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
    .btn { height: 40px; padding: 0 18px; border: none; border-radius: 10px; font-family: inherit; font-size: 14px; font-weight: 600; cursor: pointer; display: inline-flex; align-items: center; gap: 7px; transition: background .15s, opacity .15s; -webkit-tap-highlight-color: transparent; }
    .btn-primary { background: var(--green); color: white; }
    .btn-primary:hover { background: var(--green-dark); }
    .btn-primary:disabled { opacity: .4; cursor: not-allowed; }
    .btn-outline { background: white; color: var(--text); border: 1.5px solid var(--border); }
    .btn-outline:hover { background: var(--bg); }
    .count-chip { background: var(--green-light); color: var(--green-dark); font-size: 13px; font-weight: 700; padding: 5px 12px; border-radius: 99px; margin-left: auto; white-space: nowrap; }

    /* ── Main ── */
    main { padding: 16px; max-width: 700px; margin: 0 auto; }
    .page-title { font-size: 20px; font-weight: 800; margin-bottom: 4px; }
    .page-sub { font-size: 14px; color: var(--muted); margin-bottom: 16px; line-height: 1.5; }

    /* ── Entry cards ── */
    .entry-list { display: flex; flex-direction: column; gap: 10px; }
    .entry-card {
      background: white;
      border-radius: 14px;
      box-shadow: var(--shadow);
      display: flex;
      align-items: stretch;
      border: 1.5px solid transparent;
      transition: border-color .15s, background .15s;
      overflow: hidden;
    }
    .entry-card:has(.cb:checked) { border-color: var(--green-mid); background: #f0fdf4; }

    .entry-select {
      display: flex;
      align-items: center;
      padding: 0 4px 0 14px;
      cursor: pointer;
      flex-shrink: 0;
    }
    .cb { width: 20px; height: 20px; cursor: pointer; accent-color: var(--green); flex-shrink: 0; }

    .entry-body { flex: 1; padding: 14px 12px; min-width: 0; }
    .entry-meta { display: flex; align-items: center; gap: 6px; margin-bottom: 8px; }
    .entry-date { font-size: 11px; color: var(--muted); margin-left: auto; }

    .entry-pt { font-size: 19px; font-weight: 700; color: var(--green-dark); line-height: 1.3; margin-bottom: 4px; }
    .entry-en { font-size: 14px; color: var(--text); margin-bottom: 6px; line-height: 1.4; }
    .entry-wrong { font-size: 13px; color: var(--muted); margin-top: 4px; }
    .entry-wrong em { font-style: normal; color: #dc2626; }
    .entry-expl { font-size: 12px; color: var(--muted); margin-top: 3px; line-height: 1.4; border-top: 1px solid var(--border); padding-top: 4px; margin-top: 6px; }

    .type-badge { display: inline-block; font-size: 11px; font-weight: 700; padding: 2px 9px; border-radius: 99px; }
    .type-badge.translation { background: #dbeafe; color: #1d4ed8; }
    .type-badge.correction  { background: var(--red-light); color: #dc2626; }
    .type-badge.practice    { background: var(--amber-light); color: #92400e; }

    .delete-btn { background: none; border: none; cursor: pointer; color: var(--muted); padding: 6px 14px 6px 6px; line-height: 1; transition: color .15s; flex-shrink: 0; align-self: flex-start; margin-top: 10px; -webkit-tap-highlight-color: transparent; }
    .delete-btn:hover { color: #dc2626; }

    /* ── Example sentence ── */
    .entry-sentence { margin-top: 8px; padding-top: 8px; border-top: 1px solid var(--border); }
    .sent-text { font-size: 14px; color: var(--green-dark); font-style: italic; line-height: 1.45; margin-bottom: 6px; min-height: 0; }
    .sent-text:empty { display: none; }
    .sent-actions { display: flex; gap: 8px; }
    .sent-regen-btn { background: none; border: 1px solid var(--border); border-radius: 6px; font-size: 12px; font-weight: 500; color: var(--muted); cursor: pointer; padding: 4px 10px; font-family: inherit; transition: border-color .15s, color .15s; }
    .sent-regen-btn:hover { border-color: var(--green-mid); color: var(--green); }
    .sent-regen-btn:disabled { opacity: .5; cursor: not-allowed; }
    .word-picker { margin-top: 8px; }
    .word-picker-label { font-size: 11px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: .05em; display: block; margin-bottom: 6px; }
    .word-chips { display: flex; flex-wrap: wrap; gap: 6px; }
    .word-chip { background: white; border: 1.5px solid var(--border); border-radius: 8px; padding: 5px 12px; font-size: 14px; font-weight: 600; color: var(--text); cursor: pointer; font-family: inherit; transition: all .15s; }
    .word-chip:hover { border-color: var(--green-mid); color: var(--green); background: var(--green-light); }
    .word-chip:disabled { opacity: .5; cursor: not-allowed; }
    .word-chip.loading { background: var(--green-light); border-color: var(--green-mid); color: var(--green); }
    .word-chip.full { background: var(--green-light); border-color: var(--green-mid); color: var(--green-dark); font-size: 12px; }

    /* ── Empty state ── */
    .empty { text-align: center; padding: 60px 20px; color: var(--muted); }
    .empty-icon { font-size: 44px; margin-bottom: 14px; display: block; }
    .empty p { font-size: 15px; line-height: 1.6; }

    /* ── Error banner ── */
    .error-banner { background: var(--red-light); border: 1.5px solid #fca5a5; border-radius: 12px; padding: 14px 16px; margin-bottom: 16px; font-size: 14px; color: #dc2626; }

    /* ── Modal ── */
    .modal-overlay { display: none; position: fixed; inset: 0; background: rgba(0,0,0,.45); z-index: 100; align-items: flex-end; justify-content: center; padding: 0; }
    .modal-overlay.show { display: flex; }
    .modal { background: white; border-radius: 20px 20px 0 0; padding: 28px 24px calc(28px + env(safe-area-inset-bottom)); width: 100%; max-width: 480px; box-shadow: 0 -8px 40px rgba(0,0,0,.15); }
    .modal h3 { font-size: 18px; font-weight: 700; margin-bottom: 8px; }
    .modal p { font-size: 14px; color: var(--muted); margin-bottom: 22px; line-height: 1.6; }
    .modal-actions { display: flex; gap: 10px; }
    .btn-danger { background: #dc2626; color: white; flex: 1; }
    .btn-danger:hover { background: #b91c1c; }

    /* ── Toast ── */
    .spinner { display: inline-block; width: 15px; height: 15px; border: 2px solid rgba(255,255,255,.35); border-top-color: white; border-radius: 50%; animation: spin .6s linear infinite; }
    @keyframes spin { to { transform: rotate(360deg); } }
    .toast { display: none; position: fixed; bottom: 28px; left: 50%; transform: translateX(-50%); padding: 14px 22px; border-radius: 12px; font-size: 14px; font-weight: 600; box-shadow: 0 8px 30px rgba(0,0,0,.2); z-index: 200; white-space: nowrap; }
    .toast.success { background: var(--green); color: white; }
    .toast.error   { background: #dc2626; color: white; }
    .toast.show { display: block; animation: slideUp .25s ease; }
    @keyframes slideUp { from { transform: translateX(-50%) translateY(12px); opacity: 0; } to { transform: translateX(-50%) translateY(0); opacity: 1; } }
  </style>
</head>
<body>

<header>
  <span style="font-size:22px">🇵🇹</span>
  <h1>Flashcards</h1>
</header>

{% if mistakes %}
<div class="toolbar-wrap">
  <div class="toolbar">
    <button class="btn btn-outline" onclick="selectAll()">Select all</button>
    <button class="btn btn-outline" onclick="selectNone()">None</button>
    <span class="count-chip" id="countChip">0 selected</span>
    <button class="btn btn-primary" id="generateBtn" onclick="generate()" disabled>
      <span class="btn-label">Generate &amp; Email</span>
      <span class="spinner" style="display:none"></span>
    </button>
  </div>
</div>
{% endif %}

<main>
  <p class="page-title">Your mistakes</p>
  <p class="page-sub">Select entries to generate Anki flashcards with audio, emailed to you.</p>

  {% if error %}
  <div class="error-banner">⚠️ Could not load entries: {{ error }}</div>
  {% endif %}

  {% if mistakes %}
  <div class="entry-list">
    {% for m in mistakes %}
    <div class="entry-card" id="entry-{{ m.id }}">
      <label class="entry-select" title="Select">
        <input type="checkbox" class="cb" onchange="updateCount()"
          data-id="{{ m.id }}"
          data-english="{{ m.english }}"
          data-portuguese="{{ m.portuguese }}"
          data-original="{{ m.original }}"
        >
      </label>
      <div class="entry-body">
        <div class="entry-meta">
          {% if m.type == 'Translation' %}
            <span class="type-badge translation">Translate</span>
          {% elif m.type == 'Practice' %}
            <span class="type-badge practice">Practice</span>
          {% else %}
            <span class="type-badge correction">Mistake</span>
          {% endif %}
          <span class="entry-date">{{ m.timestamp[:10] if m.timestamp else '' }}</span>
        </div>
        <div class="entry-pt">{{ m.portuguese }}</div>
        <div class="entry-en">{{ m.english }}</div>
        {% if m.original %}
        <div class="entry-wrong">You wrote: <em>{{ m.original }}</em></div>
        {% endif %}
        {% if m.explanation %}
        <div class="entry-expl">{{ m.explanation }}</div>
        {% endif %}
        <div class="entry-sentence" id="sent-{{ m.id }}">
          <div class="sent-text" id="sent-text-{{ m.id }}"></div>
          <div class="sent-actions">
            <button class="sent-regen-btn" data-entry-id="{{ m.id }}" data-phrase="{{ m.portuguese | e }}" title="Generate example sentence">↻ Example sentence</button>
          </div>
          <div class="word-picker" id="words-{{ m.id }}" style="display:none;">
            <span class="word-picker-label">Generate sentence using:</span>
            <div class="word-chips" id="chips-{{ m.id }}"></div>
          </div>
        </div>
      </div>
      <button class="delete-btn" onclick="deleteEntry('{{ m.id }}', this)" title="Delete">
        <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14H6L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/><path d="M9 6V4h6v2"/></svg>
      </button>
    </div>
    {% endfor %}
  </div>

  {% else %}
  <div class="empty">
    <span class="empty-icon">🎉</span>
    <p>No mistakes logged yet.<br>Use the Check tab or Practice Test to add entries.</p>
  </div>
  {% endif %}
</main>

<div class="toast" id="toast"></div>

<!-- Delete confirmation sheet -->
<div class="modal-overlay" id="deleteModal">
  <div class="modal">
    <h3>Delete from log?</h3>
    <p>Remove <strong id="deleteCount">0</strong> entries from your practice log?
    <br><small style="color:#16a34a">✓ They'll stay in Google Sheets.</small></p>
    <div class="modal-actions">
      <button class="btn btn-danger" onclick="confirmDelete()">Yes, delete</button>
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
    const card = btn.closest('.entry-card');
    card.style.opacity = '0.4';
    try {
      const resp = await fetch('/api/log/' + id, { method: 'DELETE' });
      const data = await resp.json();
      if (data.error) throw new Error(data.error);
      card.remove();
      updateCount();
      showToast('Entry deleted', 'success');
    } catch (e) {
      card.style.opacity = '1';
      showToast(e.message || 'Delete failed', 'error');
    }
  }

  async function generate() {
    const checked = [...document.querySelectorAll('.cb:checked')];
    if (!checked.length) return;
    const cards = checked.map(c => ({
      english: c.dataset.english, portuguese: c.dataset.portuguese, original: c.dataset.original,
    }));
    const btn = document.getElementById('generateBtn');
    const label = btn.querySelector('.btn-label');
    const spinner = btn.querySelector('.spinner');
    btn.disabled = true;
    label.style.display = 'none';
    spinner.style.display = 'inline-block';
    try {
      const resp = await fetch('/api/generate-flashcards', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({cards}),
      });
      const data = await resp.json();
      if (data.error) throw new Error(data.error);
      showToast(data.message, 'success');
      pendingDeleteIds = checked.map(c => c.dataset.id).filter(Boolean);
      document.getElementById('deleteCount').textContent = pendingDeleteIds.length;
      setTimeout(() => document.getElementById('deleteModal').classList.add('show'), 600);
    } catch (e) {
      showToast(e.message || 'Something went wrong', 'error');
    } finally {
      btn.disabled = false;
      label.style.display = '';
      spinner.style.display = 'none';
      updateCount();
    }
  }

  async function confirmDelete() {
    document.getElementById('deleteModal').classList.remove('show');
    for (const id of pendingDeleteIds) {
      try { await fetch('/api/log/' + id, { method: 'DELETE' }); } catch (_) {}
      const card = document.getElementById('entry-' + id);
      if (card) card.remove();
    }
    pendingDeleteIds = [];
    updateCount();
    showToast('Entries deleted', 'success');
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

  document.addEventListener('click', function(e) {
    var btn = e.target.closest('.sent-regen-btn');
    if (btn) showWordPicker(Number(btn.dataset.entryId), btn.dataset.phrase);
  });

  function showWordPicker(id, phrase) {
    var picker = document.getElementById('words-' + id);
    if (picker.style.display === 'block') {
      picker.style.display = 'none';
      return;
    }
    var chips = document.getElementById('chips-' + id);
    var words = phrase.split(/\s+/).filter(function(w) { return w.length > 0; });
    chips.innerHTML = '';
    // "Full phrase" chip
    var fullBtn = document.createElement('button');
    fullBtn.className = 'word-chip full';
    fullBtn.textContent = 'Full phrase';
    fullBtn.dataset.entryId = id;
    fullBtn.dataset.phrase = phrase;
    fullBtn.onclick = function() { regenSentence(id, phrase); };
    chips.appendChild(fullBtn);
    // Individual word chips (only if multi-word)
    if (words.length > 1) {
      words.forEach(function(w) {
        var btn = document.createElement('button');
        btn.className = 'word-chip';
        btn.textContent = w;
        btn.dataset.entryId = id;
        btn.dataset.phrase = w;
        btn.onclick = function() { regenSentence(id, w); };
        chips.appendChild(btn);
      });
    }
    picker.style.display = 'block';
  }

  async function regenSentence(id, phrase) {
    var sentText = document.getElementById('sent-text-' + id);
    var picker = document.getElementById('words-' + id);
    picker.querySelectorAll('.word-chip').forEach(function(c) { c.disabled = true; });
    sentText.textContent = 'Generating\u2026';
    sentText.style.display = 'block';
    try {
      var resp = await fetch('/api/regen-sentence', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({portuguese: phrase}),
      });
      var data = await resp.json();
      if (data.error) throw new Error(data.error);
      sentText.textContent = data.sentence;
    } catch(e) {
      sentText.textContent = 'Failed \u2014 tap to retry';
    } finally {
      picker.querySelectorAll('.word-chip').forEach(function(c) { c.disabled = false; });
    }
  }
</script>
""" + _bottom_nav_html('cards') + """
</body>
</html>
"""

_PRACTICE_CSS = """
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    :root {
      --green-dark:#14532d; --green:#166534; --green-mid:#16a34a;
      --green-light:#dcfce7; --red-light:#fee2e2; --amber-light:#fef9c3;
      --bg:#f1f5f9; --surface:#fff; --border:#e2e8f0; --text:#0f172a; --muted:#64748b;
      --radius:14px; --shadow:0 1px 3px rgba(0,0,0,.07),0 6px 20px rgba(0,0,0,.06);
    }
    body { font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif; background:var(--bg); color:var(--text); min-height:100vh; padding-bottom:calc(68px + env(safe-area-inset-bottom)); }
    header { background:var(--green); padding:0 20px; padding-top:env(safe-area-inset-top); display:flex; align-items:center; height:calc(56px + env(safe-area-inset-top)); gap:10px; }
    header h1 { color:white; font-size:17px; font-weight:700; }
    .back { color:rgba(255,255,255,.85); font-size:13px; font-weight:600; text-decoration:none; background:rgba(255,255,255,.15); padding:5px 12px; border-radius:99px; margin-left:auto; }
    main { padding:20px 16px 20px; max-width:640px; margin:0 auto; }""" + _BOTTOM_NAV_CSS + """
    .card { background:var(--surface); border-radius:var(--radius); box-shadow:var(--shadow); padding:20px; margin-bottom:16px; }
    .card h2 { font-size:18px; font-weight:700; margin-bottom:6px; }
    .card p { font-size:14px; color:var(--muted); margin-bottom:16px; line-height:1.6; }
    label.field-label { display:block; font-size:11px; font-weight:700; text-transform:uppercase; letter-spacing:.07em; color:var(--muted); margin-bottom:8px; }
    textarea { width:100%; min-height:100px; padding:12px 14px; border:1.5px solid var(--border); border-radius:10px; font-family:inherit; font-size:16px; color:var(--text); background:#fafafa; resize:vertical; outline:none; line-height:1.5; transition:border-color .15s,box-shadow .15s; }
    textarea:focus { background:white; border-color:var(--green-mid); box-shadow:0 0 0 3px rgba(22,163,74,.12); }
    .btn { height:50px; padding:0 22px; border:none; border-radius:10px; font-family:inherit; font-size:16px; font-weight:700; cursor:pointer; display:inline-flex; align-items:center; gap:8px; transition:background .15s,transform .1s; text-decoration:none; }
    .btn-primary { background:var(--green); color:white; }
    .btn-primary:hover { background:var(--green-dark); }
    .btn-primary:disabled { opacity:.5; cursor:not-allowed; }
    .btn-outline { background:white; color:var(--text); border:1.5px solid var(--border); height:42px; font-size:14px; }
    .btn-outline:hover { border-color:var(--green-mid); color:var(--green); }
    .btn-sm { height:36px; font-size:13px; padding:0 16px; }
    .btn-block { width:100%; justify-content:center; margin-top:12px; }
    /* Progress */
    .progress-wrap { margin-bottom:16px; }
    .progress-meta { display:flex; justify-content:space-between; font-size:12px; font-weight:600; color:var(--muted); margin-bottom:6px; }
    .progress-bar { height:6px; background:var(--border); border-radius:99px; overflow:hidden; }
    .progress-fill { height:100%; background:var(--green-mid); border-radius:99px; transition:width .4s ease; }
    /* Sentence */
    .sentence-text { font-size:20px; font-weight:600; line-height:1.45; margin-bottom:20px; color:var(--text); }
    /* Feedback */
    .feedback { margin-top:16px; border-radius:12px; padding:16px 18px; border:1.5px solid; }
    .feedback.correct { background:var(--green-light); border-color:#86efac; }
    .feedback.partial  { background:var(--amber-light); border-color:#fde047; }
    .feedback.wrong    { background:var(--red-light); border-color:#fca5a5; }
    .score-badge { display:inline-block; font-size:12px; font-weight:700; padding:3px 10px; border-radius:99px; text-transform:uppercase; letter-spacing:.05em; margin-bottom:8px; }
    .correct .score-badge { background:#bbf7d0; color:#14532d; }
    .partial .score-badge { background:#fde68a; color:#78350f; }
    .wrong   .score-badge { background:#fecaca; color:#7f1d1d; }
    .feedback-body { font-size:14px; line-height:1.6; color:#374151; }
    .correct-block { margin-top:10px; padding-top:10px; border-top:1px solid rgba(0,0,0,.08); }
    .correct-block .lbl { font-size:10.5px; font-weight:700; text-transform:uppercase; letter-spacing:.07em; color:var(--muted); margin-bottom:4px; }
    .correct-block .val { font-size:16px; font-weight:600; }
    .mistakes-list { margin-top:10px; padding-top:10px; border-top:1px solid rgba(0,0,0,.08); }
    .mistake-item { background:rgba(0,0,0,.04); border-radius:6px; padding:8px 10px; margin-bottom:6px; font-size:13px; line-height:1.5; }
    .mistake-item strong { font-size:14px; }
    .mistake-gloss { color:var(--muted); font-size:12px; }
    /* Ask about correction */
    .ask-section { margin-top:14px; padding-top:14px; border-top:1px solid rgba(0,0,0,.08); }
    .ask-input { flex:1; height:36px; padding:0 12px; border:1.5px solid var(--border); border-radius:8px; font-family:inherit; font-size:14px; outline:none; background:#fafafa; color:var(--text); }
    .ask-input:focus { border-color:var(--green-mid); background:white; box-shadow:0 0 0 3px rgba(22,163,74,.12); }
    .ask-bubble { border-radius:10px; padding:10px 14px; margin-bottom:8px; font-size:14px; line-height:1.55; }
    .ask-q { background:var(--green-light); color:var(--green-dark); }
    .ask-a { background:rgba(0,0,0,.04); color:var(--text); }
    .row-actions { display:flex; gap:10px; margin-top:12px; align-items:center; }
    /* Summary */
    .stats-row { display:flex; gap:10px; margin-bottom:20px; }
    .stat-box { flex:1; background:var(--surface); border-radius:12px; box-shadow:var(--shadow); padding:14px 10px; text-align:center; }
    .stat-num { font-size:28px; font-weight:700; line-height:1; }
    .stat-lbl { font-size:11px; font-weight:600; text-transform:uppercase; letter-spacing:.06em; color:var(--muted); margin-top:4px; }
    .stat-box.correct .stat-num { color:#16a34a; }
    .stat-box.partial  .stat-num { color:#d97706; }
    .stat-box.wrong    .stat-num { color:#dc2626; }
    .sentence-group { margin-bottom:20px; }
    .sentence-ctx { background:#f8fafc; border-radius:8px; padding:10px 14px; margin-bottom:10px; font-size:13px; }
    .ctx-en { font-weight:600; }
    .ctx-yours { color:var(--muted); font-style:italic; margin-top:3px; }
    .sbadge { display:inline-block; font-size:11px; font-weight:700; padding:2px 9px; border-radius:99px; text-transform:uppercase; margin-bottom:6px; }
    .sbadge.partial { background:#fde68a; color:#78350f; }
    .sbadge.wrong   { background:#fecaca; color:#7f1d1d; }
    .mistake-card { background:var(--surface); border-radius:var(--radius); box-shadow:var(--shadow); padding:16px 18px; margin-bottom:10px; border:1.5px solid transparent; transition:border-color .15s,background .15s; }
    .mistake-card:has(.fc-cb:checked) { border-color:var(--green-mid); background:#f0fdf4; }
    .mc-phrase { font-size:16px; font-weight:700; margin-bottom:2px; }
    .mc-gloss { font-size:12px; color:var(--muted); font-weight:400; margin-left:5px; }
    .mc-feedback { font-size:13px; color:#374151; line-height:1.5; margin-bottom:10px; }
    .sentence-wrap { background:#f0fdf4; border-left:3px solid var(--green-mid); border-radius:0 6px 6px 0; padding:8px 12px; margin-bottom:10px; }
    .sentence-wrap .slbl { font-size:10px; font-weight:700; text-transform:uppercase; letter-spacing:.07em; color:var(--green); margin-bottom:3px; }
    .sentence-wrap .sval { font-size:14px; font-weight:600; color:var(--green-dark); }
    .btn-regen { height:28px; padding:0 12px; background:none; border:1.5px solid var(--border); border-radius:6px; font-family:inherit; font-size:12px; font-weight:500; color:var(--muted); cursor:pointer; transition:border-color .15s,color .15s; }
    .btn-regen:hover { border-color:var(--green-mid); color:var(--green); }
    .btn-regen:disabled { opacity:.5; cursor:not-allowed; }
    .flash-note { background:var(--green-light); border:1.5px solid #86efac; border-radius:10px; padding:12px 16px; font-size:14px; color:var(--green-dark); margin-bottom:12px; }
    .spinner { display:inline-block; width:18px; height:18px; border:2.5px solid rgba(255,255,255,.35); border-top-color:white; border-radius:50%; animation:spin .6s linear infinite; }
    @keyframes spin { to { transform:rotate(360deg); } }
    .btn.loading .btn-label { display:none; }
    .btn.loading .spinner { display:block; }
    .toolbar { display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
    .toolbar-wrap { position:sticky; top:calc(56px + env(safe-area-inset-top)); background:var(--bg); z-index:40; padding:10px 16px; border-bottom:1px solid var(--border); margin:0 -16px 16px; }
    .count-chip { background:var(--green-light); color:var(--green); font-size:13px; font-weight:700; padding:4px 12px; border-radius:99px; }
    .toast { display:none; position:fixed; bottom:28px; left:50%; transform:translateX(-50%); padding:14px 22px; border-radius:12px; font-size:14px; font-weight:600; box-shadow:0 8px 30px rgba(0,0,0,.18); z-index:99; white-space:nowrap; }
    .toast.show { display:block; animation:slideUp .25s ease; }
    .toast.success { background:var(--green); color:white; }
    .toast.error   { background:#dc2626; color:white; }
    @keyframes slideUp { from { transform:translateX(-50%) translateY(10px); opacity:0; } to { transform:translateX(-50%) translateY(0); opacity:1; } }
    /* Start page source picker */
    .src-tabs { display:flex; gap:8px; margin-bottom:20px; }
    .src-tab { flex:1; text-align:center; padding:10px 6px; border-radius:10px; border:1.5px solid var(--border); font-size:12px; font-weight:600; cursor:pointer; background:white; color:var(--muted); transition:all .15s; line-height:1.45; font-family:inherit; }
    .src-tab.active { background:var(--green); color:white; border-color:var(--green); }
    .src-panel { display:none; }
    .src-panel.active { display:block; }
    .topic-input { width:100%; padding:10px 12px; border:1.5px solid var(--border); border-radius:10px; font-family:inherit; font-size:16px; outline:none; transition:border-color .15s; color:var(--text); background:#fafafa; margin-bottom:10px; }
    .topic-input:focus { border-color:var(--green-mid); background:white; box-shadow:0 0 0 3px rgba(22,163,74,.12); }
    .diff-row { display:flex; gap:8px; margin-bottom:14px; }
    .diff-btn { flex:1; padding:9px 4px; border-radius:8px; border:1.5px solid var(--border); font-size:12px; font-weight:600; cursor:pointer; background:white; color:var(--muted); transition:all .15s; text-align:center; font-family:inherit; }
    .diff-btn.active { background:var(--green-light); color:var(--green-dark); border-color:var(--green-mid); }
    .preview-lbl { font-size:11px; font-weight:700; text-transform:uppercase; letter-spacing:.07em; color:var(--muted); margin-bottom:6px; margin-top:16px; display:flex; justify-content:space-between; align-items:center; }
    .btn-clear { background:none; border:none; font-size:12px; font-weight:600; color:var(--muted); cursor:pointer; padding:0; }
    .btn-clear:hover { color:#dc2626; }
"""

PRACTICE_START_PAGE = """<!doctype html>
<html lang="en"><head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
  <meta name="theme-color" content="#166534">
  <title>PT Practice — Practice Test</title>
  <style>""" + _PRACTICE_CSS + """</style>
</head><body>
<header>
  <span style="font-size:22px">🇵🇹</span>
  <h1>Practice Test</h1>
</header>
<main>
  <div class="card">
    <h2>Choose your text</h2>
    <p>Each sentence becomes a translation challenge into European Portuguese.</p>

    <!-- Difficulty selector (shared by conversation + scenario) -->
    <div class="field-label" style="margin-bottom:8px;">Difficulty</div>
    <div class="diff-row">
      <button type="button" class="diff-btn active" onclick="setDiff('b1')">B1</button>
      <button type="button" class="diff-btn" onclick="setDiff('b2')">B2</button>
      <button type="button" class="diff-btn" onclick="setDiff('c1')">C1</button>
      <button type="button" class="diff-btn" onclick="setDiff('c2')">C2</button>
    </div>

    <!-- Source tabs -->
    <div class="src-tabs">
      <button type="button" class="src-tab active" onclick="setMode('conversation')">💬 Conversation</button>
      <button type="button" class="src-tab" onclick="setMode('ai')">🤖 Scenario</button>
      <button type="button" class="src-tab" onclick="setMode('paste')">✏️ Paste text</button>
    </div>

    <!-- Conversation panel -->
    <div id="panel-conversation" class="src-panel active">
      <p style="font-size:13px;color:var(--muted);margin-bottom:12px;">Generates a realistic conversation, interview, or podcast excerpt.</p>
      <button type="button" class="btn btn-outline btn-block" id="convBtn" onclick="getParagraph('conversation')">
        <span class="btn-label">Generate conversation →</span>
        <span class="spinner" style="display:none;border-color:rgba(0,0,0,.2);border-top-color:var(--green);"></span>
      </button>
    </div>

    <!-- AI scenario panel -->
    <div id="panel-ai" class="src-panel">
      <p style="font-size:13px;color:var(--muted);margin-bottom:12px;">Generates a random everyday scenario each time.</p>
      <button type="button" class="btn btn-outline btn-block" id="generateBtn" onclick="getParagraph('ai')">
        <span class="btn-label">Generate scenario →</span>
        <span class="spinner" style="display:none;border-color:rgba(0,0,0,.2);border-top-color:var(--green);"></span>
      </button>
    </div>

    <!-- Paste panel -->
    <div id="panel-paste" class="src-panel">
      <label class="field-label" for="text">Your English text</label>
    </div>

    <!-- Shared: preview label + textarea + start button -->
    <form method="post" action="/practice/start" id="startForm">
      <div id="previewWrap" style="display:none;">
        <div class="preview-lbl">
          <span>Text to practise</span>
          <button type="button" class="btn-clear" onclick="clearText()">✕ Clear</button>
        </div>
      </div>
      <textarea id="text" name="text"
        placeholder="Paste your English text here…"
        rows="5" style="display:none;margin-top:0;"></textarea>
      <button type="submit" class="btn btn-primary btn-block" id="startBtn" disabled>
        Start practice →
      </button>
    </form>
  </div>
</main>
<div class="toast" id="toast"></div>
<script>
  let currentMode = 'conversation';
  let currentDiff = 'b1';

  function setMode(mode) {
    currentMode = mode;
    ['conversation','ai','paste'].forEach((m, i) => {
      document.querySelectorAll('.src-tab')[i].classList.toggle('active', m === mode);
    });
    document.querySelectorAll('.src-panel').forEach(p => p.classList.remove('active'));
    document.getElementById('panel-' + mode).classList.add('active');
    const ta = document.getElementById('text');
    if (mode === 'paste') {
      showTextarea(true);
    } else if (!ta.value.trim()) {
      ta.style.display = 'none';
      document.getElementById('previewWrap').style.display = 'none';
      document.getElementById('startBtn').disabled = true;
    }
  }

  function setDiff(d) {
    currentDiff = d;
    ['b1','b2','c1','c2'].forEach((v, i) => {
      document.querySelectorAll('.diff-btn')[i].classList.toggle('active', v === d);
    });
  }

  function showTextarea(focusIt) {
    const ta = document.getElementById('text');
    ta.style.display = 'block';
    document.getElementById('previewWrap').style.display = currentMode === 'paste' ? 'none' : 'block';
    document.getElementById('startBtn').disabled = !ta.value.trim();
    ta.oninput = () => { document.getElementById('startBtn').disabled = !ta.value.trim(); };
    if (focusIt) ta.focus();
  }

  function clearText() {
    const ta = document.getElementById('text');
    ta.value = '';
    ta.style.display = 'none';
    document.getElementById('previewWrap').style.display = 'none';
    document.getElementById('startBtn').disabled = true;
  }

  async function getParagraph(source) {
    const btnId = source === 'conversation' ? 'convBtn' : 'generateBtn';
    const btn = document.getElementById(btnId);
    const params = new URLSearchParams({ source, difficulty: currentDiff });
    btn.querySelector('.btn-label').style.display = 'none';
    btn.querySelector('.spinner').style.display = 'inline-block';
    btn.disabled = true;
    try {
      const resp = await fetch('/practice/get-paragraph?' + params);
      const data = await resp.json();
      if (data.error) throw new Error(data.error);
      document.getElementById('text').value = data.text;
      showTextarea(false);
    } catch(e) {
      showToast('Error: ' + (e.message || 'Could not fetch text'), 'error');
    } finally {
      btn.querySelector('.btn-label').style.display = '';
      btn.querySelector('.spinner').style.display = 'none';
      btn.disabled = false;
    }
  }

  function showToast(msg, type='success') {
    const t = document.getElementById('toast');
    t.textContent = msg;
    t.className = 'toast show ' + type;
    setTimeout(() => { t.className = 'toast'; }, 3500);
  }
</script>
""" + _bottom_nav_html('practice') + """
</body></html>
"""

PRACTICE_SENTENCE_PAGE = """<!doctype html>
<html lang="en"><head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
  <meta name="theme-color" content="#166534">
  <title>PT Practice · {{ current }}/{{ total }}</title>
  <style>""" + _PRACTICE_CSS + """</style>
</head><body>
<header>
  <span style="font-size:22px">🇵🇹</span>
  <h1>Practice Test</h1>
  <span style="margin-left:auto;background:rgba(255,255,255,.18);color:white;font-size:12px;font-weight:600;padding:4px 12px;border-radius:99px;">{{ current }} / {{ total }}</span>
</header>
<main>
  <div class="progress-wrap">
    <div class="progress-meta">
      <span>Sentence {{ current }} of {{ total }}</span>
      <span>{{ progress }}% complete</span>
    </div>
    <div class="progress-bar"><div class="progress-fill" style="width:{{ progress }}%"></div></div>
  </div>

  <div class="card">
    <label class="field-label">Translate into European Portuguese</label>
    <p class="sentence-text" id="englishSentence">{{ sentence }}</p>
    <textarea id="translationInput" placeholder="Escreva a sua tradução aqui…" rows="3" autofocus></textarea>
    <div class="row-actions">
      <button class="btn btn-primary" id="checkBtn" onclick="checkTranslation()">
        <span class="btn-label">Check</span>
        <div class="spinner" style="display:none"></div>
      </button>
      <button class="btn btn-outline btn-sm" onclick="finishEarly()">Done</button>
    </div>
    <div id="gradeError" style="display:none;background:var(--red-light);border:1.5px solid #fca5a5;border-radius:12px;padding:14px 16px;margin-top:12px;font-size:14px;color:#dc2626;line-height:1.5;"></div>

    <div class="feedback" id="feedback" style="display:none">
      <span class="score-badge" id="scoreBadge"></span>
      <div class="feedback-body">
        <p id="feedbackText"></p>
        <div class="mistakes-list" id="mistakesList" style="display:none"></div>
        <div class="correct-block" id="correctBlock">
          <div class="lbl">Correct translation</div>
          <div class="val" id="correctText"></div>
        </div>
      </div>
      <div class="ask-section" id="askSection" style="display:none;">
        <div class="ask-thread" id="askThread"></div>
        <div style="display:flex;gap:8px;margin-top:10px;">
          <input type="text" id="askInput" class="ask-input" placeholder="Ask about this correction…">
          <button class="btn btn-primary btn-sm" id="askBtn" onclick="askQuestion()" style="flex-shrink:0;">
            <span class="btn-label">Ask</span>
            <span class="spinner" style="display:none"></span>
          </button>
        </div>
      </div>
    </div>

    <div id="nextRow" style="display:none; margin-top:14px;">
      <button class="btn btn-primary btn-block" id="nextBtn" onclick="advance()">
        <span class="btn-label">Next sentence →</span>
        <div class="spinner" style="display:none"></div>
      </button>
    </div>
  </div>
</main>

<script>
  const english = document.getElementById('englishSentence').textContent;
  let gradeResult = null;

  async function checkTranslation() {
    const userPt = document.getElementById('translationInput').value.trim();
    if (!userPt) return;
    const btn = document.getElementById('checkBtn');
    setLoading(btn, true);
    try {
      const resp = await fetch('/practice/grade', {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({ english, user_pt: userPt }),
      });
      const data = await resp.json();
      if (data.error) throw new Error(data.error);
      gradeResult = data;
      gradeResult.english = english;
      gradeResult.user_translation = userPt;
      showFeedback(gradeResult);
      btn.style.display = 'none';
      document.getElementById('translationInput').disabled = true;
      document.getElementById('nextRow').style.display = 'block';
    } catch(e) {
      const ge = document.getElementById('gradeError');
      ge.textContent = e.message || 'Something went wrong.';
      const retryBtn = document.createElement('button');
      retryBtn.textContent = ' Retry';
      retryBtn.style.cssText = 'background:none;border:none;color:var(--green);font-weight:700;font-size:inherit;cursor:pointer;text-decoration:underline;font-family:inherit;padding:0;margin-left:4px;';
      retryBtn.onclick = function() { ge.style.display = 'none'; checkTranslation(); };
      ge.appendChild(retryBtn);
      ge.style.display = 'block';
    } finally {
      setLoading(btn, false);
    }
  }

  function showFeedback(r) {
    const panel     = document.getElementById('feedback');
    const cb        = document.getElementById('correctBlock');
    const ml        = document.getElementById('mistakesList');
    const badge     = document.getElementById('scoreBadge');

    panel.className      = 'feedback ' + (r.score || 'wrong');
    panel.style.display  = 'block';
    badge.textContent    = r.score === 'correct' ? '✓ Correct' : r.score === 'partial' ? '~ Partial' : '✗ Wrong';
    document.getElementById('feedbackText').textContent = r.feedback || '';

    document.getElementById('askSection').style.display = r.score === 'correct' ? 'none' : 'block';
    document.getElementById('askThread').innerHTML = '';

    if (r.score === 'correct') {
      cb.style.display = 'none';
      ml.style.display = 'none';
    } else {
      // Always show correct answer for wrong / partial
      cb.style.display = 'block';
      document.getElementById('correctText').textContent = r.correct_translation || '(not available)';
      // Show specific mistakes if any
      if (r.mistakes && r.mistakes.length > 0) {
        let html = '<div style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.07em;color:#64748b;margin-bottom:8px;">Specific errors</div>';
        for (const m of r.mistakes) {
          html += `<div class="mistake-item"><strong>${m.pt_key_phrase || ''}</strong>`;
          if (m.en_key_phrase) html += ` <span class="mistake-gloss">(${m.en_key_phrase})</span>`;
          html += `<br>${m.feedback || ''}</div>`;
        }
        ml.innerHTML = html;
        ml.style.display = 'block';
      } else {
        ml.style.display = 'none';
      }
    }
  }

  async function askQuestion() {
    const input = document.getElementById('askInput');
    const question = input.value.trim();
    if (!question || !gradeResult) return;
    const btn = document.getElementById('askBtn');
    setLoading(btn, true);
    input.disabled = true;
    // Show the question bubble immediately
    const thread = document.getElementById('askThread');
    thread.innerHTML += `<div class="ask-bubble ask-q">${question}</div>`;
    input.value = '';
    try {
      const resp = await fetch('/practice/ask', {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({
          question,
          english: gradeResult.english,
          user_translation: gradeResult.user_translation,
          correct_translation: gradeResult.correct_translation,
          score: gradeResult.score,
          feedback: gradeResult.feedback,
          mistakes: gradeResult.mistakes || [],
        }),
      });
      const data = await resp.json();
      if (data.error) throw new Error(data.error);
      thread.innerHTML += `<div class="ask-bubble ask-a">${data.answer}</div>`;
    } catch(e) {
      thread.innerHTML += `<div class="ask-bubble ask-a" style="color:#dc2626;">Could not get answer. Try again.</div>`;
    } finally {
      setLoading(btn, false);
      input.disabled = false;
      input.focus();
    }
  }

  async function advance() {
    const btn = document.getElementById('nextBtn');
    setLoading(btn, true);
    if (gradeResult) {
      await fetch('/practice/advance', {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify(gradeResult),
      });
    }
    window.location = '/practice/go';
  }

  async function finishEarly() {
    if (gradeResult) {
      await fetch('/practice/advance', {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify(gradeResult),
      });
    }
    window.location = '/practice/summary';
  }

  function setLoading(btn, on) {
    btn.disabled = on;
    btn.querySelector('.btn-label').style.display = on ? 'none' : '';
    btn.querySelector('.spinner').style.display = on ? 'inline-block' : 'none';
  }

  document.getElementById('translationInput').addEventListener('keydown', (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') checkTranslation();
  });
  document.getElementById('askInput').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') askQuestion();
  });
</script>
</body></html>
"""

PRACTICE_SUMMARY_PAGE = """<!doctype html>
<html lang="en"><head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
  <meta name="theme-color" content="#166534">
  <title>PT Practice · Summary</title>
  <style>""" + _PRACTICE_CSS + """</style>
</head><body>
<header>
  <span style="font-size:22px">🇵🇹</span>
  <h1>Practice Test</h1>
</header>
<main>
  {% set n_correct = results | selectattr('score','equalto','correct') | list | length %}
  {% set n_partial = results | selectattr('score','equalto','partial') | list | length %}
  {% set n_wrong   = results | selectattr('score','equalto','wrong')   | list | length %}

  <div class="stats-row">
    <div class="stat-box correct"><div class="stat-num">{{ n_correct }}</div><div class="stat-lbl">Correct</div></div>
    <div class="stat-box partial"><div class="stat-num">{{ n_partial }}</div><div class="stat-lbl">Partial</div></div>
    <div class="stat-box wrong"><div class="stat-num">{{ n_wrong }}</div><div class="stat-lbl">Wrong</div></div>
  </div>

  {% if n_partial > 0 or n_wrong > 0 %}
  <div class="flash-note">
    Tick the mistakes you want to save as flashcards, then tap <strong>Add to Flashcards</strong>.
  </div>
  <div class="toolbar-wrap">
    <div class="toolbar">
      <button class="btn btn-outline btn-sm" onclick="selectAll()">Select all</button>
      <button class="btn btn-outline btn-sm" onclick="selectNone()">Deselect all</button>
      <span class="count-chip" id="countChip" style="margin-left:auto">0 selected</span>
      <button class="btn btn-primary btn-sm" id="addBtn" onclick="addToFlashcards()" disabled>
        <span class="btn-label">Add to Flashcards</span>
        <span class="spinner" style="display:none"></span>
      </button>
    </div>
  </div>
  {% endif %}

  {% for r in results %}
  {% if r.score != 'correct' %}
  {% set outer_idx = loop.index %}
  <div class="sentence-group">
    <!-- Sentence context header — no checkbox here any more -->
    <div class="sentence-ctx">
      <span class="sbadge {{ r.score }}">{% if r.score == 'partial' %}~ Partial{% else %}✗ Wrong{% endif %}</span>
      <div class="ctx-en">{{ r.english }}</div>
      {% if r.user_translation %}<div class="ctx-yours">You wrote: {{ r.user_translation }}</div>{% endif %}
    </div>

    {% if r.mistakes %}
      {% for m in r.mistakes %}
      {% set mc_id = "mc-" ~ outer_idx ~ "-" ~ loop.index0 %}
      <div class="mistake-card" id="{{ mc_id }}">
        <label style="display:flex;align-items:flex-start;gap:10px;cursor:pointer;margin-bottom:8px;">
          <input type="checkbox" class="fc-cb" onchange="updateCount()"
            data-card-id="{{ mc_id }}"
            data-english="{{ r.english | e }}"
            data-portuguese="{{ m.pt_key_phrase | e }}"
            data-en-phrase="{{ m.en_key_phrase | default('') | e }}"
            data-feedback="{{ m.feedback | default('') | e }}"
            data-user-wrote="{{ r.user_translation | default('') | e }}"
            style="margin-top:3px;width:17px;height:17px;accent-color:#166534;flex-shrink:0;cursor:pointer;"
          >
          <div style="flex:1">
            <div class="mc-phrase">{{ m.pt_key_phrase }}<span class="mc-gloss">{% if m.en_key_phrase %} ({{ m.en_key_phrase }}){% endif %}</span></div>
            <div class="mc-feedback">{{ m.feedback }}</div>
          </div>
        </label>
        <div class="sentence-wrap">
          <div class="slbl">Example sentence</div>
          <div class="sval" id="sent-{{ outer_idx }}-{{ loop.index0 }}">{{ r.correct_translation }}</div>
        </div>
        <button class="btn-regen"
          data-phrase="{{ m.pt_key_phrase | e }}"
          data-target="sent-{{ outer_idx }}-{{ loop.index0 }}"
          onclick="regenerate(this)">↻ New sentence</button>
      </div>
      {% endfor %}
    {% else %}
      <!-- Fallback when no specific mistakes were identified -->
      {% set mc_id = "mc-" ~ outer_idx ~ "-0" %}
      <div class="mistake-card" id="{{ mc_id }}">
        <label style="display:flex;align-items:flex-start;gap:10px;cursor:pointer;margin-bottom:8px;">
          <input type="checkbox" class="fc-cb" onchange="updateCount()"
            data-card-id="{{ mc_id }}"
            data-english="{{ r.english | e }}"
            data-portuguese="{{ r.correct_translation | e }}"
            data-en-phrase=""
            data-feedback="{{ r.feedback | default('') | e }}"
            data-user-wrote="{{ r.user_translation | default('') | e }}"
            style="margin-top:3px;width:17px;height:17px;accent-color:#166534;flex-shrink:0;cursor:pointer;"
          >
          <div style="flex:1">
            <div class="mc-feedback">{{ r.feedback }}</div>
          </div>
        </label>
        <div class="sentence-wrap">
          <div class="slbl">Correct translation</div>
          <div class="sval">{{ r.correct_translation }}</div>
        </div>
      </div>
    {% endif %}
  </div>
  {% endif %}
  {% endfor %}

  <div style="display:flex;gap:10px;flex-wrap:wrap;margin-top:16px;">
    <a href="/practice/" class="btn btn-outline btn-sm">← New practice</a>
    <a href="/flashcards" class="btn btn-primary btn-sm">📚 Go to flashcards</a>
  </div>
</main>

<div class="toast" id="toast"></div>

<script>
  function updateCount() {
    const n = document.querySelectorAll('.fc-cb:checked').length;
    document.getElementById('countChip').textContent = n + ' selected';
    document.getElementById('addBtn').disabled = n === 0;
  }

  function selectAll()  { document.querySelectorAll('.fc-cb:not(:disabled)').forEach(c => c.checked = true);  updateCount(); }
  function selectNone() { document.querySelectorAll('.fc-cb').forEach(c => c.checked = false); updateCount(); }

  async function addToFlashcards() {
    const checked = [...document.querySelectorAll('.fc-cb:checked')];
    if (!checked.length) return;

    const items = checked.map(c => ({
      english:    c.dataset.english,
      portuguese: c.dataset.portuguese,
      en_phrase:  c.dataset.enPhrase,
      user_wrote: c.dataset.userWrote,
      feedback:   c.dataset.feedback,
    }));

    const btn = document.getElementById('addBtn');
    btn.disabled = true;
    btn.querySelector('.btn-label').style.display = 'none';
    btn.querySelector('.spinner').style.display = 'inline-block';

    try {
      const resp = await fetch('/practice/add-to-flashcards', {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({items}),
      });
      const data = await resp.json();
      if (data.error) throw new Error(data.error);
      // Dim and disable the rows that were added
      checked.forEach(c => {
        c.checked = false;
        c.disabled = true;
        c.closest('.mistake-card').style.opacity = '0.5';
      });
      showToast(`${data.count} error${data.count !== 1 ? 's' : ''} added to flashcards ✓`);
    } catch(e) {
      showToast('Error: ' + (e.message || 'Something went wrong'), 'error');
    } finally {
      btn.querySelector('.btn-label').style.display = '';
      btn.querySelector('.spinner').style.display = 'none';
      updateCount();
    }
  }

  async function regenerate(btn) {
    const phrase = btn.dataset.phrase;
    const target = btn.dataset.target;
    btn.disabled = true;
    btn.textContent = '↻ Generating…';
    try {
      const resp = await fetch('/practice/generate-sentence', {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({ pt_key_phrase: phrase }),
      });
      const data = await resp.json();
      if (data.sentence) document.getElementById(target).textContent = data.sentence;
    } catch(e) {}
    btn.disabled = false;
    btn.textContent = '↻ New sentence';
  }

  let _toastTimer;
  function showToast(msg, type = 'success') {
    const t = document.getElementById('toast');
    t.textContent = msg;
    t.className = 'toast show ' + type;
    clearTimeout(_toastTimer);
    _toastTimer = setTimeout(() => t.classList.remove('show'), 4000);
  }
</script>
""" + _bottom_nav_html('practice') + """
</body></html>
"""


if __name__ == "__main__":
    host = os.getenv("FLASK_HOST", "0.0.0.0")
    port = int(os.getenv("FLASK_PORT", "5001"))
    print(f"\n  PT Practice app running at http://localhost:{port}/")
    print(f"  On your phone (same Wi-Fi): http://<your-mac-ip>:{port}/\n")
    app.run(host=host, port=port, debug=False)
