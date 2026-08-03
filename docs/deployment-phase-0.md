# Phase 0 — Application readiness for Kubernetes

Companion to `docs/project-status.md` §8, which holds the *plan*. This file holds the
**reasoning**: for each setting introduced in Phase 0, what problem it solves, what breaks
without it, and how that was verified.

Written to be re-read cold. If you are picking this up months later — or explaining it in an
interview — start at "The request path", because almost everything else falls out of it.

---

## The request path

Everything in Phase 0 derives from one picture:

```
  Browser  ──── HTTPS (encrypted) ────>  Cloudflare
                                              │
                                        (tunnel, encrypted)
                                              │
                                              v
                                         cloudflared pod
                                              │
                                    HTTP (plain, unencrypted)
                                              │
                                              v
                                         your Django pod
```

Encryption **stops at Cloudflare**. From there inward the traffic is plain HTTP, which is
acceptable because it never leaves the cluster's internal network.

Vocabulary: Cloudflare acts as a **reverse proxy**, and the point where encryption ends is
**TLS termination** (TLS is the modern name for SSL; both terms are used interchangeably).

The consequence that drives half of this document: **Django is the last box in that diagram.**
It receives a plain, unencrypted request. When Django asks "was this connection secure?", the
honest answer from its own vantage point is *no* — it cannot see the browser's leg of the
journey. Several settings exist purely to close that gap.

---

## Unit 1 — Configuration from the environment

### The problem

The app must behave differently in different places: `localhost` versus a Postgres Service,
full error pages versus none. Same code, different knobs.

The naive approaches — `if PRODUCTION:` branches, or `settings_prod.py` beside
`settings_dev.py` — put deployment-specific values *in the repository*. Some of those values
are secrets, and a repository is the one place a secret must never live.

The discipline is **twelve-factor config**: anything that varies between deployments comes from
the environment. An **environment variable** is a named string the OS hands a process at
startup — from the Deployment spec in Kubernetes, from a `.env` file locally.

`django-environ` was chosen over `python-dotenv`/`os.environ` for two reasons: typed casts
(see `DEBUG` below, where this is not cosmetic) and `env.db_url()`, which parses a
`DATABASE_URL` into Django's `DATABASES` dict for free.

### `SECRET_KEY` — deliberately has no default

```python
SECRET_KEY = env('SECRET_KEY')
```

Django uses this to **cryptographically sign** session cookies, password-reset links and form
tokens. Signing attaches a fingerprint only the key-holder can produce, so Django can issue a
cookie, receive it back later, and verify nothing was edited in between.

An attacker holding the key can forge that fingerprint — minting a session cookie for any user,
including the coordinator, without ever knowing a password.

The pre-Phase-0 key (`django-insecure--j4$d+...`) was committed to git. That is normal
`startproject` behaviour and fine for development, but had it survived to production the key to
the entire authentication system would have been readable by anyone with repo access.

**Why no fallback.** With `default='django-insecure-...'`, a deploy that forgets the variable
**starts perfectly** — while signing sessions with a value published in git history, and
nothing anywhere reports it. A crash at startup is a bad afternoon; a silent insecure fallback
is a breach discovered later.

> **Principle: prefer a loud crash to a quiet insecure default.** It recurs throughout Phase 0.

Verified:
```
PASS  missing SECRET_KEY raised: Set the SECRET_KEY environment variable
```

The old key remains in git history. It is a development key that was never deployed, so history
was not rewritten.

### `DEBUG` — where the type cast earns the dependency

```python
DEBUG = env('DEBUG')      # declared as (bool, False)
```

`DEBUG=True` returns, on any error, a page containing the stack trace, local variable values and
much of the settings — a map of the application's internals.

Environment variables are **always strings**. There is no boolean env var; the OS hands over the
text `"False"`. In Python:

```python
bool("False")   # True  ← every non-empty string is truthy
```

So the obvious `os.environ.get('DEBUG')` yields `DEBUG=True` on a server explicitly configured
with `DEBUG=False`. A common production bug that looks like nothing is wrong until an error page
appears in front of a user.

The default is `False` — the safe value, not the convenient one. A forgotten variable produces a
production-safe app rather than an exposed one.

Verified: `PASS  DEBUG is a real bool: False`

