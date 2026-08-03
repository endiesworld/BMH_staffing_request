# BMH Service Hub — Project Status & Resume Guide

> **Purpose:** the "start here" document to resume work in a new session. It captures *what
> exists*, *how to run it*, and *what's next*. The **reasoning** behind every decision lives in
> [`interview-prep-brief.md`](./interview-prep-brief.md) as ADR-001 … ADR-013 — read that for *why*.
> New here? [`../README.md`](../README.md) covers setup and running it locally.
>
> **Last updated:** 2026-08-02

---

## 1. How we work (ground rules)
- **Hands-on by default:** the developer writes the code; the assistant teaches, reasons, reviews.
  *(Suspended 2026-08-02 under deadline — the assistant wrote the code, one unit at a time, with a
  review after each. Revert to hands-on when the pressure is off.)*
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
| Make migrations | `uv run python manage.py makemigrations <app>` |
| Run the test suite | `uv run python manage.py test` |
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

**This project's eleven:**
```
accounts/  0001_initial                     auto   User table
           0002_clientprofile_...           auto   the three profile tables
           0003_coordinator_group           HAND   COORDINATOR group + perms; backfills coordinators
           0004_coordinator_can_view_...    HAND   adds view_assignment to the group
servicing/ 0001_initial                     auto   RequestType table
           0002_seed_request_types          HAND   inserts the four request types
           0003_servicerequest              auto   ServiceRequest table + 2 CheckConstraints
           0004_request_slot_and_location   HAND   drops title; adds slot, duration, location
           0005_structured_service_address  HAND   splits location into a US address + phone
           0006_assignment                  auto   Assignment + partial unique constraint
           0007_servicerequest_started_...  MIXED  SCHEDULED state + started_at + BACKFILL
```
`0004`/`0005` are hand-written because adding **non-nullable** columns makes `makemigrations` prompt
interactively for a one-off default; they use `preserve_default=False`, exactly what Django emits
when you answer that prompt.

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
5. **A new constraint fails on a populated table** if any existing row violates it — as `0007` did.
   The fix is to split the migration and backfill *between* the schema change and the constraint:
   `AddField` → `AlterField` → `RunPython` → `AddConstraint`.

## 4. Architecture (apps) — ADR-007
```
accounts/   ✅ DONE   custom User, roles, per-role profiles, creation service, admin
servicing/  ✅ DONE   the whole request lifecycle: ServiceRequest + review + Assignment
                      + eligibility + client/personnel pages + notification tasks
audit/      ⬜ TODO   append-only AuditEvent log (cross-cutting) — the ONE remaining seam
```
Dependency direction (a clean DAG, no cycles): `servicing → accounts`; `audit` minimal.
Rule for splitting apps: **coupling/cohesion, not category.**

## 5. What's built

**99 tests passing.** `manage.py check` clean, no model/migration drift.

### `accounts` — ADR-006, ADR-009, ADR-013
- `User(AbstractUser)`: email is the login, nested `Role` enum, custom `UserManager`.
- `ClientProfile` / `CoordinatorProfile` / `PersonnelProfile`, one per role, `OneToOne`, CASCADE.
- **Services** create user + profile atomically. `create_coordinator()` also sets `is_staff` and
  adds the `COORDINATOR` group.
- **Self-registration** for clients (`/accounts/register/`) and personnel
  (`/accounts/register/personnel/`); coordinators stay admin-provisioned.
- **Availability page** — personnel opt in; new registrations are `UNAVAILABLE`, so registering is
  not the same as being assignable.
- `role_required()` decorator, `normalize_us_phone()` (shared with `servicing`).
- Migrations: `0001` user · `0002` profiles · `0003` COORDINATOR group + backfill ·
  `0004` view_assignment.

### `servicing` — ADR-010, ADR-011, ADR-012
- **`RequestType`** — reference table: `code` (stable identity) + `name` (display) +
  `required_sector` + `is_active`. Four seeded by migration.
- **`ServiceRequest`** — client, type, scheduled slot + duration, structured US address, contact
  phone, description, status, review outcome, `started_at`. **Three `CheckConstraint`s** tying the
  nullable fields to status.
- **`Assignment`** — one row per attempt, `PENDING → ACCEPTED / DECLINED`, plus a **partial unique
  constraint** ("one live assignment per request") and its own paired-nullable check.
