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
venv\Scripts\python manage.py seed_users          # local-only demo accounts, one per role
venv\Scripts\python manage.py seed_law_profiles   # federal PPA 2007 profile + threshold rules (needs seed_users first)
venv\Scripts\python manage.py seed_sample_data    # 10 sample procurement records
venv\Scripts\python manage.py seed_foundation_demo # one demo plan walked end-to-end to a record
```

`seed_users` prints the local login credentials to the console. They are
obviously fake and **must be changed before any real deployment**. Run it
*before* `seed_law_profiles` — the threshold-rule seeding needs a
superuser to attribute the rules to.

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

## Deploying for testing access (free forever, no server admin)

This pairs Render's free web-service tier (genuinely free forever, sleeps
after 15 min idle, wakes on the next request) with Neon's free Postgres
tier (also free forever, no 90-day expiry — unlike Render's *own* free
Postgres, which does expire after 90 days; this setup avoids that trap
entirely by keeping the database on Neon).

1. **Database — Neon**: sign up free at neon.tech, create a project, and
   copy the connection string it gives you (starts `postgresql://...`,
   already includes `?sslmode=require`).
2. **App — Render**: sign up free at render.com, connect this GitHub repo,
   and choose **New → Blueprint** — Render will read `render.yaml` from
   the repo root and configure the service automatically.
3. When prompted for the `DATABASE_URL` environment variable (marked
   `sync: false` in the blueprint, so Render asks rather than guessing),
   paste the Neon connection string from step 1.
4. Deploy. The build step runs migrations and all four seed commands
   automatically (they're idempotent — safe to re-run on every deploy).
5. **Get the staff login credentials**: open the deploy's build logs in
   the Render dashboard and look for the `seed_users` output — it prints
   a freshly generated `admin` and `officer` password, shown only once.
   Save them from the log; there's no other way to retrieve them.

This is a testing/demo deployment, not a hardened production one — see
`PRODAP_AGENT_BUILD_PROMPT_V2.md` for the Oracle Cloud Always Free
self-hosting path recommended once this is headed toward a real
university deployment rather than just testing access.

## What's implemented (Phase 1 / MVP)

- Public transparency dashboard: search (title/vendor), filter (status,
  budget source), project list, project detail with full status-history
  timeline, and summary stats (active project count, total contract value).
  Zero authentication required, verified via direct HTTP checks.
- Procurement office backend: login (role-based access — see Phase
  1-Foundation below for the full five-role model), create/edit records,
  and a status-transition action that always
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

## What's implemented (Phase 1-Foundation: statutory e-procurement layer)

Building on a legal/technical blueprint from the project owner mapping the
platform to the Nigerian Public Procurement Act 2007's statutory workflow,
this adds the first of five phases converting ProDAP from a pure
disclosure register into a genuine e-procurement *transaction* system —
starting with the piece everything else depends on: a `ProcurementRecord`
can no longer be created out of thin air.

- **Annual procurement plans**: a `ProcurementPlan` per financial year,
  made up of `PlanLine` items proposed by a Requesting Unit. Only an
  approved plan line may initiate a procurement — approving the plan
  bulk-approves its lines; a line added after approval (an amendment)
  needs its own individual approval.
- **Requisitions with a funds-confirmation gate**: a `Requisition` can only
  be created against an *approved* plan line, and is blocked from
  producing a procurement record until Finance has confirmed funds are
  available — at which point a race-safe, unique process identifier is
  generated (`{LAW-PROFILE}-{FINANCIAL-YEAR}-{00001}`).
- **Anti-splitting review**: a required, written packaging-review note
  before a procurement method can be determined — deliberately a human
  decision aided by a read-only list of similar recent requisitions in the
  same department, not automated pattern detection (that's a later phase).
- **Versioned threshold/method rule engine**: `ThresholdRule` rows replace
  treating the law profile's flat JSON thresholds as canonical — each rule
  is effective-dated and never mutated, only superseded, so historical
  determinations stay reconstructable.
- **Real role separation, enforced twice**: five roles (`requesting_unit`,
  `procurement_unit`, `finance`, `accounting_officer`, `admin`) gate which
  screens a user can reach, and the service layer separately refuses to
  let anyone approve or confirm their own request — a short-staffed unit
  assigning one person two roles doesn't bypass this.
- **A second, generic audit log** (`AuditEvent`) for every gate crossing
  (plan approval, funds confirmation, packaging review, method
  determination, record creation) — kept deliberately separate from the
  existing `StatusUpdate` trail rather than merging them, since
  `StatusUpdate` already does its one job correctly.

## What's implemented (blueprint Phase 2 — Competition, non-cryptographic slice)

The first piece of the blueprint's Competition phase: a `ProcurementRecord`
can no longer be manually flipped from `Planning` to `Advertised` with
nothing but a text note. That transition is now evidence-derived.

- **Solicitation preparation**: a `Solicitation` (eligibility criteria,
  scope/specifications, evaluation criteria, optional bid-security
  requirement) prepared by the Procurement Unit for a record still in
  `Planning`, versioned and append-only — a rejected solicitation is
  superseded by a new version, never mutated.
- **Solicitation approval, with separation of duties**: the Accounting
  Officer approves or rejects — the preparer cannot approve their own
  solicitation, same enforcement pattern as plan/requisition approval.
- **Advertisement/publication**: publishing an approved solicitation
  (channels used, publication proof, closing date) is what actually moves
  the record to `Advertised`, inside the same transaction as the existing
  `transition_status()` call — so `StatusUpdate` stays the single source of
  truth for status history. The closing date must respect a configurable
  institutional minimum bidding period (`LawProfile.
  default_minimum_bidding_days`) — explicitly documented as a policy
  placeholder, not a verified statutory day-count, since the source
  blueprint doesn't supply a complete method-by-method table the way it
  does for approval thresholds.
- **Public disclosure**: once published, eligibility, scope, evaluation
  criteria, bid-security terms, and the closing date appear on the public
  project page — matching the blueprint's own disclosure boundary (notice/
  criteria/dates are public; bid *contents*, not built yet, stay
  protected).

**Explicitly not yet built** (the rest of blueprint Phases 2–5): encrypted
electronic bid submission/opening, prequalification/EOI, clarifications and
addenda, evaluation committees, Tenders Board approval routing, BPP
Certificate of No Objection, contract/milestone/payment management,
NOCOPO/OCDS export, audit analytics. Prequalification and clarifications
both have an obvious extension point (an FK to `Solicitation`) without
needing to restructure anything built so far. Record `status` for every
other transition (`Advertised` → `Tendering` → `Awarded` → ...) still moves
manually via the existing staff status-transition screen — evidence-gating
those states isn't honestly buildable until the evaluation/approval
machinery above exists.

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