### `ALLOWED_HOSTS` — empty is the correct default

```python
ALLOWED_HOSTS = env('ALLOWED_HOSTS')     # default: []
```

Every HTTP request carries a `Host` header naming the site it is for — how one server hosts many
domains. It is client-supplied text and can say anything.

Django checks it against this list and answers `400 Bad Request` on a mismatch. The attack
prevented is **Host header injection**: Django sometimes builds absolute URLs from the `Host`
header, and password reset is the dangerous case. Request a reset while sending
`Host: evil.com`; if Django trusts it, the emailed reset link points at `evil.com` carrying a
*valid* token. The user clicks their own legitimate email and hands over the account.

Empty plus `DEBUG=False` means Django refuses everything. That is preferable to a `['*']`
default, under which a misconfigured deploy is silently vulnerable rather than visibly broken.
**Visibly broken is the better failure** — it is noticed in seconds.

### `CSRF_TRUSTED_ORIGINS` — the setting that 403s the login page

New in Phase 0; irrelevant on `http://localhost`.

For form submissions Django compares the `Origin` header against the site it believes it is
serving. Behind the tunnel the browser is on `https://<host>`, but Django — last box in the
diagram — sees plain HTTP and computes `http://<host>`. Scheme mismatch, so Django concludes
cross-origin POST and rejects.

Result: **403 on every form, including login.** The site is reachable, the login page renders,
and nobody can get in. This is the most common "worked locally, broken in production" Django
failure.

Parsing is verified; the setting cannot be genuinely exercised until a real hostname exists
behind the tunnel. **If forms 403 on first deploy, look here first.**

---

## Unit 2 — Postgres

### Why not SQLite

SQLite is a file, and two facts make that fatal:

1. **Containers have no durable filesystem.** A pod is disposable — rescheduled, restarted,
   replaced on every deploy — and its local disk goes with it. The database would vanish on the
   next rollout.
2. **More than one pod** means each has its own separate file: two users, two different
   databases, depending on which pod they reach.

Both are real. Neither is the interesting reason.

### The interesting reason: `select_for_update` was doing nothing

A **transaction** is a group of operations treated as indivisible — all of it happens or none
does (`with transaction.atomic():`).

The scenario `servicing/services.py::_transition` was written to prevent — two coordinators,
Ada and Ben, on the same unassigned request:

```
  time   Ada                                Ben
   │
   1     read request → READY_FOR_ASSIGNMENT
   2                                        read request → READY_FOR_ASSIGNMENT
   3     "READY is assignable" ✓
   4                                        "READY is assignable" ✓
   5     write → ASSIGNED (personnel A)
   6                                        write → ASSIGNED (personnel B)
```

Both read the same state, both validated against it, both wrote. The second silently overwrites
the first: one request, two assignments, and personnel A believes they hold a job that is no
longer theirs. Nothing errors, nothing logs.

This is a **race condition** — correctness depending on timing — in its most common
**read-modify-write** shape.

The guard at `servicing/services.py:81`:

```python
locked = ServiceRequest.objects.select_for_update().get(pk=service_request.pk)
```

`SELECT ... FOR UPDATE` reads a row and **locks it until the transaction ends**. Ben's identical
query at step 2 now blocks until Ada commits; when it resumes it reads `ASSIGNED`, the
transition check fails, and Ben receives a proper error rather than silently clobbering Ada.
This is **pessimistic locking**: assume conflict, take the lock up front.

**On SQLite this line does nothing.** SQLite has no row-level locking — it locks the whole
database file — and Django's SQLite backend accepts `select_for_update()` while generating no
lock. No warning, no error. The code reads as though it is protected; it is not.

Verified by running the same probe against both backends:

```
Postgres:  PASS  row is genuinely locked: could not obtain lock on row
                 in relation "servicing_servicerequest"
SQLite:    FAIL  second connection took the lock -- it is a no-op here
```

> **This is the answer to "why Postgres" worth giving in an interview.** Not "SQLite doesn't
> scale". The precise version: *a concurrency guarantee the code already claims to make is
> unenforced on SQLite; switching engines is what makes existing code correct.* No feature was
> added — a line that was already there started working.

### `DATABASE_URL`

```
postgres://user:password@host:5432/dbname
```

