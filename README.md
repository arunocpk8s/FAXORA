# Facility Tracker

A small internal web app for raising and tracking facility requests (Transport, Security,
Housekeeping, Maintenance, Others). Flask + SQLAlchemy + server-rendered Jinja2 templates.

- **Employees** raise requests and see their own tickets.
- **Agents** see all tickets, assign them, and move them through the workflow.
- SLA due times are set per category (see `sla.py`); urgent tickets get half the time.
- Times are stored in UTC and shown in Asia/Kolkata (IST).
- Emails are sent on create, status change and assignment. Failed emails are kept and can be retried.

## Install

Requires Python 3.10 or newer.

```bash
cd facility-tracker
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

python -m pip install -r requirements.txt
```

> **Locked-down Windows PCs:** if `flask`, `pip` or `pytest` fail with "Access is denied", company
> policy is blocking the `.exe` launchers in `.venv\Scripts`. Run everything through Python instead,
> which is what the commands below do. If `activate` is also blocked, skip it and call
> `.venv\Scripts\python.exe` directly (e.g. `.venv\Scripts\python.exe -m flask --app app run`).

## Configure

Copy the example settings and fill them in:

```bash
cp .env.example .env        # Windows (cmd): copy .env.example .env
```

At minimum set `SECRET_KEY` (a long random string), `SEED_ADMIN_PASSWORD`, `SEED_AGENT_PASSWORD`
and `SEED_EMPLOYEE_PASSWORD` (8+ characters each). Generate a secret key with:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

Settings are read from environment variables; `.env` is loaded automatically and never committed.

## Create the first users

```bash
python seed.py
```

This creates an admin (`admin`), an agent (`agent`) and an employee (`employee`) with the passwords from `.env`.
Running it again leaves existing users unchanged.

## Run

```bash
python -m flask --app app run
```

Open http://localhost:5000 and log in.

## Test

```bash
python -m pytest
```

## Email

Every email is recorded in the `email_outbox` table before sending. If SMTP fails (for example when
running locally without a mail server) the ticket is still saved, a warning is logged, and the email
stays `PENDING`. Retry pending emails with:

```bash
python -m flask --app app resend-failed
```

On a server, schedule this command (cron / Task Scheduler) every 15 minutes or so.
Email bodies contain the ticket number, title, status, due time and a link, never the description.

### Testing email with Gmail

For testing only; switch to the company SMTP server before real use.

1. Turn on 2-Step Verification for the Gmail account, then create an App Password at
   https://myaccount.google.com/apppasswords (a 16-character code). Your normal Gmail password
   will not work.
2. In `.env`:
   ```
   SMTP_HOST=smtp.gmail.com
   SMTP_PORT=587
   SMTP_USE_TLS=true
   SMTP_USER=your.name@gmail.com
   SMTP_PASSWORD=the16charapppassword
   SMTP_FROM=your.name@gmail.com
   ```
3. Restart the app, then check the settings: `python -m flask --app app test-email your.name@gmail.com`
4. Point the seeded users at real inboxes so they receive notifications:
   `python -m flask --app app set-email employee your.name@gmail.com` (same for `agent`).

Gmail limits sending to about 500 emails a day, and some company networks block outgoing port 587.

## Branding

The header shows `APP_NAME` (default `FAXORA`). To show your company logo, put the official file
in `static/` as `logo.svg`, `logo.png` or `logo.jpg` (about 36px tall is displayed) and restart.

## Roles

| Role | Can do |
|---|---|
| employee | Raise requests and SOS; see their own tickets (will sign in with Azure SSO, see `docs/`) |
| agent | Member of one facility team; sees and updates that team's tickets: those assigned to it, and unassigned ones in its category; sees every active SOS; can cancel |
| admin | Sees all tickets; assigns teams; updates and cancels any ticket; Admin console (users, delete, reports, mail) |

## Workflow

`NEW` → (team assigned) → `IN_PROGRESS` ⇄ `ON_HOLD` → `RESOLVED` → `CLOSED`.
A RESOLVED ticket can be reopened (`IN_PROGRESS`). Any open ticket (NEW, IN_PROGRESS, ON_HOLD) can
be **cancelled** (`CANCELLED`) from its page with a reason; the requester is emailed. CLOSED and
CANCELLED are final.

