# BMH Service Hub — Project Status & Resume Guide

> **Purpose:** the "start here" document to resume work in a new session. It captures *what
> exists*, *how to run it*, and *what's next*. The **reasoning** behind every decision lives in
> [`interview-prep-brief.md`](./interview-prep-brief.md) as ADR-001 … ADR-010 — read that for *why*.
>
> **Last updated:** 2026-08-02

---

## 1. How we work (ground rules)
- **Hands-on:** the developer writes the code; the assistant teaches, reasons, and reviews. No
  commands are run and no project code is written unless explicitly asked.
- **Reason-first:** every step surfaces design options and trade-offs before code.
- **Explanations:** plain-language grounding first, *then* the domain jargon (to build vocabulary).
- Decisions get logged as **ADRs** (Architecture Decision Records) in the prep brief.

## 2. What the project is
A coordinator-facing Django app that receives a service request, validates it, routes it through an
internal workflow, and records every action. Three authenticating roles: **Client**, **Coordinator**,
**Personnel**. Designed *as-if* HIPAA applies (synthetic data; technical safeguards only).
Full workflow and state machine: see brief §2 and §6.

## 3. Tech stack & how to run
- **Python** ≥ 3.12, **Django** 5.2.x, dependency/runtime manager **`uv`**.
- **DB:** SQLite in dev (`db.sqlite3`, gitignored); **Postgres** planned for deployment (ADR-005).
- Every command runs through `uv run`:

| Task | Command |
|---|---|
| Run migrations | `uv run python manage.py migrate` |
| Make migrations | `uv run python manage.py makemigrations accounts` |
| Run the test suite | `uv run python manage.py test accounts` |
| Start dev server | `uv run python manage.py runserver` → `http://127.0.0.1:8000/admin/` |
| Create an admin user | `uv run python manage.py createsuperuser` (prompts for **email**, not username) |
| System checks | `uv run python manage.py check` |
| Empty migration to hand-write | `uv run python manage.py makemigrations --empty <app>` |
| List migrations + applied state | `uv run python manage.py showmigrations` |

### Migrations come in two kinds
`makemigrations` reads **`models.py`** — never views, forms, admin or templates. It diffs the models
against the state recorded in previous migrations and writes the difference.

| | **Schema** migration | **Data** migration |
|---|---|---|
| Changes | the table *structure* | the *rows* |
| Operations | `CreateModel`, `AddField`, `AlterField` | `RunPython` |
| Written by | `makemigrations` | **by hand** |
| Why | Django can diff your models | the intent exists nowhere in `models.py`, so Django cannot guess it |

**Rule of thumb:** hand-write a migration when what you want isn't derivable from the models — seed
vocabulary, groups/permissions, backfilling a new column, reshaping existing rows.

**This project's six:**
```
accounts/  0001_initial                  auto   User table
           0002_clientprofile_...        auto   the three profile tables
           0003_coordinator_group        HAND   COORDINATOR group + perms; backfills coordinators
servicing/ 0001_initial                  auto   RequestType table
           0002_seed_request_types       HAND   inserts the four request types
           0003_servicerequest           auto   ServiceRequest table + 2 CheckConstraints
```

**Four things that bite when hand-writing one:**
1. **`apps.get_model("app", "Model")`, never a direct import.** Migrations run against the *historical*
   model state; a direct import binds to today's model and breaks the migration the moment a field is
   added. This is the most common way data migrations rot.
2. **Always supply a reverse function** to `RunPython`, or rollbacks are blocked. Reverse narrowly —
   `0002` deletes only the four seeded codes, so admin-added types survive a rollback.
3. **Permissions don't exist yet mid-migration.** They're created by a `post_migrate` signal that fires
   *after* all migrations finish, so a migration granting permissions must create them early —
   see `accounts/0003`'s `_ensure_permissions_exist()`.
4. **Data migrations also run when building the test database**, so seeded rows are present in tests.
   Have tests create their own fixtures rather than depending on seed data.

## 4. Architecture (apps) — ADR-007
```
accounts/   ✅ DONE   custom User, roles, per-role profiles, creation service, admin
servicing/  ⬜ TODO   design DECIDED (ADR-010), not built. The whole request lifecycle
                      (ServiceRequest + review + Assignment
                      + recommendation). Internally modular ("modular monolith within an app").
audit/      ⬜ TODO   append-only AuditEvent log (cross-cutting)
```
Dependency direction (a clean DAG, no cycles): `servicing → accounts`; `audit` minimal.
Rule for splitting apps: **coupling/cohesion, not category.**

## 5. What's built — the `accounts` app (functionally complete)

**Custom user & profiles** (`accounts/models.py`) — ADR-006, ADR-009:
- `User(AbstractUser)`: `username = None`, **email is the login** (`USERNAME_FIELD = "email"`,
  unique), nested `User.Role` enum (`CLIENT / COORDINATOR / PERSONNEL`, default `CLIENT`), custom
  `UserManager` (email-based `create_user` / `create_superuser`).
