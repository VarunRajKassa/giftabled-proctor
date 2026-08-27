import os
import io
import json
import random
import string
import secrets
import smtplib
import sqlite3
import uuid
from email.mime.text import MIMEText
from datetime import datetime
from flask import (
    Flask, render_template, request, jsonify, redirect,
    url_for, session, g, send_from_directory, send_file
)
from werkzeug.security import generate_password_hash, check_password_hash
import requests

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-this")
app.jinja_env.filters["from_json"] = lambda s: json.loads(s) if s else []

DB_PATH = os.path.join(os.path.dirname(__file__), "proctor.db")
RECORDINGS_DIR = os.path.join(os.path.dirname(__file__), "recordings")
os.makedirs(RECORDINGS_DIR, exist_ok=True)

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = "openai/gpt-oss-120b"

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "changeme")

SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587") or 587)
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
FROM_EMAIL = os.environ.get("FROM_EMAIL", SMTP_USER)

DIFFICULTY_LABELS = {"easy": "Easy", "medium": "Medium", "hard": "Hard"}


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def _ensure_column(db, table, column, coltype):
    cols = [r[1] for r in db.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in cols:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS assignments (
            id TEXT PRIMARY KEY,
            topic TEXT NOT NULL,
            difficulty TEXT NOT NULL DEFAULT 'medium',
            duration_minutes INTEGER NOT NULL DEFAULT 30,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS questions (
            id TEXT PRIMARY KEY,
            assignment_id TEXT NOT NULL,
            qtype TEXT NOT NULL,
            prompt TEXT NOT NULL,
            options_json TEXT,
            correct_json TEXT,
            FOREIGN KEY (assignment_id) REFERENCES assignments(id)
        );

        CREATE TABLE IF NOT EXISTS students (
            id TEXT PRIMARY KEY,
            assignment_id TEXT NOT NULL,
            name TEXT NOT NULL,
            email TEXT,
            username TEXT UNIQUE,
            password_hash TEXT,
            password_plain TEXT,
            started_at TEXT,
            submitted_at TEXT,
            ended_reason TEXT,                -- null if normal submit, else auto-termination reason
            question_order_json TEXT,
            recording_path TEXT,
            FOREIGN KEY (assignment_id) REFERENCES assignments(id)
        );

        CREATE TABLE IF NOT EXISTS responses (
            id TEXT PRIMARY KEY,
            student_id TEXT NOT NULL,
            question_id TEXT NOT NULL,
            answer_text TEXT,
            score INTEGER,
            feedback TEXT,
            FOREIGN KEY (student_id) REFERENCES students(id),
            FOREIGN KEY (question_id) REFERENCES questions(id)
        );

        CREATE TABLE IF NOT EXISTS proctor_logs (
            id TEXT PRIMARY KEY,
            student_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            message TEXT NOT NULL,
            level TEXT NOT NULL,
            ts TEXT NOT NULL,
            FOREIGN KEY (student_id) REFERENCES students(id)
        );

        CREATE TABLE IF NOT EXISTS batches (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS batch_members (
            id TEXT PRIMARY KEY,
            batch_id TEXT NOT NULL,
            name TEXT NOT NULL,
            email TEXT,
            FOREIGN KEY (batch_id) REFERENCES batches(id)
        );
        """
    )
    _ensure_column(db, "assignments", "difficulty", "TEXT NOT NULL DEFAULT 'medium'")
    _ensure_column(db, "assignments", "duration_minutes", "INTEGER NOT NULL DEFAULT 30")
    _ensure_column(db, "students", "question_order_json", "TEXT")
    _ensure_column(db, "students", "recording_path", "TEXT")
    _ensure_column(db, "students", "username", "TEXT")
    _ensure_column(db, "students", "password_hash", "TEXT")
    _ensure_column(db, "students", "password_plain", "TEXT")
    _ensure_column(db, "students", "email", "TEXT")
    _ensure_column(db, "students", "ended_reason", "TEXT")
    _ensure_column(db, "responses", "score", "INTEGER")
    _ensure_column(db, "responses", "feedback", "TEXT")
    db.commit()
    db.close()


# ---------------------------------------------------------------------------
# Groq: question generation + code grading + improvement report
# ---------------------------------------------------------------------------
def _groq_chat(system_prompt, user_prompt, temperature=0.7, timeout=60):
    resp = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
        json={
            "model": GROQ_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature,
            "response_format": {"type": "json_object"},
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    return json.loads(resp.json()["choices"][0]["message"]["content"])


def generate_questions(topic, num_mcq, num_msq, num_code, difficulty="medium"):
    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY is not set.")
    difficulty_label = DIFFICULTY_LABELS.get(difficulty, "Medium")
    system_prompt = (
        "You are a question-bank generator for a student assessment platform. "
        "Respond with ONLY valid JSON, no markdown fences, no commentary. Schema:\n"
        "{\n"
        '  "mcq": [ { "prompt": str, "options": [str, str, str, str], "correct_index": int } ],\n'
        '  "msq": [ { "prompt": str, "options": [str, str, str, str], "correct_indices": [int, ...] } ],\n'
        '  "code": [ { "prompt": str } ]\n'
        "}\n"
        "MCQ = single correct answer. MSQ = at least 2 correct options. "
        "Code questions are open-ended programming problems, no stored solution needed."
    )
    user_prompt = (
        f"Topic: {topic}\nDifficulty level: {difficulty_label}\n"
        f"Generate exactly {num_mcq} MCQ, {num_msq} MSQ, and {num_code} coding questions "
        f"on this topic at {difficulty_label} difficulty."
    )
    return _groq_chat(system_prompt, user_prompt, temperature=0.7)


def grade_code_answer(question_prompt, student_code):
    if not GROQ_API_KEY:
        return None, "Auto-grading unavailable (no GROQ_API_KEY set) — please review manually."
    if not student_code or not student_code.strip():
        return 0, "No answer submitted."
    system_prompt = (
        "You are grading a student's coding answer. Respond with ONLY valid JSON: "
        '{"score": int, "feedback": str}. score is 0-100. feedback is 1-2 sentences, '
        "constructive and specific about what's right or missing."
    )
    user_prompt = f"Question: {question_prompt}\n\nStudent's answer:\n{student_code}"
    try:
        data = _groq_chat(system_prompt, user_prompt, temperature=0.3, timeout=30)
        score = max(0, min(100, int(data.get("score", 0))))
        return score, data.get("feedback", "")
    except Exception as e:
        return None, f"Auto-grading failed ({e}). Please review manually."


def generate_improvement_report(topic, wrong_prompts):
    """Short study-guidance paragraph based on questions the student got wrong."""
    if not wrong_prompts:
        return "Great work — no incorrect answers to review."
    if not GROQ_API_KEY:
        return "Review these topics you missed: " + "; ".join(wrong_prompts[:5])
    system_prompt = (
        "You are an academic coach. Respond with ONLY valid JSON: {\"summary\": str}. "
        "Given a topic and a list of questions a student got wrong, write a short (3-5 sentence) "
        "encouraging paragraph naming the specific sub-topics they should review, based on the pattern "
        "of mistakes. Do not repeat the questions verbatim."
    )
    user_prompt = f"Topic: {topic}\n\nQuestions answered incorrectly:\n" + "\n".join(f"- {p}" for p in wrong_prompts)
    try:
        data = _groq_chat(system_prompt, user_prompt, temperature=0.5, timeout=30)
        return data.get("summary", "")
    except Exception:
        return "Review these topics you missed: " + "; ".join(wrong_prompts[:5])


# ---------------------------------------------------------------------------
# Per-student question / option shuffling
# ---------------------------------------------------------------------------
def build_question_order(assignment_id):
    db = get_db()
    questions = db.execute("SELECT * FROM questions WHERE assignment_id = ?", (assignment_id,)).fetchall()
    q_ids = [q["id"] for q in questions]
    random.shuffle(q_ids)
    option_orders = {}
    for q in questions:
        if q["options_json"]:
            n = len(json.loads(q["options_json"]))
            idxs = list(range(n))
            random.shuffle(idxs)
            option_orders[q["id"]] = idxs
    return {"order": q_ids, "options": option_orders}


# ---------------------------------------------------------------------------
# Credential generation + email
# ---------------------------------------------------------------------------
def _slugify(name):
    base = "".join(ch for ch in name.lower().strip() if ch.isalnum() or ch == " ")
    return "".join(base.split())[:12] or "student"


def generate_credentials(db, name):
    base = _slugify(name)
    while True:
        candidate = f"{base}{secrets.randbelow(9000) + 1000}"
        exists = db.execute("SELECT 1 FROM students WHERE username = ?", (candidate,)).fetchone()
        if not exists:
            username = candidate
            break
    alphabet = string.ascii_uppercase + string.ascii_lowercase + string.digits
    password = "".join(secrets.choice(alphabet) for _ in range(8))
    return username, password


def send_credentials_email(to_email, name, username, password, login_link, topic):
    if not (SMTP_HOST and SMTP_USER and SMTP_PASSWORD):
        return False, "SMTP not configured"
    subject = f"Your login for the '{topic}' assessment"
    body = (
        f"Hi {name},\n\n"
        f"You've been added to the '{topic}' assessment on GiftAbled's assessment platform.\n\n"
        f"Login link: {login_link}\nUsername: {username}\nPassword: {password}\n\n"
        f"Please keep these credentials private. Good luck!\n"
    )
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = FROM_EMAIL
    msg["To"] = to_email
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(FROM_EMAIL, [to_email], msg.as_string())
        return True, None
    except Exception as e:
        return False, str(e)


def _parse_student_lines(raw):
    """Parses lines like 'Name, email@x.com' or just 'email@x.com'. Returns list of (name, email)."""
    out = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        if "," in line:
            name, email = line.split(",", 1)
            name, email = name.strip(), email.strip()
        elif "@" in line:
            email = line
            name = email.split("@")[0].replace(".", " ").replace("_", " ").title()
        else:
            name, email = line, ""
        out.append((name, email))
    return out


# ---------------------------------------------------------------------------
# Admin auth
# ---------------------------------------------------------------------------
def admin_required(fn):
    from functools import wraps

    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect(url_for("admin_login"))
        return fn(*args, **kwargs)

    return wrapper


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    error = None
    if request.method == "POST":
        u = request.form.get("username", "")
        p = request.form.get("password", "")
        if u == ADMIN_USERNAME and p == ADMIN_PASSWORD:
            session["is_admin"] = True
            return redirect(url_for("admin_dashboard"))
        error = "Invalid username or password."
    return render_template("admin_login.html", error=error)


@app.route("/admin/logout")
def admin_logout():
    session.pop("is_admin", None)
    return redirect(url_for("admin_login"))


# ---------------------------------------------------------------------------
# Admin dashboard
# ---------------------------------------------------------------------------
@app.route("/admin")
@admin_required
def admin_dashboard():
    db = get_db()
    assignments = db.execute("SELECT * FROM assignments ORDER BY created_at DESC").fetchall()
    return render_template("admin_dashboard.html", assignments=assignments)


@app.route("/admin/create", methods=["GET", "POST"])
@admin_required
def create_assignment():
    error = None
    if request.method == "POST":
        topic = request.form.get("topic", "").strip()
        difficulty = request.form.get("difficulty", "medium")
        duration_minutes = int(request.form.get("duration_minutes", 30) or 30)
        num_mcq = int(request.form.get("num_mcq", 0) or 0)
        num_msq = int(request.form.get("num_msq", 0) or 0)
        num_code = int(request.form.get("num_code", 0) or 0)

        if not topic or (num_mcq + num_msq + num_code) == 0:
            error = "Enter a topic and at least one question count."
        else:
            try:
                data = generate_questions(topic, num_mcq, num_msq, num_code, difficulty)
            except Exception as e:
                error = f"Question generation failed: {e}"
                return render_template("create_assignment.html", error=error)

            db = get_db()
            assignment_id = str(uuid.uuid4())[:8]
            db.execute(
                "INSERT INTO assignments (id, topic, difficulty, duration_minutes, created_at) VALUES (?, ?, ?, ?, ?)",
                (assignment_id, topic, difficulty, duration_minutes, datetime.utcnow().isoformat()),
            )
            for q in data.get("mcq", []):
                db.execute(
                    "INSERT INTO questions (id, assignment_id, qtype, prompt, options_json, correct_json) "
                    "VALUES (?, ?, 'mcq', ?, ?, ?)",
                    (str(uuid.uuid4()), assignment_id, q["prompt"], json.dumps(q["options"]), json.dumps([q["correct_index"]])),
                )
            for q in data.get("msq", []):
                db.execute(
                    "INSERT INTO questions (id, assignment_id, qtype, prompt, options_json, correct_json) "
                    "VALUES (?, ?, 'msq', ?, ?, ?)",
                    (str(uuid.uuid4()), assignment_id, q["prompt"], json.dumps(q["options"]), json.dumps(q["correct_indices"])),
                )
            for q in data.get("code", []):
                db.execute(
                    "INSERT INTO questions (id, assignment_id, qtype, prompt, options_json, correct_json) "
                    "VALUES (?, ?, 'code', ?, NULL, NULL)",
                    (str(uuid.uuid4()), assignment_id, q["prompt"]),
                )
            db.commit()
            return redirect(url_for("assignment_detail", assignment_id=assignment_id))

    return render_template("create_assignment.html", error=error)


def _score_summary(db, student_id, total_questions):
    agg = db.execute(
        "SELECT SUM(score) as total_score, COUNT(*) as graded_count FROM responses WHERE student_id = ? AND score IS NOT NULL",
        (student_id,),
    ).fetchone()
    if agg["graded_count"] is None or agg["graded_count"] == 0 or total_questions == 0:
        return None
    marks = (agg["total_score"] or 0) / 100.0
    percent = round((marks / total_questions) * 100, 1)
    return {"marks": round(marks, 1), "total": total_questions, "percent": percent}


@app.route("/admin/assignment/<assignment_id>")
@admin_required
def assignment_detail(assignment_id):
    db = get_db()
    assignment = db.execute("SELECT * FROM assignments WHERE id = ?", (assignment_id,)).fetchone()
    questions = db.execute("SELECT * FROM questions WHERE assignment_id = ?", (assignment_id,)).fetchall()
    total_questions = len(questions)
    student_rows = db.execute(
        "SELECT * FROM students WHERE assignment_id = ? ORDER BY started_at DESC", (assignment_id,)
    ).fetchall()

    students = []
    for s in student_rows:
        summary = _score_summary(db, s["id"], total_questions)
        students.append({
            "id": s["id"], "name": s["name"], "username": s["username"],
            "started_at": s["started_at"], "submitted_at": s["submitted_at"],
            "ended_reason": s["ended_reason"],
            "has_recording": bool(s["recording_path"]),
            "score_summary": summary,
        })

    login_link = url_for("student_login", _external=True)
    return render_template(
        "assignment_detail.html", assignment=assignment, questions=questions,
        students=students, login_link=login_link,
    )


def _add_students_to_assignment(db, assignment_id, topic, entries, send_email):
    """Adds (name, email) entries to an assignment, skipping duplicates. Returns (new_credentials, skipped, email_results)."""
    new_credentials, skipped, email_results = [], [], []
    login_link = url_for("student_login", _external=True)

    for name, email in entries:
        existing = None
        if email:
            existing = db.execute(
                "SELECT id FROM students WHERE assignment_id = ? AND email = ?", (assignment_id, email)
            ).fetchone()
        else:
            existing = db.execute(
                "SELECT id FROM students WHERE assignment_id = ? AND name = ? AND (email IS NULL OR email = '')",
                (assignment_id, name),
            ).fetchone()

        if existing:
            skipped.append(name)
            continue

        username, password = generate_credentials(db, name)
        student_id = str(uuid.uuid4())
        db.execute(
            "INSERT INTO students (id, assignment_id, name, email, username, password_hash, password_plain) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (student_id, assignment_id, name, email or None, username, generate_password_hash(password), password),
        )
        new_credentials.append({"name": name, "email": email, "username": username, "password": password})

        if send_email and email:
            ok, err = send_credentials_email(email, name, username, password, login_link, topic)
            email_results.append({"email": email, "sent": ok, "error": err})

    db.commit()
    return new_credentials, skipped, email_results


@app.route("/admin/batches", methods=["GET", "POST"])
@admin_required
def manage_batches():
    db = get_db()
    error = None
    if request.method == "POST":
        name = request.form.get("batch_name", "").strip()
        raw = request.form.get("names", "")
        entries = _parse_student_lines(raw)
        if not name or not entries:
            error = "Enter a batch name and at least one student."
        else:
            batch_id = str(uuid.uuid4())[:8]
            db.execute("INSERT INTO batches (id, name, created_at) VALUES (?, ?, ?)",
                       (batch_id, name, datetime.utcnow().isoformat()))
            for student_name, email in entries:
                db.execute("INSERT INTO batch_members (id, batch_id, name, email) VALUES (?, ?, ?, ?)",
                           (str(uuid.uuid4()), batch_id, student_name, email or None))
            db.commit()
            return redirect(url_for("view_batch", batch_id=batch_id))

    batches = db.execute("SELECT * FROM batches ORDER BY created_at DESC").fetchall()
    batch_list = []
    for b in batches:
        count = db.execute("SELECT COUNT(*) as c FROM batch_members WHERE batch_id = ?", (b["id"],)).fetchone()["c"]
        batch_list.append({"id": b["id"], "name": b["name"], "created_at": b["created_at"], "member_count": count})

    return render_template("manage_batches.html", batches=batch_list, error=error)


@app.route("/admin/batches/<batch_id>", methods=["GET", "POST"])
@admin_required
def view_batch(batch_id):
    db = get_db()
    batch = db.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
    if not batch:
        return "Batch not found.", 404

    if request.method == "POST":
        raw = request.form.get("names", "")
        entries = _parse_student_lines(raw)
        for student_name, email in entries:
            db.execute("INSERT INTO batch_members (id, batch_id, name, email) VALUES (?, ?, ?, ?)",
                       (str(uuid.uuid4()), batch_id, student_name, email or None))
        db.commit()

    members = db.execute("SELECT * FROM batch_members WHERE batch_id = ? ORDER BY name", (batch_id,)).fetchall()
    return render_template("view_batch.html", batch=batch, members=members)


@app.route("/admin/batches/<batch_id>/delete", methods=["POST"])
@admin_required
def delete_batch(batch_id):
    db = get_db()
    db.execute("DELETE FROM batch_members WHERE batch_id = ?", (batch_id,))
    db.execute("DELETE FROM batches WHERE id = ?", (batch_id,))
    db.commit()
    return redirect(url_for("manage_batches"))


@app.route("/admin/assignment/<assignment_id>/students", methods=["GET", "POST"])
@admin_required
def manage_students(assignment_id):
    db = get_db()
    assignment = db.execute("SELECT * FROM assignments WHERE id = ?", (assignment_id,)).fetchone()
    if not assignment:
        return "Assignment not found.", 404

    new_credentials, skipped, email_results = [], [], []

    if request.method == "POST":
        send_email = request.form.get("send_email") == "on"
        batch_id = request.form.get("batch_id", "")

        if batch_id:
            members = db.execute("SELECT name, email FROM batch_members WHERE batch_id = ?", (batch_id,)).fetchall()
            entries = [(m["name"], m["email"] or "") for m in members]
        else:
            raw = request.form.get("names", "")
            entries = _parse_student_lines(raw)

        new_credentials, skipped, email_results = _add_students_to_assignment(
            db, assignment_id, assignment["topic"], entries, send_email
        )

    all_students = db.execute(
        "SELECT * FROM students WHERE assignment_id = ? ORDER BY name ASC", (assignment_id,)
    ).fetchall()
    smtp_configured = bool(SMTP_HOST and SMTP_USER and SMTP_PASSWORD)
    batches = db.execute("SELECT * FROM batches ORDER BY name").fetchall()

    return render_template(
        "manage_students.html", assignment=assignment, students=all_students,
        new_credentials=new_credentials, skipped=skipped, email_results=email_results,
        smtp_configured=smtp_configured, batches=batches,
    )


@app.route("/admin/assignment/<assignment_id>/credentials.xlsx")
@admin_required
def export_credentials_xlsx(assignment_id):
    from openpyxl import Workbook

    db = get_db()
    assignment = db.execute("SELECT * FROM assignments WHERE id = ?", (assignment_id,)).fetchone()
    students = db.execute(
        "SELECT name, email, username, password_plain FROM students WHERE assignment_id = ? ORDER BY name",
        (assignment_id,),
    ).fetchall()

    wb = Workbook()
    ws = wb.active
    ws.title = "Credentials"
    ws.append(["Name", "Email", "Username", "Password"])
    for s in students:
        ws.append([s["name"], s["email"] or "", s["username"], s["password_plain"]])
    for col in "ABCD":
        ws.column_dimensions[col].width = 24

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    topic_slug = (assignment["topic"] if assignment else "assignment").replace(" ", "_")
    return send_file(buf, as_attachment=True, download_name=f"{topic_slug}_credentials.xlsx",
                      mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.route("/admin/assignment/<assignment_id>/results.xlsx")
@admin_required
def export_results_xlsx(assignment_id):
    from openpyxl import Workbook

    db = get_db()
    assignment = db.execute("SELECT * FROM assignments WHERE id = ?", (assignment_id,)).fetchone()
    total_questions = db.execute(
        "SELECT COUNT(*) as c FROM questions WHERE assignment_id = ?", (assignment_id,)
    ).fetchone()["c"]
    students = db.execute("SELECT * FROM students WHERE assignment_id = ? ORDER BY name", (assignment_id,)).fetchall()

    wb = Workbook()
    ws = wb.active
    ws.title = "Results"
    ws.append(["Name", "Username", "Started", "Submitted", "Marks", "Total", "Percent", "Ended Reason", "Proctoring Flags", "Has Recording"])

    for s in students:
        summary = _score_summary(db, s["id"], total_questions)
        flag_count = db.execute(
            "SELECT COUNT(*) as c FROM proctor_logs WHERE student_id = ? AND level IN ('warn','danger')", (s["id"],)
        ).fetchone()["c"]
        ws.append([
            s["name"], s["username"] or "", s["started_at"] or "", s["submitted_at"] or "",
            summary["marks"] if summary else "", summary["total"] if summary else total_questions,
            summary["percent"] if summary else "", s["ended_reason"] or "",
            flag_count, "Yes" if s["recording_path"] else "No",
        ])
    for col in "ABCDEFGHIJ":
        ws.column_dimensions[col].width = 18

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    topic_slug = (assignment["topic"] if assignment else "assignment").replace(" ", "_")
    return send_file(buf, as_attachment=True, download_name=f"{topic_slug}_results.xlsx",
                      mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.route("/admin/student/<student_id>")
@admin_required
def student_detail(student_id):
    db = get_db()
    student = db.execute("SELECT * FROM students WHERE id = ?", (student_id,)).fetchone()
    total_questions = db.execute(
        "SELECT COUNT(*) as c FROM questions WHERE assignment_id = ?", (student["assignment_id"],)
    ).fetchone()["c"]
    rows = db.execute(
        """SELECT responses.answer_text, responses.score, responses.feedback,
                  questions.prompt, questions.qtype, questions.options_json, questions.correct_json
           FROM responses JOIN questions ON responses.question_id = questions.id
           WHERE responses.student_id = ?""",
        (student_id,),
    ).fetchall()

    responses = []
    for r in rows:
        correct_text = None
        if r["correct_json"] and r["options_json"]:
            opts = json.loads(r["options_json"])
            correct_idxs = json.loads(r["correct_json"])
            correct_text = ", ".join(opts[i] for i in correct_idxs)
        responses.append({
            "prompt": r["prompt"], "qtype": r["qtype"], "answer_text": r["answer_text"],
            "score": r["score"], "feedback": r["feedback"], "correct_text": correct_text,
        })

    summary = _score_summary(db, student_id, total_questions)
    logs = db.execute("SELECT * FROM proctor_logs WHERE student_id = ? ORDER BY ts ASC", (student_id,)).fetchall()
    return render_template("student_detail.html", student=student, responses=responses, logs=logs, summary=summary)


@app.route("/admin/recording/<student_id>")
@admin_required
def admin_recording(student_id):
    db = get_db()
    student = db.execute("SELECT * FROM students WHERE id = ?", (student_id,)).fetchone()
    if not student or not student["recording_path"]:
        return "No recording available for this student.", 404
    return send_from_directory(RECORDINGS_DIR, student["recording_path"])


# ---------------------------------------------------------------------------
# Student login + instructions + exam flow + report
# ---------------------------------------------------------------------------
@app.route("/")
def home():
    return render_template("home.html")


@app.route("/login", methods=["GET", "POST"])
def student_login():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        db = get_db()
        student = db.execute("SELECT * FROM students WHERE username = ?", (username,)).fetchone()
        if student and student["password_hash"] and check_password_hash(student["password_hash"], password):
            if student["submitted_at"]:
                return redirect(url_for("exam_report", student_id=student["id"]))
            return redirect(url_for("exam_instructions", student_id=student["id"]))
        error = "Invalid username or password. Check with your instructor if you're unsure."
    return render_template("student_login.html", error=error)


@app.route("/exam/<assignment_id>")
def exam_entry(assignment_id):
    return redirect(url_for("student_login"))


@app.route("/exam/instructions/<student_id>")
def exam_instructions(student_id):
    db = get_db()
    student = db.execute("SELECT * FROM students WHERE id = ?", (student_id,)).fetchone()
    if not student:
        return "Session not found.", 404
    if student["submitted_at"]:
        return redirect(url_for("exam_report", student_id=student_id))

    assignment = db.execute("SELECT * FROM assignments WHERE id = ?", (student["assignment_id"],)).fetchone()
    counts = db.execute(
        "SELECT qtype, COUNT(*) as c FROM questions WHERE assignment_id = ? GROUP BY qtype",
        (student["assignment_id"],),
    ).fetchall()
    count_map = {r["qtype"]: r["c"] for r in counts}
    difficulty_label = DIFFICULTY_LABELS.get(assignment["difficulty"], "Medium")

    return render_template(
        "exam_instructions.html", student=student, assignment=assignment,
        count_map=count_map, difficulty_label=difficulty_label,
    )


@app.route("/exam/session/<student_id>")
def exam_session(student_id):
    db = get_db()
    student = db.execute("SELECT * FROM students WHERE id = ?", (student_id,)).fetchone()
    if not student:
        return "Session not found.", 404
    if student["submitted_at"]:
        return redirect(url_for("exam_report", student_id=student_id))

    assignment = db.execute("SELECT * FROM assignments WHERE id = ?", (student["assignment_id"],)).fetchone()

    if not student["started_at"]:
        db.execute("UPDATE students SET started_at = ? WHERE id = ?", (datetime.utcnow().isoformat(), student_id))
        db.commit()
        student = db.execute("SELECT * FROM students WHERE id = ?", (student_id,)).fetchone()

    order_data = json.loads(student["question_order_json"]) if student["question_order_json"] else None
    if not order_data:
        order_data = build_question_order(student["assignment_id"])
        db.execute("UPDATE students SET question_order_json = ? WHERE id = ?", (json.dumps(order_data), student_id))
        db.commit()

    all_questions = {
        q["id"]: q for q in db.execute(
            "SELECT * FROM questions WHERE assignment_id = ?", (student["assignment_id"],)
        ).fetchall()
    }
    ordered_ids = order_data["order"] if order_data.get("order") else list(all_questions.keys())

    q_list = []
    for qid in ordered_ids:
        q = all_questions.get(qid)
        if not q:
            continue
        options = None
        if q["options_json"]:
            original_options = json.loads(q["options_json"])
            idx_map = order_data["options"].get(qid)
            options = [original_options[i] for i in idx_map] if idx_map else original_options
        q_list.append({"id": q["id"], "qtype": q["qtype"], "prompt": q["prompt"], "options": options})

    return render_template(
        "exam_session.html", student=student, questions_json=json.dumps(q_list),
        duration_minutes=assignment["duration_minutes"],
    )


@app.route("/exam/report/<student_id>")
def exam_report(student_id):
    db = get_db()
    student = db.execute("SELECT * FROM students WHERE id = ?", (student_id,)).fetchone()
    if not student:
        return "Session not found.", 404
    if not student["submitted_at"]:
        return redirect(url_for("exam_instructions", student_id=student_id))

    assignment = db.execute("SELECT * FROM assignments WHERE id = ?", (student["assignment_id"],)).fetchone()
    total_questions = db.execute(
        "SELECT COUNT(*) as c FROM questions WHERE assignment_id = ?", (student["assignment_id"],)
    ).fetchone()["c"]

    rows = db.execute(
        """SELECT responses.answer_text, responses.score, responses.feedback,
                  questions.prompt, questions.qtype, questions.options_json, questions.correct_json
           FROM responses JOIN questions ON responses.question_id = questions.id
           WHERE responses.student_id = ?""",
        (student_id,),
    ).fetchall()

    responses, wrong_prompts = [], []
    for r in rows:
        correct_text = None
        if r["correct_json"] and r["options_json"]:
            opts = json.loads(r["options_json"])
            correct_idxs = json.loads(r["correct_json"])
            correct_text = ", ".join(opts[i] for i in correct_idxs)
        is_low_score = r["score"] is not None and r["score"] < 70
        if is_low_score:
            wrong_prompts.append(r["prompt"])
        responses.append({
            "prompt": r["prompt"], "qtype": r["qtype"], "answer_text": r["answer_text"],
            "score": r["score"], "feedback": r["feedback"], "correct_text": correct_text,
            "is_low_score": is_low_score,
        })

    summary = _score_summary(db, student_id, total_questions)
    improvement_summary = generate_improvement_report(assignment["topic"], wrong_prompts)

    return render_template(
        "exam_report.html", student=student, assignment=assignment,
        responses=responses, summary=summary, improvement_summary=improvement_summary,
    )


@app.route("/api/log", methods=["POST"])
def api_log():
    data = request.get_json(force=True)
    db = get_db()
    db.execute(
        "INSERT INTO proctor_logs (id, student_id, event_type, message, level, ts) VALUES (?, ?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), data["student_id"], data["event_type"], data["message"], data.get("level", "info"), datetime.utcnow().isoformat()),
    )
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/upload_recording", methods=["POST"])
def api_upload_recording():
    student_id = request.form.get("student_id")
    file = request.files.get("recording")
    if not student_id or not file:
        return jsonify({"ok": False, "error": "missing student_id or recording"}), 400
    filename = f"{student_id}.webm"
    file.save(os.path.join(RECORDINGS_DIR, filename))
    db = get_db()
    db.execute("UPDATE students SET recording_path = ? WHERE id = ?", (filename, student_id))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/submit", methods=["POST"])
def api_submit():
    data = request.get_json(force=True)
    student_id = data["student_id"]
    answers = data.get("answers", {})
    termination_reason = data.get("termination_reason")

    db = get_db()
    student = db.execute("SELECT * FROM students WHERE id = ?", (student_id,)).fetchone()
    if student and student["submitted_at"]:
        return jsonify({"ok": True, "already_submitted": True})

    order_data = json.loads(student["question_order_json"]) if student and student["question_order_json"] else {"order": [], "options": {}}
    option_maps = order_data.get("options", {})

    for question_id, answer in answers.items():
        question = db.execute("SELECT * FROM questions WHERE id = ?", (question_id,)).fetchone()
        if not question:
            continue
        qtype = question["qtype"]
        score, feedback = None, None

        if qtype == "mcq":
            idx_map = option_maps.get(question_id)
            original_options = json.loads(question["options_json"])
            shuffled_index = int(answer)
            original_index = idx_map[shuffled_index] if idx_map else shuffled_index
            correct = json.loads(question["correct_json"])
            is_correct = original_index == correct[0]
            score = 100 if is_correct else 0
            feedback = "Correct" if is_correct else "Incorrect"
            answer_text = original_options[original_index]

        elif qtype == "msq":
            idx_map = option_maps.get(question_id)
            original_options = json.loads(question["options_json"])
            shuffled_indices = answer if isinstance(answer, list) else []
            original_indices = [idx_map[i] if idx_map else i for i in shuffled_indices]
            correct = json.loads(question["correct_json"])
            matched = len(set(original_indices) & set(correct))
            wrong = len(set(original_indices) - set(correct))
            total_correct = len(correct) or 1
            score = round(max(0, (matched - wrong) / total_correct * 100))
            feedback = f"{matched}/{len(correct)} correct selected" + (f", {wrong} incorrect" if wrong else "")
            answer_text = ", ".join(original_options[i] for i in original_indices) if original_indices else "(no selection)"

        else:
            answer_text = str(answer)
            score, feedback = grade_code_answer(question["prompt"], answer_text)

        db.execute(
            "INSERT INTO responses (id, student_id, question_id, answer_text, score, feedback) VALUES (?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), student_id, question_id, answer_text, score, feedback),
        )

    db.execute(
        "UPDATE students SET submitted_at = ?, ended_reason = ? WHERE id = ?",
        (datetime.utcnow().isoformat(), termination_reason, student_id),
    )
    db.commit()
    return jsonify({"ok": True})


if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)