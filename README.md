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
| Podman *or* Docker | **Postgres** (the database) and **Redis** (the Celery broker) |

---

## Setup

```bash
uv sync                    # install dependencies

cp .env.example .env       # then edit it — see below
```

**`.env` is required.** Configuration comes from the environment (ADR-004), and `SECRET_KEY`
deliberately has **no default** — the app refuses to start without one, rather than silently
falling back to a value that would be public in git history. Generate one:

```bash
uv run python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"
```

Start the two backing services, then create the schema:

```bash
podman run -d --name bmh-postgres -p 5432:5432 \
  -e POSTGRES_USER=bmh -e POSTGRES_PASSWORD=bmh -e POSTGRES_DB=bmh \
  docker.io/library/postgres:17

podman run -d --name bmh-redis -p 6379:6379 docker.io/library/redis:7-alpine

# already created? -> podman start bmh-postgres bmh-redis

uv run manage.py migrate           # create the schema + seed request types
uv run manage.py createsuperuser   # prompts for EMAIL, not username
```

Migrations seed four request types, so the client form has something to offer immediately.

> **Why Postgres and not SQLite.** `select_for_update()` in `servicing/services.py` — the lock
> that stops two coordinators assigning the same request — is a **silent no-op on SQLite**. It
> generates no lock and no warning, so the concurrency guard only becomes real on Postgres.
> SQLite remains the fallback when `DATABASE_URL` is unset (so a fresh clone can run the test
> suite with nothing running), but it is *not* an equivalent. See `docs/deployment-phase-0.md`.

---

## Running it

Two processes, plus the containers above.

```bash
# Worker (notifications). Its terminal is where notifications appear.
uv run celery -A config worker --loglevel=info

# The site
uv run manage.py runserver        # http://127.0.0.1:8000/
```

The app runs without Redis and the worker — it just will not send notifications.

Notifications use Django's **console email backend**, so they print to the worker's terminal
rather than being delivered anywhere.

### Running the production image locally

The same image the cluster runs. Useful for checking `DEBUG=False` behaviour — hashed static
assets, secure cookies, the SSL redirect — which `runserver` never exercises.

```bash
podman build -t bmh-service-hub:dev .

podman run --rm --network=host \
  -e SECRET_KEY=local-only -e DEBUG=False \
  -e ALLOWED_HOSTS=localhost,127.0.0.1 \
  -e CSRF_TRUSTED_ORIGINS=https://localhost \
  -e DATABASE_URL=postgres://bmh:bmh@127.0.0.1:5432/bmh \
  -e CELERY_BROKER_URL=redis://127.0.0.1:6379/0 \
  bmh-service-hub:dev            # or: ... bmh-service-hub:dev worker
```

One image, three roles — `web` (default), `worker`, `migrate`. With `DEBUG=False` the app
redirects plain http to https, so browse it with the header the tunnel would add:

```bash
curl -H 'X-Forwarded-Proto: https' http://127.0.0.1:8000/accounts/login/
```

### Running the whole stack in containers

`compose.yaml` runs everything — Postgres, Redis, the web server, the worker — as containers on
one network. This catches the class of bug that only appears once containerised, which
`runserver` on your host can never show you: **service discovery by DNS name**, startup ordering,
and `DEBUG=False` behaviour.

Requires **podman-compose ≥ 1.1** (Ubuntu's 1.0.6 is too old — see the note at the bottom of
`compose.yaml`):

```bash
pipx install podman-compose
```

Then:

```bash
podman-compose --profile migrate run --rm migrate   # 1. schema (waits for Postgres to be healthy)
podman-compose up -d                                # 2. postgres, redis, web, worker
podman-compose ps                                   #    all four should be healthy
```

```bash
podman-compose logs -f web worker    # follow
podman-compose down                  # stop, KEEP the database volume
podman-compose down -v               # stop and DELETE the database
```

The site is on **http://localhost:8000**. Because `DEBUG=False`, plain http gets a `301` to
https — browse it the way the tunnel would:

```bash
curl -H 'X-Forwarded-Proto: https' http://127.0.0.1:8000/accounts/login/
```

The topology deliberately mirrors Phase 2:

| compose service | Kubernetes object |
|---|---|
| `postgres` | CNPG `Cluster` |
| `redis` | Deployment + Service |
| `migrate` | **Job** — runs once, before the rest |
| `web` | Deployment + Service (behind the tunnel) |
| `worker` | Deployment (no Service — nothing routes to it) |

**The thing this stack is really for.** Inside the network the app reaches its dependencies by
**name**, not by address:

```
DATABASE_URL      = postgres://bmh:bmh@postgres:5432/bmh
CELERY_BROKER_URL = redis://redis:6379/0

postgres -> 10.89.0.4     redis -> 10.89.0.5     web -> 10.89.0.7
```

Compose runs a DNS server that resolves service names to container IPs. Kubernetes does exactly
the same thing with Services — `postgres` here becomes `postgres.bmh.svc.cluster.local` there.
Anything that hardcodes `127.0.0.1` works on your laptop and fails in **both**.

**Why `migrate` is a separate command and not a `depends_on`.** Podman translates `depends_on`
into container-level `--requires`, which cannot express "depends on something that has
*finished*" — web and worker end up stuck in `Created` with `depends on container ... not found
in input list`. Docker Compose v2 handles it; podman does not. Running it as its own step is
also the more faithful model of Phase 2, where a Job is a separate object and the Deployments
declare no dependency on it either.

### Health endpoints

```bash
curl http://127.0.0.1:8000/healthz/live     # process alive        (never touches the DB)
curl http://127.0.0.1:8000/healthz/ready    # can serve traffic    (503 if Postgres is down)
```

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

Tests also run without Postgres — `DATABASE_URL` unset falls back to SQLite so a fresh clone
works with nothing running. **That fallback is a convenience, not an equivalent**: the
`select_for_update()` guard is a no-op there, so a green SQLite run proves nothing about
concurrency. CI always uses Postgres for this reason.

### Continuous integration

`.github/workflows/ci.yml` runs on every push to `main`, every pull request, and on demand.

| Job | What it does |
|---|---|
| **Tests (Postgres)** | `uv sync --frozen` · missing-migration check · migrate · full suite · `check --deploy` · `collectstatic` with `DEBUG=False` |
| **Container image builds** | builds the Dockerfile, then smoke-tests it: liveness 200 and readiness 503 with **no database**, and a non-root uid |

Two details worth knowing:

- **CI runs against real Postgres**, for the `select_for_update()` reason above.
- **`collectstatic` runs with `DEBUG=False`**, which selects the manifest storage backend. That
  backend raises on any `{% static %}` path that does not resolve — so a typo'd asset reference
  fails the build rather than a user's page.

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