- **Services** — `TRANSITIONS` + `ASSIGNMENT_TRANSITIONS` maps, `submit / approve / reject /
  assign / accept / decline / start_work / fulfil`. Every status write goes through `_transition`
  (lock → validate → write → record).
- **Eligibility** (`recommendation.py`) — Strategy interface, rule-based, synchronous.
- **Client pages** — raise a request, watch status. **Personnel pages** — assignments, accept,
  decline, start, complete.
- **Admin** — read-only requests with Approve + two-step Assign actions, `Assigned to` column,
  assignment history inline, standalone Assignment list.
- **Notifications** (`tasks.py`) — 5 Celery tasks on a Redis broker, queued via
  `transaction.on_commit`, retry with backoff, tasks take IDs not instances.
  Notified: personnel on assignment; client on acceptance; client **and coordinator** on start and
  completion; client on rejection.
- Migrations `0001`–`0007`.

### Scheduling rules (ADR-011 D5) — settled after several passes, so read this before changing it
- The client **picks** a slot. Validity is **"from now onwards"**, compared against the start of the
  **current minute** — `datetime-local` has minute precision, so comparing to the exact instant
  rejects the client's own "now".
- The form **prefills now + 5 minutes**, which is valid and immediately workable.
- The service window has grace at **both** edges: **15 min early** (people arrive early; also makes
  the workflow demonstrable without waiting) and **24 h late**. Both edges govern **starting and
  completing alike**.
- **`TIME_ZONE = America/New_York`** (env-overridable). This is not cosmetic: a `datetime-local`
  input submits naive wall-clock time, and with `TIME_ZONE='UTC'` every booking silently shifted
  four hours into the past and was refused. `USE_TZ` stays `True` — storage is UTC.

### Styling
Single warm theme (butter ground `#fbf6e9`, sage accent `#38795a`), **no dark mode** — deliberately
removed. All CSS is inline in `templates/base.html` because `STATICFILES_DIRS`/WhiteNoise are not
configured yet; move it to `static/css/app.css` as part of the deployment work.

### Project
- `config/views.py:home` — role-aware landing, also `LOGIN_REDIRECT_URL`.
- `config/celery.py` — Celery app; Redis broker; console email backend.
- `templates/` — `base.html`, login, registration.

## 6. Open / deferred items

**Domain gaps** *(all logged in the brief §7)*
- [ ] **Double-booking** — eligibility ignores the calendar; one person can hold two `ACCEPTED`
      assignments for overlapping slots. Natural fourth rule in `RuleBasedRecommender`.
- [ ] **No failure path after approval** — `REJECTED` is reachable only from `SUBMITTED`, so
      "started and could not be completed" is inexpressible.
- [ ] **No cancellation** — a client whose need disappears has no exit, and `PROTECT` blocks deletion.
- [ ] **Unbounded decline loop** — no attempt counter, no escape.

**Unbuilt**
- [ ] **`audit` app** — `_record_transition()` currently writes an INFO **log line**, not an
      `AuditEvent` row. History is degraded, not discarded; one function body changes.
- [ ] **Reject from the admin** — the service and constraint exist, but there is no admin action, so
      rejection is shell-only. Deprioritised 2026-08-02 as an alternate path.
- [ ] **Coordinator ↔ request-type routing** — mechanism decided (M2M to `RequestType`), not built.
- [ ] **Client-facing status wording** — clients see internal vocabulary ("Ready for assignment").
- [ ] **Settings/env hygiene** (ADR-004) — `DEBUG=True`, hardcoded `SECRET_KEY`, empty
      `ALLOWED_HOSTS`. **The real deploy blocker.**
- [ ] **Deployment + CI/CD** (ADR-005) — Postgres, Gunicorn, WhiteNoise, health endpoint, K8s.

**Known limitations**
- `select_for_update()` is a **silent no-op on SQLite**; the concurrency protection in `_transition`
  only becomes real on Postgres.
- Notifications print to the console. Provider still open (ADR-011 D2).

## 7. Recommended next step
**Deployment Phase 0** — see §8. It begins with settings/env hygiene (ADR-004), which was already
the top item and is now also the first thing the cluster needs. Feature work (`audit` app,
double-booking rule) is deliberately paused behind it.

## 8. Deployment plan (ADR-005 finally in motion) — NEXT UP

### The homelab is not an empty cluster
`kubectl get ns` shows an established platform. **Use it rather than inventing parallel machinery:**