**Assigning:** on the ticket page an admin picks a team (TRANSPORT, SECURITY, HOUSEKEEPING,
MAINTENANCE, MEDICAL, OTHERS). Every active agent in that team and the team mailbox
(`CATEGORY_MAILBOX_<TEAM>`) are emailed, the ticket appears on that team's dashboard, and they can
update its status. A NEW ticket moves to IN_PROGRESS when assigned. Agents also see unassigned
tickets in their own category and can start them; starting work assigns the ticket to their team.
An SOS goes to the Medical team automatically, and every agent and admin is alerted.

**Cancel vs delete:** to stop a real request, cancel it on the ticket page. Deleting is only
possible in the Admin console (Tickets tab) and is meant for test, duplicate or spam tickets. A
deleted ticket disappears from all lists, counts and reports, and its unsent emails are cancelled;
the row and its history stay in the database (with who deleted it, when and why) for audit.

**Dashboard tabs:** staff see **Open | Resolved / closed | Cancelled | All** tabs with counts. The
tabs work together with the other filters, the 60-second refresh and the CSV export.

**Monthly filter and export:** the staff dashboard has a "Month raised" filter (IST months) and an
**Export CSV** button that downloads the current view (agents get their team's tickets only). The
CSV opens in Excel and never includes ticket descriptions.

## Users, teams and departments

Departments are `DPS, SSG, SSI, FSI, CYBER` (change with `DEPARTMENTS` in `.env`, comma-separated).
`python seed.py` creates `admin`, `agent` (Maintenance team) and `employee`. After that, manage
users in the **Admin console** (below). The same tasks are also available as commands:

```bash
# an agent in a team (you are asked for the password)
python -m flask --app app create-user ravi --name "Ravi Kumar" --email ravi@company.com --role agent --team HOUSEKEEPING
# an admin
python -m flask --app app create-user meena --name "Meena R" --email meena@company.com --role admin
# an employee
python -m flask --app app create-user priya --name "Priya S" --email priya@company.com --department DPS
# change someone's role, team or email
python -m flask --app app set-role agent admin
python -m flask --app app set-team ravi TRANSPORT
python -m flask --app app set-email ravi ravi.k@company.com
```

## Admin console

Admins see an **Admin console** link in the header (`/admin`). Other roles get 403.

- **Overview:** active users by role, deactivated users, pending emails, teams with no agent,
  and the last 30 admin actions.
- **Users:** search and filter; add an **agent or admin** (username, name, email, role, team for
  agents, password); edit details, role and team; reset a password; deactivate or reactivate.
  Employees are not created here: they will sign in with Azure SSO (plan in `docs/`). Until then,
  test employees come from `seed.py` or `flask create-user`.
  Users are never deleted, because tickets and history refer to them. Deactivated users are signed
  out at once and cannot log in. You cannot deactivate yourself or remove your own admin role,
  and the last active admin is protected.
- **Tickets:** search by month, status or text; **delete** a ticket with a reason (the only place
  tickets can be deleted); "Show deleted" lists deleted tickets with who deleted them and when.
- **Reports:** pick a month to see tickets raised, resolved, open, cancelled, breached, SLA met %
  and average resolution time, by category and by team; export the summary or all tickets as CSV.
- **Mail settings:** the six team mailboxes, extra SOS recipients and the emergency numbers shown
  on SOS pages. Values saved here override `.env`; an empty mailbox means no team mailbox. A
  "send test email" button checks the mail server. The **mail server** itself (SMTP host, port,
  STARTTLS, login, password, sender address and the FAXORA address used in email links) is
  editable too and applies immediately, without a restart. The password is write-only: it is
  never shown, leaving the field empty keeps it, and it is stored encrypted with a key derived from
  `SECRET_KEY` (if `SECRET_KEY` changes, enter the password again). Anything not saved in the
  console comes from `.env`; "Reset to .env values" forgets the console values.
- **Email outbox:** every email with status, attempts and last error; retry or cancel a pending
  email, or resend all pending. Email bodies are not shown.

Every console action is written to an append-only `admin_events` table. Passwords are never
recorded or logged.

## Medical help and SOS

- **Medical** is a normal category for health help that isn't an emergency (feeling unwell,
  first aid, need to go home). SLA: 2 hours, or 1 hour if urgent.