One string carries the whole connection. This matters concretely: **CloudNativePG** (the
Postgres operator on the cluster) generates a Secret that already contains exactly this format,
so the Deployment passes it straight through — no reassembling five fields, five chances to
typo.

SQLite remains the fallback so `manage.py test` and a fresh clone work with nothing running. As
the probe above shows, it is a convenience, **not an equivalent**.

### `CONN_MAX_AGE` and the arithmetic that bites

By default Django opens a connection per request and closes it at the end. Each open costs a TCP
handshake plus — in Postgres specifically — the server **forking a process**. That is paid on
every page load.

`CONN_MAX_AGE = 60` holds the connection open for reuse: **connection pooling** in its simplest
form.

```
total connections = number of pods × workers per pod
```

Three pods × four workers = twelve connections held open, idle between requests. Postgres
defaults to `max_connections = 100`. Scale carelessly and it is exhausted, at which point *every*
pod fails, including healthy ones. **This number and the replica count must move together.**

```python
CONN_HEALTH_CHECKS = True
```

This is what makes pooling safe. A held connection can die while idle — CNPG failover, idle
timeout, network blip. Without this the death is discovered when a query explodes mid-request,
as an intermittent 500 that is hard to reproduce. With it, Django cheaply tests the connection at
request start and transparently reconnects. **Enabling `CONN_MAX_AGE` without this is a known
way to get mystery errors after any database restart.**

Readiness uses `SELECT 1` through a cursor rather than `connection.ensure_connection()`, because
with pooling a socket can be *open* but no longer usable — `ensure_connection()` would call that
healthy.

### Migrating the demo data: two classic traps

**Natural keys.** `dumpdata` writes foreign keys as raw primary-key integers by default. The
SQLite `RequestType` rows were at pks 5–8; a fresh Postgres database had the same four types at
pks 1–4 from a data migration, so loading collided. `User` came through cleanly as
`['endiesworld@gmail.com']` because it defines `natural_key()` — a stable business identifier
rather than an arbitrary database ID, which makes data portable between databases.
**`RequestType` has an obvious candidate in `code` and does not use it; worth adding.**

**Sequences.** Postgres assigns IDs from a **sequence** (a counter). `loaddata` inserts explicit
IDs without advancing it, so if nothing resets the counter it still reads 1 while rows 1–4
exist — and every subsequent insert collides. Django does reset them, but this is the single
most common "restored database throws `IntegrityError` on every insert" bug, so it was checked
rather than trusted:

```
new ServiceRequest got pk=5 (must be > 4)
PASS  sequences reset
```

---

## Unit 3 — Static files

Django's dev server serves static files only when `DEBUG=True`; it is a convenience explicitly
disabled in production and documented as unsuitable for real traffic.

The traditional answer is a separate nginx process — a second container, a shared volume, a
second configuration to drift. **WhiteNoise** serves them from inside Django instead: no
sidecar, no volume, no drift.

### Three similarly-named concepts

| Setting | What it is |
|---|---|
| `STATICFILES_DIRS` | **source** — where you put files (`static/css/app.css`). Committed. |
| `STATIC_ROOT` | **build output** — where `collectstatic` assembles everything. Gitignored. |
| `STATIC_URL` | the **URL prefix** browsers request (`/static/…`). |

`collectstatic` merges every source — yours, Django admin's, every app's — into one output
directory, baked into the image at build time. Nothing writes there at runtime, which lets the
container filesystem stay read-only.

### Content hashing

```
/static/css/app.573d5f78e42d.css
```

The hex string is a hash of the file's **contents**.

Aggressive caching is desirable — no point re-downloading unchanged CSS — but "cache for a year"
means a shipped fix is invisible for a year. The old workaround, `app.css?v=2`, was manual and
easy to forget.

Content hashing automates it: change one byte, the hash changes, the **URL** changes, so the
browser sees a resource it has never fetched. An unchanged file keeps its URL and is served from
cache. The cache header can therefore be maximal:

```
Cache-Control: max-age=315360000, public, immutable
```

Ten years, `immutable` meaning "do not even revalidate". **A stale cache becomes structurally
impossible.** The technique is **cache busting**.

The `.gz` files alongside are pre-compressed at build time rather than per request — `app.css`
went 8608 → 2796 bytes, with the cost paid once during the image build.