| Namespace | Consequence for us |
|---|---|
| `cnpg-system` | **CloudNativePG** — Postgres is a `Cluster` CR, **not** a hand-rolled StatefulSet |
| `flux-system` | **Flux** — GitOps. No `kubectl apply` from CI, no Argo |
| `cloudflared` | Tunnel **already running** — no ingress/port-forward/public IP needed |
| `longhorn-system` | StorageClass is `longhorn` |
| `traefik`, `metallb-system`, `cert-manager` | ingress / LB IPs / TLS already solved |
| `cilium-secrets` | Cilium CNI — NetworkPolicy available |
| `renovate` | image tags presumably bumped in git by Renovate |
| `linkding` | **an existing app — copy its manifest pattern** |

**Decided:** cluster is kubeadm/vanilla · images to **GHCR** · Postgres **in-cluster via CNPG**.

### Phases
```
Phase 0  app-side readiness   ✅ COMPLETE (2026-08-03)
Phase 1  build + push to GHCR via GitHub Actions   ← NEXT
Phase 2  manifests in git     Namespace · CNPG Cluster · Redis · migration JOB (not an
                              entrypoint — replicas would race) · web Deployment+Service ·
                              worker Deployment · route via the existing tunnel
Phase 3  Flux reconciles
```

### Phase 0 progress (2026-08-03) — **107 tests passing**
| # | Unit | State |
|---|---|---|
| 1 | env-driven settings via `django-environ` (+ `.env.example`) | ✅ |
| 2 | Postgres via `DATABASE_URL` + psycopg; demo data ported | ✅ |
| 3 | WhiteNoise + `collectstatic`; CSS out of `base.html` | ✅ |
| 4 | `/healthz/live` + `/healthz/ready` (middleware, not urls) | ✅ |
| 5 | HTTPS hardening; `check --deploy` 5 warnings → 2 (intentional) | ✅ |
| 6 | gunicorn + Dockerfile (one image: web / worker / migrate) | ✅ |

Image builds at **217 MB**, runs non-root, gunicorn as PID 1. All three roles smoke-tested
against real Postgres and Redis, including a task enqueued from one container and executed by
another. Build and run locally:
```
podman build -t bmh-service-hub:dev .
podman run --rm --network=host -e SECRET_KEY=x -e DEBUG=False \
  -e ALLOWED_HOSTS=localhost,127.0.0.1 \
  -e DATABASE_URL=postgres://bmh:bmh@127.0.0.1:5432/bmh bmh-service-hub:dev
```

> **`docs/deployment-phase-0.md` is the companion to this section** — for each setting, the
> problem it solves, what breaks without it, and how it was verified. Read it before changing
> anything in `config/settings.py`; several settings interlock and fail *silently* when broken
> (notably `SECURE_PROXY_SSL_HEADER`, without which every Secure cookie is quietly not set).

**Local development now requires Postgres running:** `podman start bmh-postgres`.

### Still to answer before Phase 2
1. **Flux repo layout** — same repo as cluster config or per-app repo? Kustomize or Helm?
   *Read the `linkding` manifests and follow that pattern.*
2. **Secrets** — no `sealed-secrets`/`external-secrets` namespace, so probably **SOPS** (Flux
   decrypts natively) or created out-of-band. Needed for `SECRET_KEY` + CNPG credentials.
3. **How `cloudflared` routes** — tunnel config naming Services directly, or pointing at Traefik
   with hostname routing? Decides whether an `Ingress` is needed at all.
4. **Hostname** for the app.
5. **CNPG backup destination** (S3/R2/MinIO) — already configured for other clusters?

### Why Postgres matters beyond "pods are ephemeral"
`select_for_update()` in `_transition` is a **silent no-op on SQLite**. The concurrency protection
against two coordinators assigning the same request only becomes real on Postgres.

## 9. Key files
```
manage.py
README.md                      ← setup, running locally, the demo walkthrough
config/                 settings.py, urls.py, views.py (role-aware home), celery.py
accounts/               models, services, forms, views, decorators, validators, admin, migrations
servicing/              models, services, recommendation, forms, views, tasks, admin, migrations
templates/              base.html, registration/login.html
docs/interview-prep-brief.md   ← the WHY: ADR-001…013, workflow, state machine
docs/project-status.md         ← this file: the WHAT / how to resume
docs/deployment-phase-0.md     ← the WHY of every deployment setting + verification log
```