- **SOS** (red button in the top bar, available to every logged-in user) is for urgent help. The
  person gives their location and, optionally, a phone number and a note. It creates an URGENT
  Medical ticket due in 15 minutes (`SOS_SLA_HOURS` in `sla.py`) and immediately emails all agents,
  `CATEGORY_MAILBOX_MEDICAL` and the addresses in `SOS_ALERT_EMAILS`. The alert includes the
  location and phone number but not the note, so health details stay out of email.
- Agents see active SOS tickets in a red banner at the top of the dashboard and first in the table.
  The dashboard reloads within 60 seconds of a new SOS.
- Pressing SOS again within 10 minutes opens the active SOS instead of creating a duplicate.
- The SOS page always shows `EMERGENCY_NUMBERS`. The app does not replace calling emergency services.
- Medical tickets can contain health information. Only the requester and agents can see them.

## Changing SLAs

Edit `SLA_HOURS` (hours per category) and `PRIORITY_FACTOR` (urgent multiplier) in `sla.py`.
The new values apply to tickets created after the change.

## Test deployment on Vercel

The repository deploys to Vercel with its Flask support: `pyproject.toml` points Vercel at
`wsgi:app` (`wsgi.py`); Vercel installs the packages listed in `pyproject.toml` / `uv.lock`
(keep them in sync with `requirements.txt`, which local installs use).
Local use is unchanged: without `VERCEL` set, it still uses `.env` and the SQLite file.

1. In Vercel: **Add New -> Project**, import this GitHub repository, keep the defaults, deploy.
2. **Storage -> Create database -> Neon (Postgres)** and connect it to the project. This sets
   `DATABASE_URL`. Until it is connected, the app uses a temporary SQLite file in `/tmp` that is
   wiped often.
3. **Settings -> Environment Variables**: add `SECRET_KEY` (64 random hex characters; required,
   the app refuses to start without it on Vercel) and `APP_BASE_URL` (`https://<project>.vercel.app`).
   Optional: `SMTP_*`, `CATEGORY_MAILBOX_*` (or set mail in the Admin console later).
4. **Deployments -> Redeploy** so the new variables are used.
5. Create the first users in the Vercel database from your PC (PowerShell, project folder):
   ```powershell
   $env:DATABASE_URL = "<Neon connection string from Vercel Storage -> .env.local>"
   .venv\Scripts\python.exe seed.py
   Remove-Item Env:DATABASE_URL
   ```
   `seed.py` uses the `SEED_*_PASSWORD` values from your local `.env`.

Notes: cookies are Secure automatically on Vercel; emails go out only when SMTP is configured
(Gmail port 587 works from Vercel; port 25 is blocked there). Scheduled `resend-failed` is not set
up on Vercel; use **Admin console -> Email outbox -> Resend all** instead.

## Deploying later

1. **Server:** run behind a WSGI server instead of `flask run`, e.g.
   `python -m pip install waitress` then `python -m waitress --port=8000 --call app:create_app` (Windows), or
   `pip install gunicorn` then `gunicorn -w 2 "app:create_app()"` (Linux). Put it behind IIS/nginx
   with HTTPS.
2. **HTTPS cookies:** set `SESSION_COOKIE_SECURE=true` once served over HTTPS.
3. **Settings:** set real values for `SECRET_KEY`, `APP_BASE_URL` (the public URL used in email
   links), SMTP settings and category mailboxes in the server environment.
4. **PostgreSQL:** `pip install psycopg[binary]` and set
   `DATABASE_URL=postgresql+psycopg://user:password@host/dbname`. Tables are created on first
   start. For later schema changes, add Flask-Migrate (Alembic).
5. **Seed** once on the server with `python seed.py`.
6. **Backups:** back up `instance/tracker.db` (SQLite) or the PostgreSQL database regularly.
7. **Email retries:** schedule `python -m flask --app app resend-failed`.

## Project layout

```
app.py        app factory, routes, CLI commands
admin.py      admin console: users, tickets (delete), reports, mail settings, outbox
reports.py    CSV exports and monthly summary figures
models.py     User, Ticket, TicketEvent, EmailOutbox, Setting, AdminEvent; status transitions
sla.py        SLA hours, due dates, IST helpers
notify.py     SMTP email sending and retry
auth.py       login, logout, login_required, role_required
seed.py       creates the initial users
config.py     reads environment variables
templates/    Jinja2 templates (admin/ holds the console pages)
static/       style.css, app.js, logo
tests/        pytest tests
docs/         HLD, LLD, runbook, components (Word/PDF) and the technical deck
```