- `ClientProfile` (`organization_name`, `phone_number`), `CoordinatorProfile` (`department`,
  `region`), `PersonnelProfile` (`sector` [nested `SectorCategory`], `availability_status` [nested
  `AvailabilityStatus`, default `UNAVAILABLE`]). Each is `OneToOne → settings.AUTH_USER_MODEL`,
  `on_delete=CASCADE`, with a `related_name` and a `__str__`.
- **Invariant:** every participant has exactly one matching profile
  (Client→ClientProfile, etc.). Superusers/staff are exempt (a superuser is `role=CLIENT`, no profile).

**Creation service** (`accounts/services.py`) — ADR-009:
- `create_client(...)`, `create_coordinator(...)`, `create_personnel(...)` — each creates the `User`
  **and** its profile inside a single `transaction.atomic()` block (all-or-nothing), returns the `user`.
- This is the **self-registration** path for clients/personnel.

**Tests** (`accounts/tests.py`) — 5 passing:
- client happy-path, client atomic-rollback (mocked failure), personnel default availability,
  coordinator happy-path, coordinator atomic-rollback.

**Admin** (`accounts/admin.py`) — ADR-009 wrinkle resolved:
- `CustomUserAdmin(BaseUserAdmin)` with `CustomUserCreationForm` / `CustomUserChangeForm` (email-based,
  password stays a read-only hash), email-based `fieldsets` / `add_fieldsets`.
- **Role-aware profile inlines**: `get_inlines()` shows only the `StackedInline` matching `obj.role`.
  Provisioning a coordinator is a two-step admin flow (save user → fill profile inline). Atomicity
  comes from the admin's own transaction. Not *forced* (superuser exception).
- Profiles also registered standalone.

**Migrations:** `0001_initial` (User), `0002_...` (the three profiles). Both applied.

## 6. Open / deferred items (with pointers)
- [ ] **Self-registration UI** for client/personnel — services exist; **no forms/views/templates yet**.
- [ ] **Role ↔ Group sync + permissions** — ADR-006. No authorization rules wired. Note: admin-created
      coordinators still need their `COORDINATOR` **group** (profile half is done).
- [ ] **Coordinator ↔ request-type routing** — deferred to the `servicing` app (brief §7); must NOT be a
      field on `CoordinatorProfile` (would create an `accounts → servicing` cycle).
- [ ] **Settings/env hygiene** — ADR-004. Still on dev defaults (`DEBUG=True`, hardcoded `SECRET_KEY`).
- [ ] **Deployment walking skeleton + CI/CD** — ADR-005 (Postgres, Gunicorn, WhiteNoise, health
      endpoint, Redis + Celery worker, K8s homelab, pipeline).

## 7. `servicing` — the review slice (in progress)
Design: **ADR-010** (models, transitions, review outcome) and **ADR-011** (async workflow, fulfilment).

**Built and verified:**
1. ✅ **`RequestType`** — `code` (stable slug identity) + `name` (display) + `required_sector` +
   `is_active`. Migration `0001_initial`.
2. ✅ **Seed data migration** `0002_seed_request_types` — four types, one per `SectorCategory`.
   Bootstrap only; the admin owns the vocabulary from here.
3. ✅ **`ServiceRequest`** + **two `CheckConstraint`s** (review fields ↔ status; rejection reason ↔
   `REJECTED`). Migration `0003_servicerequest`. Constraints probed: all illegal rows refused.
4. ✅ **`servicing/services.py`** — `TRANSITIONS` map, `IllegalTransition`, `_transition`
   (`select_for_update` → check → write → record), `submit_request` / `approve_request` /
   `reject_request`. Probed: legal paths pass, illegal transitions and bad arguments blocked.

**Remaining to ship today:**
5. ⬜ **Tests** (`servicing/tests.py`) — happy paths, illegal transitions, constraints firing.
6. ⬜ **Admin** — `RequestType` with `prepopulated_fields = {"code": ("name",)}` (**never** auto-slug in
   `save()` — a rename would silently change the identifier). `ServiceRequest` read-mostly.
7. ⬜ **Settings/env hygiene** (ADR-004) — `SECRET_KEY`, `DEBUG`, `ALLOWED_HOSTS`, DB from env.

**Decided but deliberately NOT built (ADR-011 D6):** `Assignment` + its state machine, `fulfil_request`
and the scheduled window, notifications (Celery + Redis), coordinator↔type routing, the `audit` app.

**Known seam:** `_record_transition()` currently writes an INFO log line, not an `AuditEvent` row —
history is degraded, not discarded. One function body changes when `audit` lands.

**Known limitation:** `select_for_update()` is a **silent no-op on SQLite**; the concurrency protection
in `_transition` only becomes real on Postgres.

## 8. Key files
```
manage.py
config/                 settings.py (AUTH_USER_MODEL='accounts.User'), urls.py, wsgi.py, asgi.py
accounts/               models.py, services.py, admin.py, tests.py, migrations/
docs/interview-prep-brief.md   ← the WHY: ADR-001…009, workflow, state machine
docs/project-status.md         ← this file: the WHAT / how to resume
```
