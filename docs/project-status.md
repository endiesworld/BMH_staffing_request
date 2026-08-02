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

## 7. Recommended next step — build the first `servicing` slice
**Design is settled: see ADR-010** (request type, state representation, transitions, review outcome,
scope). Nothing is built yet. Build order:

1. **`RequestType`** model — `name` (unique) + `required_sector` (reuses
   `PersonnelProfile.SectorCategory`). A reference table, admin-registered.
2. **`ServiceRequest`** model — `client` FK (**`on_delete=PROTECT`**), `request_type` FK, `title`,
   `description`, `status` (`TextChoices`, default `SUBMITTED`), `reviewed_by` / `reviewed_at` /
   `rejection_reason` (nullable), `created_at`, `updated_at`.
3. **`CheckConstraint`** tying the review fields to `status` (ADR-010 D4) — the invariant that keeps
   three nullable columns honest.
4. **`servicing/services.py`** — the transition map + `submit_request` / `approve_request` /
   `reject_request`. Each: `transaction.atomic()` → `select_for_update()` → validate transition →
   write `status` → write the audit row.
5. **Tests** — happy paths, an illegal transition, and the constraint firing.

**Out of scope this slice (deliberate):** `Assignment` and its own state machine; coordinator↔type
routing; the audit app's final schema.

## 8. Key files
```
manage.py
config/                 settings.py (AUTH_USER_MODEL='accounts.User'), urls.py, wsgi.py, asgi.py
accounts/               models.py, services.py, admin.py, tests.py, migrations/
docs/interview-prep-brief.md   ← the WHY: ADR-001…009, workflow, state machine
docs/project-status.md         ← this file: the WHAT / how to resume
```
