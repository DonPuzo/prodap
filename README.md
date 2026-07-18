# ProDAP — Procurement Digital Application Platform

ProDAP is a standalone public procurement transparency platform for
Nigerian universities, modeled conceptually on the federal NOCOPO portal
but built as a hosted, per-university instance. Anyone — students, staff,
journalists, the public — can browse a university's procurement records
with no login required, while authorized procurement staff log in to
create and manage records through their full lifecycle (planning →
advertised → tendering → awarded → implementation → completed/abandoned),
with every status change permanently logged in an audit trail. See
`PRODAP_AGENT_BUILD_PROMPT.md` and `PRODAP_AGENT_BUILD_PROMPT_V2.md` for
the full product spec and the reasoning behind it.

## Setup

### 1. Prerequisites

- Python 3.12+ (tested on 3.14)
- PostgreSQL (tested on 18), running locally

### 2. Create the database and role

Using `psql` or pgAdmin, connected as a postgres superuser:

```sql
CREATE DATABASE prodap;
CREATE USER prodap_user WITH PASSWORD 'your-local-dev-password';
GRANT ALL PRIVILEGES ON DATABASE prodap TO prodap_user;
ALTER DATABASE prodap OWNER TO prodap_user;
ALTER USER prodap_user CREATEDB;  -- needed so `manage.py test` can spin up a test database
```

### 3. Environment

```bash
cp .env.example .env
# edit .env: set DB_PASSWORD to match what you chose above,
# and generate a real SECRET_KEY for anything beyond local dev.
```

### 4. Install dependencies

```bash
python -m venv venv
venv\Scripts\pip install -r requirements.txt      # Windows
# source venv/bin/activate && pip install -r requirements.txt   # macOS/Linux
```

### 5. Migrate and seed

```bash
venv\Scripts\python manage.py migrate
venv\Scripts\python manage.py seed_law_profiles   # federal PPA 2007 profile
venv\Scripts\python manage.py seed_users          # local-only admin + officer accounts
venv\Scripts\python manage.py seed_sample_data    # 10 sample procurement records
```

`seed_users` prints the local login credentials to the console. They are
obviously fake and **must be changed before any real deployment**.

### 6. Run

```bash
venv\Scripts\python manage.py runserver
```

- Public dashboard: http://127.0.0.1:8000/ — no login required.
- Staff login: http://127.0.0.1:8000/staff/login/
- Django admin: http://127.0.0.1:8000/admin/
- Open data export: `/export/data.json` and `/export/data.csv`

### 7. Run tests

```bash
venv\Scripts\python manage.py test procurement
```

Covers: the audit trail can't be bypassed or left partial, law-profile
validation rejects out-of-profile procurement methods, the cost-outlier
math, citizen-flag session deduplication, and that every public view stays
accessible with zero auth while every staff view correctly requires login.

## What's implemented (Phase 1 / MVP)

- Public transparency dashboard: search (title/vendor), filter (status,
  budget source), project list, project detail with full status-history
  timeline, and summary stats (active project count, total contract value).
  Zero authentication required, verified via direct HTTP checks.
- Procurement office backend: login (`procurement_officer` / `admin`
  roles), create/edit records, and a status-transition action that always
  requires a note and writes an immutable `status_updates` row in the same
  transaction as the status change — there is no code path, including the
  Django admin, that can change status without it.
- One fully-populated law profile (federal PPA 2007) driving the
  `procurement_method` choices on the record form — not a hardcoded
  dropdown. The law-profile system is built to take additional profiles as
  data, with no code change required.
- Open data export (JSON + CSV) of the same public-safe fields shown on
  the dashboard, field-named to map cleanly onto the Open Contracting Data
  Standard used by Nigeria's own NOCOPO portal and Rwanda's Umucyo system.
- Baseline accessibility: high-contrast toggle, text-size toggle, and an
  English/Nigerian-Pidgin UI language toggle, all persisted client-side.
  Page weight is kept small (no JS framework, no external fonts/images) for
  users on slow or metered mobile connections.
- 10 seeded sample procurement records spanning every status, including one
  `Abandoned` project with a multi-step status history, and a Faculty of
  Science / Request for Quotations cluster that demonstrates the
  cost-outlier flag (see below).

## What's implemented (Phase 2, started)

- **Citizen feedback/flagging** (Phase 2 item 1 — the highest-evidenced
  anti-corruption feature in this category, per Ukraine's Dozorro): a
  public "flag this project as concerning" button on every project detail
  page, with an optional note. No login required. Deliberately minimal —
  no moderation queue, no status workflow, just a visible public count
  (capped at one flag per browser session per project to keep counts
  meaningful without building real rate-limiting). Flag counts are also
  surfaced to procurement office staff on the record list and in the
  Django admin, so scrutiny is visible to the people responsible for the
  project, not routed into a queue nobody reads.
- **Rule-based cost-outlier flag** (Phase 2 item 2, `ProcurementRecord.
  is_cost_outlier()`): flags a record when its cost is ≥25% above the
  median awarded cost for the same procurement method + department.
  Explicitly rule-based, not ML — a plain median comparison, chosen to
  stay explainable and cheap at university scale (mirrors the mechanism
  behind NOCOPO's reported ₦173bn savings via "price intelligence").
  Surfaced as a badge on the staff record list and as a column in the
  Django admin; not shown on the public dashboard (Phase 2 scope was
  "shown to admins" — the public already has citizen flagging).

## What's explicitly deferred (Phase 2 remainder — do not build without being asked)

In roughly the priority order suggested by comparable systems elsewhere
(see `PRODAP_AGENT_BUILD_PROMPT_V2.md` section 0 for the evidence behind
this ordering):

1. Multi-step approval/sign-off workflow (council/bursar chain).
2. Vendor/contractor self-service portal (tender applications,
   prequalification).
3. Photo-evidence / geotagged status updates.
4. Multi-tenant self-service onboarding UI (the control plane for adding
   new universities).
5. Embeddable badge/widget for external university homepages.
6. Additional state law profiles beyond the first federal one.
7. Full multi-language support (Yoruba, Hausa, Igbo) via real translation,
   beyond the Phase 1 English/Pidgin toggle.

## Tech stack

Django 6 + PostgreSQL + server-rendered Django templates, session-based
auth with role-based access control. See `PRODAP_AGENT_BUILD_PROMPT_V2.md`
for the free-forever hosting comparison (Oracle Cloud Always Free
recommended over Render/Railway, which have free-tier expiry traps).