### The trap: the manifest backend and CI

`CompressedManifestStaticFilesStorage` writes a **manifest** — a JSON map from `css/app.css` to
`css/app.573d5f78e42d.css` — which `{% static %}` consults to emit hashed URLs. A lookup missing
from that manifest **raises**, and the manifest exists only after `collectstatic` runs.

Tests initially passed only because `collectstatic` had already been run locally. On a fresh
clone or in CI:

```
ValueError: Missing staticfiles manifest entry for 'admin/css/base.css'
→ 12 errors
```

Every test touching the Django admin — how the coordinator works — dead on a checkout that had
not built assets.

**Resolution:** non-manifest backend when `DEBUG` (same gzipping, no manifest requirement),
manifest backend in production. The strictness is *wanted* in production, where a typo'd
`{% static %}` path becomes a **failed image build** rather than a broken page — it simply
cannot be a precondition for running tests. The Dockerfile therefore runs `collectstatic` with
`DEBUG=False` at build time.

> **Generalises past static files:** tests passed because of local machine state that would not
> exist in CI. Deleting that state and re-running is a cheap habit.

---

## Unit 4 — Health probes

Kubernetes offers two checks. Using one where the other belongs is actively harmful.

| | Question | Failure action |
|---|---|---|
| **liveness** | "Is this process alive?" | **restart the container** |
| **readiness** | "Can it serve traffic now?" | **remove from load balancer, leave running** |

Not two flavours of "is it healthy" — two different *remedies*.

### Liveness must not touch the database

Restarting fixes a stuck process — a deadlock, an exhausted event loop. It does **not** fix an
unreachable database.

If liveness ran `SELECT 1`, a ten-second Postgres blip would fail liveness on every pod at once.
Kubernetes restarts them all. They return cold — empty caches, no warm connections — and
stampede a database that was already struggling. A recoverable blip becomes a full outage, with
pods cycling. This is a **thundering herd**.

Hence:

```python
def live(request):
    return HttpResponse("ok\n")
```

Deliberately trivial. **Anything liveness touches becomes a reason for Kubernetes to kill the
container.**

Readiness checks exactly what liveness must not:

```
                 DB reachable    DB unreachable
/healthz/live       200              200        ← restarting would not help
/healthz/ready      200              503        ← stop routing here until it recovers
```

That split is the whole design. Under the same outage pods stay up, drop out of rotation, and
rejoin automatically — no human involved. Readiness also makes **rolling deploys** safe: a pod
that has booted but cannot reach Postgres never receives a request, so Kubernetes will not retire
the old pods until the new ones can genuinely serve.

### Readiness checks Postgres but deliberately not Redis

The web process only *enqueues* notifications via `transaction.on_commit`. If Redis is down,
notifications are delayed but requesting, assigning and accepting all still work. Failing
readiness would take the site offline to protect a side effect.

> **Check what you cannot serve without, not everything you talk to.** Revisit if notifications
> ever become user-blocking.

### Why middleware, not a URL

Kubernetes addresses probes to the **pod IP**, so they arrive with `Host: 10.42.3.17:8000`.
That is never in `ALLOWED_HOSTS` — and cannot be, since it differs per pod and changes on every
restart. Routed through `urls.py`, Django answers `400`, the probe fails, the pod is killed, and
it repeats forever: a **CrashLoopBackOff whose cause looks nothing like its symptom**, on an app
that works perfectly.

`request.path` reads `PATH_INFO` and never calls `get_host()`, so matching there is safe before
host validation. Hence `config/health.py::HealthCheckMiddleware`, positioned **first** in
`MIDDLEWARE` — which turns out to be load-bearing a second time (see Unit 5).

The alternative is pinning `httpHeaders: [{name: Host, value: <hostname>}]` on every probe in
the Deployment. That works, but places a correctness requirement in the manifest where it is
easy to omit on the next probe someone adds.

---

## Unit 5 — HTTPS hardening

All of it gated on `not DEBUG`. **Not for convenience — for correctness.** Every setting assumes
HTTPS while development runs on `http://localhost`. Enable `SESSION_COOKIE_SECURE` locally and
the browser refuses to send the session cookie over plain HTTP: the login form submits, returns
success, and lands back on the login page. No error, no traceback, nothing logged — from
Django's side nothing went wrong; it simply never received a session cookie.

