# ProDAP — Agent Build Prompt (v2)

This document **supersedes** `PRODAP_AGENT_BUILD_PROMPT.md` (v1) for build
purposes. It keeps every non-negotiable decision from v1 intact and adds a
small number of changes justified by benchmarking against South Korea
(KONEPS), Japan, China, Ukraine (ProZorro/Dozorro), Rwanda (Umucyo), and
Nigeria's own federal NOCOPO portal — plus a 2026 accessibility audit of
Nigerian government websites. See Section 0 for a changelog. Everything not
listed there is unchanged from v1 — read v1 in full; this document only
states deltas plus the sections that changed.

Paste this entire document into Claude Code, Codex, or any coding agent as
the initial instruction, alongside v1 for the sections not repeated here.

---

## 0. Changelog from v1 — what changed and why

| # | Change | Why (evidence) |
|---|---|---|
| 1 | Added **Section 5B: interoperability / open data export** (OCDS-shaped JSON + CSV export, no login required) as an MVP architecture decision, not a Phase 2 feature. | Rwanda's Umucyo and Nigeria's own NOCOPO both publish in the Open Contracting Data Standard. Retrofitting a data standard after launch is a rewrite; designing field names to map onto it from day one is nearly free. Doing this now means a university's ProDAP instance could someday feed NOCOPO without a schema change. |
| 2 | Added **publication SLA** to Section 4 (status changes must be visible on the public dashboard within minutes, not batch-published later). | Japan's e-procurement law hard-codes a same/next-business-day disclosure window. ProDAP v1 never stated a freshness guarantee — "audit trail exists" and "audit trail is visible promptly" are different promises. |
| 3 | Promoted **basic accessibility (contrast toggle, font-size toggle, English/Pidgin toggle, low-data mode)** from unaddressed to **Phase 1 MVP**, in Section 7B. | A 2026 audit found zero major Nigerian government websites implement any baseline accessibility feature — this is currently a near-zero-cost, high-differentiation gap (the audit calls contrast/font support "four lines of CSS"). Given the target users are explicitly "students, staff, journalists, the public" on low-cost Android phones on 2G/3G paying per-MB, this is core usability, not polish. |
| 4 | **Re-prioritized Phase 2** (Section 4 Phase 2 list is now ordered, not flat) — citizen feedback/flagging moved to the top of Phase 2, with a note that it is the highest-leverage single feature in this category once Phase 1 ships. | Ukraine's Dozorro (citizen flagging layer on top of ProZorro) is the most credible evidence in this space: ~$6B saved, and a USAID survey found corruption-incidence reports nearly halved (54% → 29%) among users who'd used the system vs. the old paper process. v1 buried this at the bottom of an unordered deferred list with no signal that it matters more than, say, embeddable badges. |
| 5 | Added **rule-based cost-outlier flag** (not AI/ML) as a named, scoped-down Phase 2 item, replacing the vague absence of any price-intelligence concept. | NOCOPO's own reported ₦173bn (H1 2025) savings are attributed to "price intelligence" — flagging costs that look inflated versus comparable procurements. A full ML anomaly-detection system is out of scope for a university-scale MVP, but a simple "this awarded cost is N% above the median for this procurement method/department" flag is cheap and directly evidences the same mechanism. Explicitly scoped as rule-based, not AI, to avoid overbuilding. |
| 6 | Added a short **Section 3.6: audit-trail-as-control, not just record-keeping**. | China's 2026 draft procurement law reform frames full digital documentation as *the* anti-corruption control (reducing human discretion), not a side effect of good software practice. This reframes v1's existing audit-trail rule (already correct) as the platform's central integrity mechanism worth defending against any future "just let admins edit status directly" shortcut request. |

Everything else in v1 — the two-tier access model, database-per-tenant
target, law-profile-as-data principle, MVP schema, tech stack, and the rest
of the Phase 2 deferral list — stands unchanged. Do **not** reopen those
decisions.

---

## 3.6. Audit trail as the integrity control (new, read alongside v1 §3.5)

Treat the append-only `status_updates` log not as a nice-to-have audit
feature but as the platform's core anti-corruption mechanism — the same
logic China's 2026 procurement-law reform is built on: digitizing the full
process and removing discretionary, undocumented edits is itself the
control, not a side effect of having good software. Concretely:

- There must be **no code path**, including admin tooling or the Django
  admin panel, that can change `procurement_records.status` without writing
  a `status_updates` row. If Django admin is used for staff convenience
  (per v1 §6), the `status` field must be read-only there, or routed through
  the same service function as the regular UI.
- This is a constraint to defend later, not just implement once — if a
  future request asks for a "quick fix" that edits status directly (e.g., a
  data-correction script), it must still go through the audited service
  function, or explicitly log why it didn't.

---

## 4. MVP scope (revised Phase 2 ordering — read alongside v1 §4)

