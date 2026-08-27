# GiftAbled Assessment Platform

A proctored assessment platform for GiftAbled: admin creates topic + difficulty based
assignments (AI-generated MCQ/MSQ/coding questions via Groq), students log in with
admin-issued credentials, take the test under webcam+mic monitoring, and every result,
score, and recording is available to the admin — with Excel export.

## What's included

- **GiftAbled branding** — logo and yellow/white theme applied across every page.
- **Admin panel** (`/admin`) — login, create assignments (topic + difficulty + question
  counts), generate student credentials, view results, drill into any student's answers,
  proctoring log, and recorded video.
- **AI question generation** — Groq (`openai/gpt-oss-120b`) generates MCQ, MSQ, and
  coding questions for any topic, at Easy / Medium / Hard difficulty as chosen by the admin.
- **Student credentials** — admin pastes "Name, email" (or just email) per line on the
  assignment's "Manage students" page; the system generates a username + password per
  student and can email credentials automatically (see SMTP setup below). Already-added
  students (matched by email or name) are skipped automatically — no duplicate accounts.
- **Pre-test instructions screen** — after logging in, students see exactly how many MCQ,
  MSQ, and coding questions are in the test, the difficulty, the time limit, and the
  session rules — and must tick "I have read and understood" before the Start button
  becomes clickable.
- **Time limit + countdown** — admin sets a time limit per assignment; students see it
  before starting, and the test auto-submits when time runs out.
- **Proctoring with auto-termination** — webcam + microphone monitoring (face detection,
  tab-switch, copy/paste blocking, background noise/talking detection). Any of these
  triggers an **immediate automatic submission** of whatever the student has answered so
  far, ending the test on the spot — not just a logged warning.
- **Per-student shuffling** — each student gets a randomized question order and randomized
  MCQ/MSQ option order; scoring correctly maps back to the real answer regardless of shuffle.
- **Auto-grading with exact marks** — MCQ/MSQ graded instantly (MSQ gives partial credit);
  code graded by Groq. Every score is shown as exact marks (e.g. "7/10 (70%)"), not just a
  percentage, throughout the admin views and Excel export.
- **Post-test report for students** — after submitting, students land on a results page
  showing their exact score, a question-by-question breakdown (their answer vs. the correct
  one, with feedback), and an AI-generated "areas to improve" summary based on what they
  got wrong.
- **Excel export** — both the student credential list and the full results sheet (name,
  username, timestamps, marks, auto-termination reason, proctoring flag count, recording
  status) download as `.xlsx` from the admin side.
- **One submission per student** — logging in again after submitting redirects straight to
  their results report instead of letting them retake it.

## 1. Local setup

```bash
cd proctor-platform
pip install -r requirements.txt          # add --break-system-packages on Linux if needed

# Windows PowerShell:
$env:GROQ_API_KEY="your-groq-key-here"
$env:ADMIN_USERNAME="admin"
$env:ADMIN_PASSWORD="your-strong-password"
$env:SECRET_KEY="any-random-string"

# Mac/Linux:
export GROQ_API_KEY="your-groq-key-here"
export ADMIN_USERNAME="admin"
export ADMIN_PASSWORD="your-strong-password"
export SECRET_KEY="any-random-string"

python app.py
```

Open **http://localhost:5000/admin/login**, log in, create an assignment, go to
"Manage students & credentials", paste some names, and copy one of the generated
username/password pairs. Open **http://localhost:5000/login** in an incognito window
to test the student side.

### Optional: automatic credential emails

To have the platform email login credentials to students directly, set these
environment variables before running (works with Gmail using an "app password",
or any SMTP provider):

```bash
export SMTP_HOST="smtp.gmail.com"
export SMTP_PORT="587"
export SMTP_USER="your-email@gmail.com"
export SMTP_PASSWORD="your-app-password"
export FROM_EMAIL="your-email@gmail.com"
```

If these aren't set, the "Email login credentials automatically" checkbox on the
Manage Students page is unchecked by default and emails simply aren't sent — credentials
still generate normally and you can share them manually or via the Excel download.

## 2. Deploying updates to an already-live Render deployment

If you've already deployed this to Render from a GitHub repo:

```bash
git add .
git commit -m "Add branding, credentials, instructions, difficulty, Excel export"
git push
```

Render auto-redeploys on every push if you connected it via GitHub. Watch the "Events"
tab in your Render dashboard to confirm the new deploy goes live. If you set up manual
deploys instead, click "Manual Deploy" → "Deploy latest commit" in the dashboard.

**Environment variables to double check are still set on Render:** `GROQ_API_KEY`,
`ADMIN_USERNAME`, `ADMIN_PASSWORD`, `SECRET_KEY`.

## 3. Known limitations (be upfront about these with your client)

- **Auto-termination is intentionally strict** — a single tab switch, copy/paste attempt,
  or two consecutive multiple-face detections ends the test immediately. The microphone
  noise threshold is a reasonable default but not perfectly tuned — a loud room or a
  sensitive mic could trigger a false positive. If GiftAbled finds it too strict/lenient
  in practice, the thresholds are easy to adjust (search for `endTest(` and the `rms > 0.15`
  line in `templates/exam_session.html`).
- Student passwords are stored hashed for login, but the plaintext password is also kept
  in the database so the admin can view/export it for distribution. This is a reasonable
  trade-off for a small-scale assessment tool, but isn't bank-grade security — don't reuse
  these passwords anywhere sensitive.
- Recordings are stored as local `.webm` files on the server's disk, not cloud storage —
  fine for a demo or small deployment; move to S3/managed storage before scaling up.
- **Consent matters** — students are recorded with audio and video. Make sure GiftAbled
  tells students clearly, before the camera turns on, that they're being recorded and what
  happens to the footage. The instructions screen mentions this, but a real consent policy
  should back it up.
- No email/SMS notifications on flags — admin has to check the dashboard.
- SQLite is fine for testing/small batches — move to Postgres before scaling to 200+
  concurrent students.