### `SECURE_PROXY_SSL_HEADER` — the gap-closer

```python
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
```

A reverse proxy adds headers describing the original request: `X-Forwarded-For` (real client IP)
and `X-Forwarded-Proto` (protocol the browser used). This tells Django to treat requests bearing
`X-Forwarded-Proto: https` as secure, so `request.is_secure()` becomes `True` and everything
downstream starts working.

**Django does not do this automatically, on purpose.** A header is text any client can send. If
the pod were reachable directly, anyone could claim `X-Forwarded-Proto: https` and be believed —
**header spoofing**. It is safe here only because the pod has no public route: the sole ingress
is the cloudflared tunnel, which *overwrites* the header rather than passing a client-supplied
one through.

> **Phase 2 follow-up:** a NetworkPolicy restricting ingress to the `cloudflared` namespace
> enforces this at the network layer instead of relying on the reasoning holding.

**Without it:** Django believes every request insecure, so it silently declines to set any
Secure cookie *and* `SECURE_SSL_REDIRECT` redirects to HTTPS, which returns through the same
path, still looks insecure, and redirects again — `ERR_TOO_MANY_REDIRECTS`.

### Cookie flags

```python
SESSION_COOKIE_SECURE = True      CSRF_COOKIE_SECURE = True
SESSION_COOKIE_HTTPONLY = True    CSRF_COOKIE_HTTPONLY = True
```

The **session cookie** proves you are logged in — steal it and you *are* that user, no password
required.

`Secure` instructs the browser never to send the cookie over an unencrypted connection. Without
it, one accidental `http://` request — a typo, an old bookmark, a link in an email — transmits
the session in the clear for anyone on the network path to read. The attack is **session
hijacking**.

Note the interlock: Django sets `Secure` only when it believes the connection is secure, so
without `SECURE_PROXY_SSL_HEADER` these lines are **silently inert**.

`HttpOnly` forbids JavaScript from reading the cookie. If a script is ever injected into the page
— **cross-site scripting (XSS)** — its first move is to read `document.cookie` and exfiltrate the
session. `HttpOnly` prevents that; the cookie still accompanies every request, it is simply
invisible to JS.

> **`CSRF_COOKIE_HTTPONLY = True` is NOT Django's default.** Django leaves the CSRF cookie
> readable because apps posting via `fetch`/`axios` must read the token into a header. This app
> never does — every form is server-rendered with `{% csrf_token %}`, which injects the token
> server-side. **If JavaScript that POSTs is ever added, this setting will break it, and the
> symptom will be a mysterious 403.**

### `SECURE_SSL_REDIRECT` and the collision with health probes

Redirects plain HTTP to HTTPS with `301`. Cloudflare can already do this at the edge, so this is
belt-and-braces — one redirect's cost for correctness independent of edge configuration.

It collides with health probes, which arrive over plain HTTP with no `X-Forwarded-Proto` because
no proxy is involved. `SECURE_SSL_REDIRECT` answers `301`; Kubernetes treats any 3xx as a
**failed** probe and eventually restarts the container — which has the same problem. Restart,
fail, restart: **CrashLoopBackOff on an app that is answering correctly.**

Middleware is a stack; each layer sees the request inbound and may handle it or pass it down:

```
  request
     │
     v
  HealthCheckMiddleware      ← is this /healthz/*?  answer 200 and STOP
     │  (otherwise pass down)
     v
  SecurityMiddleware         ← insecure? redirect to https
     │
     v
  ... the rest of Django
```

Because health sits **above** security, probes are answered before `SECURE_SSL_REDIRECT` sees
them. Confirmed by negative control — reordered, observed the break, reordered back:

```
health middleware BELOW security:  /healthz/live -> 301   ← the CrashLoopBackOff
actual ordering (above):           /healthz/live -> 200
```

That ordering now does two jobs (this, plus `ALLOWED_HOSTS` from Unit 4) and is pinned by two
regression tests in `config/tests.py`, one of which fails if anyone reorders `MIDDLEWARE`.

### HSTS — and why it starts at one hour