Phase 1 (MVP) is **unchanged from v1** except for the two additions folded
into Section 5B and Section 7B below (open data export, accessibility) —
both are cheap enough to build alongside the rest of Phase 1 and are
justified above.

### Phase 2 (still explicitly deferred — do not build unless asked) — now ordered by evidence-backed priority, highest first:

1. **Citizen feedback/flagging tied to individual projects** — was last in
   v1's list, now first. This is the single highest-leverage feature this
   category has evidence for (Ukraine's Dozorro). Even a minimal version —
   a public "flag this record as suspicious" button with an optional note,
   visible count on the record, no moderation workflow yet — captures most
   of the value.
2. **Rule-based cost-outlier flag** for admins (new item, replacing
   nothing) — e.g., "this awarded cost is >X% above the median for this
   procurement method within this department" shown as a badge on the
   admin list view. Explicitly rule-based/statistical, not ML — do not
   build a fraud-detection model for a university-scale MVP.
3. Multi-step approval/sign-off workflow (council/bursar chain)
4. Vendor/contractor self-service portal (tender applications,
   prequalification)
5. Photo-evidence / geotagged status updates
6. Multi-tenant self-service onboarding UI (the control plane for adding
   new universities)
7. Embeddable badge/widget for external university homepages
8. Additional state law profiles beyond the first federal one
9. Full multi-language support beyond the Phase 1 English/Pidgin toggle
   (Yoruba, Hausa, Igbo), and machine-translated plain-language mode

If you find yourself building anything in this list without being
explicitly asked, stop and flag it instead of proceeding — same rule as v1.

---

## 5B. Interoperability / open data export (new — Phase 1 MVP)

Add one read-only, no-auth endpoint that exports the public-safe fields of
`procurement_records` (i.e., everything already visible on the public
dashboard, nothing more) as:

- JSON, with field names chosen to map cleanly onto **OCDS** (Open
  Contracting Data Standard) concepts where they exist — e.g. this
  project's `title`/`status`/`estimated_cost`/`awarded_cost` should be
  named and shaped so a future mapping layer to OCDS's `tender`/`award`
  release objects is a translation, not a re-architecture. You do not need
  to implement full OCDS compliance for MVP — just avoid field names or
  shapes that would make it hard later.
- CSV, for the same fields, for non-technical users (journalists,
  researchers) who won't consume an API.

This is an architecture decision, not a UI feature — it costs almost
nothing to do now (name fields sensibly, add one export view) and is
expensive to retrofit once a schema is live with real data. Nigeria's own
federal NOCOPO portal and Rwanda's Umucyo both publish in OCDS; this keeps
ProDAP compatible with that ecosystem from day one.

Also add a stated **publication SLA**: a status change written to
`status_updates` must be reflected on the public dashboard and the export
endpoint within minutes (i.e., no overnight batch job, no manual publish
step) — modeled on Japan's same/next-business-day disclosure requirement,
tightened because this is a live web app, not a bulletin.

---

## 7B. Accessibility and low-bandwidth UX (new — Phase 1 MVP, read alongside v1 §7)

A 2026 audit of major Nigerian government websites found none implement
even baseline accessibility, and most Nigerian mobile users pay per-MB on
2G/3G connections. Given ProDAP's public dashboard is explicitly for
"students, staff, journalists, the public" with no login barrier, treat the
following as MVP requirements, not later polish:

- **High-contrast toggle** and **font-size toggle** on the public dashboard
  (a persisted user preference, e.g. cookie/localStorage — trivial CSS
  variable swap, not a redesign).
- **English/Pidgin toggle** for the public dashboard's UI strings (labels,
  buttons, status names) — ship with these two; do not attempt Yoruba,
  Hausa, or Igbo for MVP (that's Phase 2 item 9 above, since it needs real
  translation review, not just more toggle code).
- **Low-data mode**: the public dashboard's default page weight should be
  budgeted (no unbounced hero images, no client-side JS framework bundle
  if server-rendered templates are used per v1 §6) — target a first-view
  page under ~300KB. State this as an explicit test: load the dashboard on
  a throttled "Slow 3G" browser profile before calling Phase 1 done.
- These are additive to v1's existing UI spec (search, filters, status
  badges, detail timeline) — none of v1's Section 7 requirements change.

---

## Definition of done for this pass (revised — read alongside v1 §10)

All of v1 §10 still applies, plus:

- The open-data export endpoint (JSON + CSV) works with zero authentication
  and returns the seeded sample data.
- The public dashboard has working contrast, font-size, and English/Pidgin
  toggles, and loads under ~300KB on a throttled connection.
- A status change made in the procurement office backend appears on the
  public dashboard and in the export endpoint within minutes, with no
  manual publish step.
- README is updated to reflect this v2 document's Phase 2 re-prioritization
  (citizen flagging is now explicitly called out as the top Phase 2
  candidate, not just one item among many).

When complete, report the same things v1 §10 asks for, plus: confirm the
open-data export was tested, and confirm the accessibility toggles were
verified visually, not just implemented.
