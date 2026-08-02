# BMH Service Hub

A coordinator-facing Django application that receives a service request, routes it through an
internal review-and-fulfilment workflow, and records every step. Three authenticating roles —
**Client**, **Coordinator**, **Personnel** — each with their own view of the same request.

Designed *as-if* HIPAA applies: synthetic data only, technical safeguards modelled.

- **Why** each design decision was made: [`docs/interview-prep-brief.md`](docs/interview-prep-brief.md) (ADR-001 … ADR-013)
- **What** exists and what is next: [`docs/project-status.md`](docs/project-status.md)

---

## The workflow

```
                      ┌──────────────────────┐
   client submits ───▶│      SUBMITTED       │
                      └──────────┬───────────┘
                    coordinator reviews  ← decision gate
                      ┌──────────┴───────────┐
                      ▼                      ▼
              ┌───────────────┐   ┌──────────────────────┐
              │   REJECTED    │   │ READY_FOR_ASSIGNMENT │◀────┐
              │  (terminal)   │   └──────────┬───────────┘     │
              └───────────────┘   coordinator assigns          │
                                             ▼                 │
                                    ┌────────────────┐         │
                                    │    ASSIGNED    │         │
                                    └────────┬───────┘         │
                                   personnel responds  ← decision gate
                                    ┌────────┴───────┐         │
                                    │                └─decline─┘
                                    ▼ accept
                                 ┌────────────┐
                                 │ SCHEDULED  │   agreed, not yet started
                                 └──────┬─────┘
                                        ▼ personnel starts work
                                 ┌──────────────┐
                                 │ IN_PROGRESS  │
                                 └──────┬───────┘
                                        ▼ personnel marks complete
                                 ┌──────────────┐
                                 │  FULFILLED   │  (terminal)
                                 └──────────────┘
```

Each `Assignment` runs its own smaller machine — `PENDING → ACCEPTED / DECLINED` — and a declined
assignment returns the request to the pool for someone else.

---

## Requirements

| | |
|---|---|
| Python | ≥ 3.12 |
| [`uv`](https://docs.astral.sh/uv/) | dependency + runtime manager — every command runs through `uv run` |
| Podman *or* Docker | only for Redis, which backs the Celery queue |

---

## Setup

```bash
uv sync                                   # install dependencies
uv run python manage.py migrate           # create the database + seed request types
uv run python manage.py createsuperuser   # prompts for EMAIL, not username
```

The database is SQLite (`db.sqlite3`, gitignored). Migrations seed four request types, so the
client form has something to offer immediately.

---

## Running it

Three processes. Redis and the worker are only needed for notifications — the app itself runs
without them, it just will not send anything.

```bash
# 1. Broker
podman run -d --name bmh-redis -p 6379:6379 docker.io/library/redis:7-alpine
#    already created? -> podman start bmh-redis

# 2. Worker (notifications). Its terminal is where notifications appear.
uv run celery -A config worker --loglevel=info

# 3. The site
uv run python manage.py runserver        # http://127.0.0.1:8000/
```

Notifications use Django's **console email backend**, so they print to the worker's terminal
rather than being delivered anywhere.

---

## Accounts

Landing on `/` sends each role to its own page.

| Role | How it is created | Lands on |
|---|---|---|
| **Client** | self-registers at `/accounts/register/` | `/requests/` |
| **Personnel** | self-registers at `/accounts/register/personnel/` | `/requests/assignments/` |
| **Coordinator** | Django admin — internal staff, no self-registration | the admin review queue |

To create a coordinator: `/admin/` → **Users** → **Add**, set role `COORDINATOR`, save, then fill
the coordinator profile inline. They get `is_staff` and the `COORDINATOR` group automatically when
created through `accounts.services.create_coordinator()`; users added by hand in the admin need the
group set manually.

> **Personnel start as UNAVAILABLE.** Registering is not the same as being ready to work — a new
> personnel member must set themselves Available before a coordinator can see them as a candidate.

---

## Walking the whole workflow

1. **Client** registers, raises a request (service, time, duration, address, contact number).
   The time field prefills a few minutes ahead, so the request is workable straight away.
   → sees **Submitted**
2. **Personnel** registers, picks their sector, sets themselves **Available**.
3. **Coordinator** opens `/admin/servicing/servicerequest/`, ticks the request, runs
   **Approve selected requests**. → **Ready for assignment**
4. **Coordinator** ticks it again, runs **Assign personnel to the selected request**, picks a
   candidate from the eligible list. → **Assigned**, personnel is emailed.
5. **Personnel** opens their assignments, clicks **Accept**. → **Scheduled**, client is emailed.
6. **Personnel** clicks **Start work** (allowed from 15 minutes before the slot).
   → **In progress**, client and coordinator emailed.
7. **Personnel** clicks **Mark complete**. → **Fulfilled**, client and coordinator emailed.

Declining at step 5 sends the request back to **Ready for assignment**, and the person who
declined is no longer offered it.

---

## Tests

```bash
uv run python manage.py test              # whole suite
uv run python manage.py test servicing    # one app
uv run python manage.py test servicing.tests.NotificationTests -v 2
```

Tests need no broker: Celery runs eagerly and `captureOnCommitCallbacks` fires the `on_commit`
hooks that would otherwise never run inside a test transaction.

---

## Useful commands

| Task | Command |
|---|---|
| System checks | `uv run python manage.py check` |
| Detect missing migrations | `uv run python manage.py makemigrations --check --dry-run` |
| Migration status | `uv run python manage.py showmigrations` |
| Blank migration to hand-write | `uv run python manage.py makemigrations --empty <app>` |
| Shell | `uv run python manage.py shell` |

---

## Layout

```
config/          settings, urls, celery app, role-aware home view
accounts/        User (email login), role, per-role profiles, registration, groups
servicing/       ServiceRequest, Assignment, the state machines, eligibility,
                 client + personnel pages, admin, notification tasks
templates/       base.html, login, registration
docs/            the ADRs and the project status
```

Dependency direction is a DAG: `servicing → accounts`. Nothing in `accounts` imports `servicing`.

---

## Known gaps

- **Double-booking** — eligibility does not check whether a person already accepted overlapping work.
- **No cancellation** and **no failure path** once a request is approved.
- **Unbounded decline loop** — no attempt limit if nobody accepts.
- **Audit log** — transitions are written to the application log, not yet to an `AuditEvent` table.
- **Settings** — `DEBUG`, `SECRET_KEY` and `ALLOWED_HOSTS` are still development defaults.