```python
SECURE_HSTS_SECONDS = 3600          # env-driven
SECURE_HSTS_INCLUDE_SUBDOMAINS = False
SECURE_HSTS_PRELOAD = False
```

**HTTP Strict Transport Security** tells the browser: for the next N seconds never attempt plain
HTTP for this hostname — go straight to HTTPS, and if the certificate is bad, refuse entirely
rather than warning.

It closes the gap `SECURE_SSL_REDIRECT` leaves: a redirect requires **one** insecure request to
happen first, and that first request is exactly where a network-positioned attacker intercepts
and never lets you reach HTTPS. After the browser has seen the HSTS header once, it upgrades
locally, before any packet leaves the machine.

**The danger.** HSTS is stored *in the browser* and is **irreversible for its full duration**
— there is no server-side undo. Ship `max-age=31536000` and every browser that saw it refuses
plain HTTP for a year, even if the certificate expires, even if you would like to serve an
explanation over HTTP. You wait it out.

For a homelab domain still being set up, that is a genuine way to lock yourself out. Hence one
hour, raised **via env with no rebuild** once hostname and certificate are settled. Usual ramp:
1 hour → 1 day → 1 year.

`INCLUDE_SUBDOMAINS` extends the rule to subdomains that do not exist yet; `PRELOAD` submits the
domain to a list **compiled into browsers**, where removal takes months and only lands as users
update. Both widen an already-irreversible commitment, so both stay off.

> These two are the only remaining `check --deploy` warnings, and **they should stay warnings.**
> A clean check output here would mean an irreversible commitment to a domain that is not
> finished. A clean report is not the goal.

### SameSite, and what CSRF actually is

```python
SESSION_COOKIE_SAMESITE = 'Lax'     CSRF_COOKIE_SAMESITE = 'Lax'
```

The attack: you are logged into the hub in one tab. In another you open a malicious page holding
a hidden form that POSTs to a hub URL. The browser's default is to attach your cookies to *any*
request to that domain regardless of which site triggered it, so the request arrives fully
authenticated and indistinguishable from a real click. That is **CSRF** — cross-site request
forgery.

Django's primary defence is the CSRF token: a secret embedded in every genuine form, which the
attacker's page cannot read (**same-origin policy**) and therefore cannot include. `SameSite` is
a second, independent, browser-level layer: do not send this cookie on requests originating from
another site.

`Lax` (Django's default, stated explicitly) blocks the cross-site POST while still sending
cookies when someone follows an ordinary link *to* the site. `Strict` would block that too,
logging users out when they click a link in a notification email — usability cost for no
meaningful gain on top of the CSRF token.

### Two cheap headers, on in every environment

```python
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = 'DENY'
```

**`nosniff`** stops browsers guessing a file's type when the declared type looks wrong — helpful
in 1998, dangerous now, since a file uploaded as an image but *containing* JavaScript could be
guessed into execution. The behaviour is **MIME sniffing**.

**`X-Frame-Options: DENY`** forbids other sites embedding these pages in an `<iframe>`. The
attack is **clickjacking**: an attacker overlays your app invisibly on their own page, and a user
who believes they are clicking "Play" actually clicks "Approve request".

Both are already Django defaults. Written out explicitly so that turning them *off* must be a
deliberate act visible in a diff, rather than a silent consequence of an upgrade.

---

## Unit 6 — Gunicorn and the image

### Why not `runserver`

`manage.py runserver` is single-threaded, reloads on file changes, and is documented as
unsuitable for production. It also **refuses to boot without a database** — it runs a migration
check first — which is why the readiness probe could not be tested through it. Gunicorn boots
regardless and reports `503` from readiness, which is the behaviour Kubernetes needs.

**Gunicorn** forks a pool of worker processes, each handling one request at a time, and restarts
any that die. Configuration lives in `config/gunicorn.py`, env-driven so a Deployment can tune it
without a rebuild.

The settings worth knowing:

- **`bind = 0.0.0.0:8000`** — inside a container `localhost` means *the container itself*, so
  binding `127.0.0.1` would make the port unreachable from the Service. The pod would pass no
  probes and serve nothing.
- **`workers = 3`**, explicit rather than derived from `os.cpu_count()`. In Kubernetes the pod's
  CPU *limit* is what matters, but `cpu_count()` reports the **node's** cores — on a big node
  that over-provisions badly. Scale by adding pods. And recall the arithmetic from
  `CONN_MAX_AGE`: pods × workers = held Postgres connections.
- **`timeout = 30`** is a hard ceiling on any single request. A slow report that legitimately
  runs longer gets killed mid-flight — a reason to move such work to Celery, not to raise this.
- **`max_requests = 1000` + jitter** recycles workers periodically: cheap insurance against a
  slow leak in any dependency, with jitter so they don't all recycle at once and empty the pool.
- **Logs to stdout/stderr, never files.** Container filesystems are ephemeral; `kubectl logs`
  reads the streams. Probe requests are filtered out of the access log — hitting `/healthz/*`
  every few seconds forever would drown any real traffic.
- **`preload_app = False`.** Preloading forks workers from one imported app, saving memory via
  copy-on-write, but it opens the database connection *before* the fork so every worker inherits
  and shares one socket — which corrupts under concurrent use.

### One image, three roles

`docker-entrypoint.sh` dispatches on the container's args:

```
args: ["web"]      gunicorn, serves HTTP
args: ["worker"]   celery, drains the notification queue
args: ["migrate"]  applies migrations, then exits 0   (a Job, not a sidecar)
```

The web Deployment, the worker Deployment and the migration Job therefore run the **identical
image digest**. That eliminates the drift behind "works in web, fails in the worker" bugs.

**`exec` is load-bearing.** It replaces the shell with gunicorn as **PID 1**, so `SIGTERM` from
the kubelet reaches gunicorn directly. Without it the shell is PID 1, does not forward the
signal, and every rollout waits out the full termination grace period before the pod is killed —
deploys crawl for no visible reason. Verified: `podman stop` returned in **0s**; a shell
swallowing the signal would take ~10s.

**Migrations are deliberately not run at web startup.** With more than one replica, every pod
would run them simultaneously against the same database, racing on the same DDL. Django takes a
lock per migration so the usual outcome is a slow deploy rather than corruption, but it is still
several pods fighting over schema changes at the worst moment. A Job runs exactly once; Phase 2
gates the Deployment behind it.

### The Dockerfile

**Multi-stage.** Stage 1 installs dependencies with `uv`; stage 2 copies only the finished
`.venv`. Build tooling and caches never reach the shipped image. Final size: **217 MB**.

**`uv sync --frozen`** installs exactly what `uv.lock` pins and fails if it disagrees with
`pyproject.toml`. Never re-resolve during a build — the image must contain the versions that were
tested, not whatever is newest at build time.

**Layer ordering.** `pyproject.toml` and `uv.lock` are copied and installed *before* the source.
Docker caches layers by content, so editing a view reuses the whole dependency install.

**Non-root.** Containers run as root by default, so a process escaping the container arrives as
root on the node. Nothing here needs privilege — static files are baked in at build time and
uploads are not implemented. Verified: `uid=1001(app)`.

**No `libpq` or C toolchain**, because `psycopg[binary]` ships prebuilt wheels. That Unit 2
choice is what keeps this to a plain slim base.

### The build-time `SECRET_KEY`, and why it is not a leak

```dockerfile
RUN SECRET_KEY=build-time-placeholder-never-used-to-sign-anything \
    DEBUG=False \
    python manage.py collectstatic --noinput --clear
```

`collectstatic` imports settings, and settings **require** `SECRET_KEY` (Unit 1 — deliberately no
default). Without a placeholder the build simply fails. The value never signs anything: no
request is served during a build, and the real key arrives from a Secret at runtime.

`DEBUG=False` is what selects `CompressedManifestStaticFilesStorage`, so the manifest and hashed
filenames are produced here — and a typo'd `{% static %}` path fails **this line** rather than a
user's page. This is the payoff for the conditional storage backend from Unit 3.

### `.dockerignore` matters more than it looks

Anything `COPY`ed is in the final layers **forever**, even if a later instruction deletes it.
`.env` holds the real `SECRET_KEY`. Verified absent from the image, along with `db.sqlite3` and
`.git`. `staticfiles/` is excluded too — copying a locally-built one in would ship stale,
differently-hashed assets.

---

## The thread running through Phase 0

Nearly every failure mode above is **silent**:

- a `SECRET_KEY` fallback that works perfectly while being public
- `DEBUG` set to the string `"False"`, which is `True`
- `select_for_update()` generating no lock and no warning
- a `Secure` cookie never sent, so login simply does not happen
- tests passing because of a directory that will not exist in CI

None raise. None log. In each case the system **reports success while being wrong** — which is
why each unit was verified by *observable behaviour* (`Set-Cookie` flags, the actual
`Strict-Transport-Security` header, a real 301, a real 200, a real row lock) rather than by
reading the settings file and pronouncing it correct.

A settings file that looks right and a system that behaves right are different claims. Only the
second is worth anything.

---

## Verification log

Reproducible checks, all run on 2026-08-03.

| Claim | How it was checked | Result |
|---|---|---|
| Missing `SECRET_KEY` crashes | import settings with the var unset | raises `ImproperlyConfigured` |
| `DEBUG` casts to a real bool | `DEBUG="False"` → `settings.DEBUG` | `False` (bool) |
| `ALLOWED_HOSTS`/`CSRF_TRUSTED_ORIGINS` parse | comma-separated string → list | lists |
| Migrations apply on Postgres | fresh database, `migrate` | all 7 applied |
| Row lock is real | two connections, `select_for_update(nowait=True)` | Postgres locks, **SQLite does not** |
| Data ported intact | counts, statuses, group membership | 17 objects, all preserved |
| PK sequences reset after `loaddata` | create a row, assert pk > 4 | pk=5 |
| CSS extraction lossless | `diff` against `HEAD:templates/base.html` | byte-identical |
| Static served under `DEBUG=False` | fetch the hashed URL | 200, `immutable`, gzip 8608→2796 |
| Fresh clone can run tests | delete `staticfiles/`, run suite | 99/99 after the fix (12 errors before) |
| Liveness ignores DB outage | dead DB port, real TCP failure | `/healthz/live` 200 |
| Readiness reports DB outage | same | `/healthz/ready` 503 + reason |
| Probes bypass host validation | `Host: 10.42.3.17:8000` vs `ALLOWED_HOSTS` | 200 (and `/` still 400) |
| Probes bypass SSL redirect | `SECURE_SSL_REDIRECT=True` | 200; reordered → 301 |
| Cookies carry Secure/HttpOnly/Lax | inspect `Set-Cookie` | all three present |
| Security headers present | inspect response headers | HSTS, `DENY`, `nosniff` |
| `check --deploy` | `DEBUG=False` | 5 warnings → 2 (both intentional) |
| Image builds | `podman build` | 217 MB, collectstatic ran inside |
| Runs as non-root | `podman exec … id` | `uid=1001(app)` |
| No secrets baked in | check `/app/.env`, `db.sqlite3`, `.git` | all absent |
| Gunicorn is PID 1 | `cat /proc/1/cmdline`; time `podman stop` | gunicorn; **0s** |
| Probes excluded from access log | hit both, count log lines | 0 healthz, 4 real |
| `migrate` role | run against Postgres | applied, exit 0 |
| `web` role | probes + login + hashed CSS | 200 / 200 / 200, plain http → 301 |
| `worker` role | run against Redis | connected, 5 tasks registered |
| End-to-end enqueue | task `.delay()` from a *separate* container | same task id received + succeeded |
| Test suite | full run on Postgres | **107 passing** |

---

## Open items carried into Phase 1/2

1. **NetworkPolicy** restricting pod ingress to the `cloudflared` namespace, so
   `SECURE_PROXY_SSL_HEADER` rests on enforcement rather than reasoning.
2. **Probe tuning** — `initialDelaySeconds`, `periodSeconds`, `failureThreshold` belong in the
   Deployment, written with the manifests.
3. **`CSRF_TRUSTED_ORIGINS`** cannot be truly exercised until a real hostname exists.
4. **HSTS ramp** — raise from 3600 once the domain and certificate are settled.
5. **`EMAIL_BACKEND`** still hardcoded to the console backend; provider open per ADR-011 D2.
6. **`RequestType.natural_key()`** — would have avoided the fixture collision and makes future
   data moves portable.
7. **`CONN_MAX_AGE` vs `max_connections`** — revisit when replica count is chosen.
