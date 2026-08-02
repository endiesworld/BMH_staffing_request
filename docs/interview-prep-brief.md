# BMH Service Request & Fulfilment Hub — Interview Prep Brief

> A living design document. We add to it as we make decisions. It doubles as your
> interview talking-point script: every decision below is something you can defend out loud.
>
> **Resuming work?** Start with [`project-status.md`](./project-status.md) — current state, how to
> run, and next steps. This file is the *why* behind it all.

---

## 1. What the system is

A **coordinator-facing** Django application that receives a service request, validates it,
routes it through an internal workflow, and records **every** action for audit.

**Actors (all authenticate / log in):**
- **Client** — submits a service request.
- **Coordinator** — reviews requests, assigns personnel.
- **Personnel** — accepts or declines assignments.

---

## 2. The workflow (original brief)

```
Client submits request
        ↓
Coordinator reviews request
        ↓
System identifies eligible personnel
        ↓
Coordinator assigns personnel
        ↓
Personnel accepts or declines
        ↓
System updates status and audit history
        ↓
External SaaS notification is triggered
```

---

## 3. Domain reading (nouns vs. verbs)

Turning the workflow into **entities** (nouns) and **state transitions** (verbs) — the
single most important early exercise, because the data model is the most expensive thing
to get wrong later.

| Workflow step | Entity in play | What actually changes |
|---|---|---|
| Client submits request | `Client`, `ServiceRequest` | request is **created** |
| Coordinator reviews | `Coordinator` | request status transition (decision gate) |
| System identifies eligible personnel | `Personnel` + eligibility rules | **a query**, not a stored state |
| Coordinator assigns | `Assignment` (request ↔ personnel) | assignment **created**, request → assigned |
| Personnel accepts/declines | `Assignment` | assignment → accepted / declined |
| System updates status + audit | `ServiceRequest`, `AuditEvent` | status transition + **immutable** log entry |
| External SaaS notification | integration boundary | side effect |

**Two insights to say out loud in an interview:**
1. **There are two state machines, not one** — the `ServiceRequest` has a lifecycle, and each
   `Assignment` has its own (pending → accepted/declined). Don't conflate them.
2. **"Identify eligible personnel" is a query, not a state** — so eligibility rules (skills?
   availability? location?) are a design decision still to be pinned down.

---

## 4. Key principle discovered: a step is only a transition if it produces a decision

"Coordinator reviews the request" was **ambiguous** — *viewing* is a read, not a transition.
If review just means "a coordinator looked at it," nothing changes and the workflow can't know
whether to proceed. So **review must be a decision gate that closes with an outcome.**

Naming that outcome revealed **branches the linear diagram was hiding:**

```
SUBMITTED
   │  coordinator reviews  ← DECISION GATE
   ├── reject ──────────────► REJECTED (terminal)
   └── approve ─────────────► READY_FOR_ASSIGNMENT
                                   │  coordinator assigns
                                   ▼
                               ASSIGNED
                                   │  personnel responds  ← DECISION GATE
                                   ├── decline ──► back to READY_FOR_ASSIGNMENT  (reassign loop)
                                   └── accept ───► IN_PROGRESS ──► FULFILLED (terminal)
```

**What the happy-path diagram omitted (name these in an interview):**
- **Terminal failure paths** (reject).
- **A loop**: "personnel declines" must return the request to the assignable pool for reassignment — it can't dead-end.

---

## 5. Decision log (ADR-style)

### ADR-001 — "Review" is a decision gate, not a passive view
- **Context:** The brief's "coordinator reviews request" step did not describe a closed action.
- **Decision:** Model review as an explicit decision with a recorded outcome.
- **Rationale:** A workflow can only advance on state changes; a read changes nothing.
- **Consequence:** The `ServiceRequest` gains explicit review-outcome states and a rejection terminal.

### ADR-002 — Review outcomes: Approve / Reject only (for now)
- **Context:** Review could also support "Return for info" (missing data → client resubmits).
- **Options:** (a) Approve/Reject only. (b) Add Return-for-info resubmit cycle.
- **Decision:** **Approve / Reject only.**
- **Rationale:** Fully satisfies the brief; keeps the first model lean and shippable.
- **Tradeoff / future work:** "Return for info" is more realistic but adds a resubmit cycle and a
  `NEEDS_INFO` state. **Documented as a future extension**, not built now.

### ADR-003 — No intermediate `UNDER_REVIEW` ("claimed") state (for now)
- **Context:** With multiple coordinators, two could work the same request simultaneously.
- **Options:** (a) Single logical queue, no claim state. (b) Add `UNDER_REVIEW` claim/lock.
- **Decision:** **Skip `UNDER_REVIEW` for now.**
- **Rationale:** Avoids concurrency machinery the brief doesn't require yet.
- **Tradeoff / future work:** A claim state (with row locking / `select_for_update`) is the honest
  answer to *"how would you scale this to a coordinator team?"* — a great talking point.

### ADR-004 — Single env-driven `settings.py` (chosen *ahead of* the split-settings "standard")
- **Context:** The Django scaffold ships one dev-default `settings.py` with hardcoded secrets. A
  common production "standard" is a **split settings package** (`base.py` / `dev.py` / `prod.py`
  selected via `DJANGO_SETTINGS_MODULE`). We also intend to deploy on Kubernetes.
- **Decision:** Keep **one `settings.py`** and drive every environment-specific value
  (`SECRET_KEY`, `DEBUG`, `ALLOWED_HOSTS`, database, etc.) from **environment variables**.
- **Why ahead of the split-settings standard:**
  - **Twelve-factor:** config lives in the environment, not in code; *one* build artifact is
    promoted unchanged across environments.
  - **Kubernetes-native:** environments differ by **ConfigMap + Secret**, not by code path. The
    container image is identical dev→prod. Split settings would *fight* this — you'd bake env
    identity into the image or juggle `DJANGO_SETTINGS_MODULE` per deployment.
  - **Less machinery** while learning; smaller surface to reason about.
  - **Reversible:** we can graduate to a settings package later if per-env needs diverge.
- **Tradeoff / when to revisit:** Split settings earn their keep when environments need different
  *code* (different middleware, installed apps, logging), not just different *values*. If ours
  diverges that far, revisit.

### ADR-005 — Build deployment-aware; target = Kubernetes homelab + CI/CD via a "walking skeleton"
- **Context:** We want to deploy to a personal Kubernetes homelab and run CI/CD, building with
  deployment in mind from the start.
- **Decision:** Adopt a **walking skeleton** — keep the app twelve-factor / container-ready from
  day one, stand up a *thin* CI pipeline and deployment path early, and grow them with the app.
  Do **not** build the full platform before there is an app to deploy.
- **Best-practice note:** Deployment-aware building is standard and good; premature, elaborate
  infra before a runnable app is an anti-pattern. Knowing that line is itself an interview signal.
- **Consequences K8s forces (decide soon):**
  - **SQLite → Postgres** (pods ephemeral / multi-replica; no file DB in-container).
  - **App server:** Gunicorn (WSGI).
  - **Static files:** WhiteNoise (simplest for a homelab).
  - **Health endpoint** for liveness/readiness probes.
  - **Migrations** run as a deliberate Job/init step, not a naïve container-start hook.
  - **Config via ConfigMap + Secret** — exactly *why* ADR-004's env-driven settings pays off.

### ADR-006 — User & role model: custom `AbstractUser`, email login, single-role RBAC + per-role profiles
- **Context:** Three actors (Client, Coordinator, Personnel) **all authenticate**. Coordinator-facing
  tool **designed as-if HIPAA applies** (technical safeguards; synthetic data — see caveats below).
  Django makes the User model a **one-way door**: cleanly swappable only *before the first migration*.
- **Key insight:** The User *class* is **orthogonal** to authorization and HIPAA — both come from
  *controls* (permissions/groups, audit, encryption, sessions), not from which User model you pick.
  The default `User` could technically host them.
- **Decisions:**
  1. **Custom user via `AbstractUser`** (extend Django's user, keep its auth machinery, add fields) —
     **not** `AbstractBaseUser` (a full rebuild), because we extend rather than change how auth works.
  2. **Login identity = email** (`USERNAME_FIELD = 'email'`), not username.
  3. **Single `role` enum on User** — `CLIENT | COORDINATOR | PERSONNEL`. **Exactly one role per
     person**; holding two roles requires **separate accounts**.
  4. **RBAC enforcement via Django Groups/Permissions** mapped to roles (least privilege / HIPAA
     "minimum necessary"). The `role` enum is the **source of truth**; group membership is **kept in
     sync** with it (mechanism TBD — `post_save` signal vs. derive-from-role).
  5. **Per-role `OneToOne` profile models** for differing data: `ClientProfile`,
     `PersonnelProfile` (skills / availability / capacity → later feeds the eligibility query),
     `CoordinatorProfile` *only if* it carries data.
- **Rationale:** Given the one-way door and the security fields we'll inevitably add (MFA,
  last-password-change, forced-reset), **custom is the lower-total-effort path** — i.e. the "easier
  option to achieve it" over the project's life, not just today. One-role rule prevents privilege
  bleed. The audit-history requirement **is** HIPAA's "audit controls" safeguard.
- **Tradeoffs / notes:**
  - Email login means overriding `USERNAME_FIELD` **and** the user manager (`create_user` /
    `create_superuser` keyed on email).
  - `role` ↔ Group **sync** is a real consistency concern to implement carefully.
  - **Must be created before the first migration.**
- **HIPAA caveats (say precisely):** (a) learning project with **synthetic data** — we *design as-if*,
  not certify; (b) the app implements only HIPAA **technical safeguards** — full compliance also needs
  hosting BAAs, encryption-at-rest, and org policy **outside the app**.

### ADR-007 — App boundaries: `accounts` + `servicing` + `audit` (modular monolith within apps)
- **Context:** App layout must be settled before the first migration (custom user must precede it, per
  ADR-006). Question was how finely to split.
- **Principle:** An app boundary = a **bounded context**, and the real test is **coupling vs.
  cohesion** — *not* category. Things that change together and reference each other constantly belong
  in the **same app**. Separation of concerns is achieved with **internal modules/packages**, and app
  boundaries are reserved for clusters that are genuinely **loosely coupled**.
- **Decision — three apps:**
  - **`accounts`** — custom `User`, roles, per-role profiles. Foundational; everything depends on it.
  - **`servicing`** — the whole request lifecycle: `ServiceRequest` + review, `Assignment` +
    accept/decline, **and** the recommendation/eligibility layer. Request ↔ recommendation ↔
    fulfilment are **mutually tightly coupled** (can't be split without cross-app FKs pointing every
    way), so they live together — **internally modular** (`services/recommendation.py`, `tasks.py`, …).
  - **`audit`** — cross-cutting append-only log; its own app. (How it references many models without a
    dependency tangle is deferred to the audit-strategy ADR.)
  - **notifications** — a **service module inside `servicing`** for now (a leaf / integration
    boundary); promote to its own app only if it grows.
- **Dependency DAG:** `notifications-module → servicing → accounts`; `audit` kept minimal. No cycles.
- **Tradeoff / revisit:** Split `servicing` into `requests` / `fulfilment` (or extract recommendation)
  only if a sub-cluster earns an **independent lifecycle**. Internal modules make that split cheap later.

### ADR-008 — Recommendation layer (rule-based, sync, swappable) + Celery introduced for notifications
- **Context:** The "identify eligible personnel" step is a **recommendation/eligibility layer**,
  **rule-based** this phase, expected to get heavier later (ML / external availability systems). Step 7
  (SaaS notification) is external I/O.
- **Key principle:** Celery (async task queue) is warranted for work that is **slow, external +
  retry-prone, CPU-heavy, or scheduled** — *not* for a fast deterministic DB query. Forcing everything
  async is an anti-pattern (worse UX, needless broker/worker/task-state machinery).
- **Decisions:**
  1. **Recommendation = synchronous rule-based service** behind a `RecommendationEngine` interface
     (**Strategy pattern**): `RuleBasedRecommender` now, swappable for `MLRecommender` later
     (**Open/Closed**). Written **Celery-ready** (pure service, no request objects) but **run
     synchronously** this phase — it's a millisecond DB filter.
  2. **Celery introduced now for the notification step** (external, flaky, needs **retry/backoff**) —
     the textbook async use, and it gets the queue into the walking-skeleton early.
  3. **Recommendation graduates to a Celery task** only when it becomes slow (ML) or calls external
     services.
- **Deployment consequence (ties to ADR-005):** adds a **broker** (Redis — simplest for the homelab) +
  a **separate Celery worker Deployment** (same image, different command) on K8s.
- **Lives in:** the `servicing` app — recommendation as a service module, notification as a Celery task.

### ADR-009 — User provisioning & per-role profiles (refines ADR-006)
- **Context:** Deciding which profiles exist, how they're created, and how accounts are provisioned.
- **Decisions:**
  1. **Profile models:** `ClientProfile`, `PersonnelProfile`, **and `CoordinatorProfile`** — all three
     roles carry role-specific stored attributes. *(Revised: a coordinator manages a `department` and
     `region`, so it does carry data. A profile is justified by stored **attributes**, not behavior.)*
  2. **Provisioning channels:** Clients & Personnel **self-register** via a form that collects their
     profile data; **Coordinators are admin-provisioned** (internal staff; role set in admin) with no
     self-registration — the admin flow creates their `CoordinatorProfile` (department, region).
  3. **Creation mechanism (D2): explicit service-layer creation** — a function creates the `User` +
     matching profile **atomically** (`transaction.atomic()`). Chosen over `post_save` signals for
     traceability/testability. **Known cost:** must be invoked everywhere app users are created;
     `createsuperuser` and admin-created coordinators are separate provisioning paths that bypass it.
  4. **Scope (D3): scaffold minimal fields now, grow later.** `PersonnelProfile` eligibility fields are
     deferred; any field added later must be `null`/`blank` or defaulted to keep migrations clean.
- **Invariant:** every user has exactly one matching profile — Client → ClientProfile,
  Personnel → PersonnelProfile, Coordinator → CoordinatorProfile.
- **Wrinkle — profile half RESOLVED (2026-08-01):** admin-created coordinators get their
  `CoordinatorProfile` via **role-aware `StackedInline`s** on the User admin — `get_inlines()` shows
  only the profile matching `obj.role` (two-step: save user → fill the profile inline; atomicity comes
  from the admin's own transaction; deliberately *not* forced, since superusers are `role=CLIENT` and
  need no profile). **Still open:** their `COORDINATOR` **group** assignment (ADR-006 role↔Group sync)
  — pending the groups work.

### ADR-010 — `servicing` foundation: request type, state representation, transitions, review outcome
- **Context:** First slice of the `servicing` app. Four coupled questions had to be settled *before* any
  model was written: how the **request-type vocabulary** is stored, how a request's **state** is
  represented, how **transitions** execute safely, and whether **review** is an entity or a set of
  attributes.
- **Cross-cutting principle (this is what settles D1 vs. D4, which otherwise look inconsistent):** build
  for **committed work, not imaginable work**; and where uncertain, prefer the option whose *"I was
  wrong"* migration is **cheap and lossless**. One-row-becomes-many is a **widening** change (mechanical);
  many-rows-become-one is **lossy** (you must choose which row survives).

**D1 — `RequestType` is a model (reference table), not a `TextChoices` enum.**
- Two **committed** §7 items hang off request type: **coordinator↔type routing** and **eligibility
  rules**. Routing is genuinely **many-to-many** (a coordinator handles several types; a type is handled
  by several coordinators), so a **join table** is required *either way* — the only real question is
  whether its right-hand column holds an unvalidated **string** or an **FK**. The FK buys **referential
  integrity** free: you cannot reference a type that doesn't exist, nor delete one still in use. The enum
  version is "a lookup table with extra steps, minus the integrity."
- An enum member **cannot carry data**, and eligibility is data *about a type*. As a model,
  `required_sector` is just a column, and "identify eligible personnel" collapses to a query —
  `PersonnelProfile.objects.filter(sector=req.request_type.required_sector, availability_status=AVAILABLE)`
  — matching the §3 insight that eligibility is **a query, not a state**.
- **Costs accepted:** a join on read (`select_related`), no code-level constants (tests/fixtures must
  create rows), one more model + migration. **Bought:** *runtime extensibility* — the vocabulary changes
  without a deploy.

**D2 — State representation is the **hybrid**: a mutable `status` column *plus* an append-only log.**
- Three shapes considered: **(a) mutable `status` only** — trivial, but overwrites destroy history;
  disqualifying for a system whose stated purpose includes recording every action. **(b) Event sourcing**
  — events are the source of truth, current state is replayed; perfect history, but every read becomes a
  replay, forcing **projections** — two systems and a sync process, a heavy architecture for this problem.
  **(c) Hybrid.**
- **Chosen: (c).** `status` is the **read model** (indexed, fast — what a coordinator dashboard filters
  on); `AuditEvent` is the **write history** (immutable `from`/`to`/`actor`/`timestamp`). Same information,
  two shapes, two jobs — a mild form of **CQRS**.
- **Already implied by ADR-007** (an append-only `audit` app *and* a `status` field); ADR-010 promotes it
  to an explicit state-representation decision.
- **Known cost:** the two halves can drift out of sync. Mitigated entirely by D3.

**D3 — Every transition goes through the `servicing` service layer; nothing writes `status` directly.**
- Continues the `accounts/services.py` pattern (ADR-009 D2): a named function owns the status write **and**
  the audit row inside one `transaction.atomic()` — all-or-nothing, so the history cannot develop holes.
- The §6 state machine is encoded as an explicit **transition map**, checked before every write:
  ```
  SUBMITTED             → REJECTED, READY_FOR_ASSIGNMENT
  READY_FOR_ASSIGNMENT  → ASSIGNED
  ASSIGNED              → IN_PROGRESS, READY_FOR_ASSIGNMENT   (declined → back to the pool)
  IN_PROGRESS           → FULFILLED
  REJECTED, FULFILLED   → terminal
  ```
- **Principle worth saying aloud: constraints validate *states*; application code validates
  *transitions*.** A `CheckConstraint` only sees the row **as it is written** — never the previous value —
  so it can enforce *"status is one of six values"* but never *"`FULFILLED` only from `IN_PROGRESS`"*.
  (A DB trigger could, at the cost of scattering business rules across two languages — rejected.)
- **Concurrency:** two coordinators assigning the same request both read `READY_FOR_ASSIGNMENT` *before*
  either writes, so both pass the check → two assignments, one request. Fixed with `select_for_update()`
  (a **pessimistic row lock**) inside the atomic block: the second reader blocks, then re-reads fresh
  state and is correctly rejected. **Caveats:** must be inside `transaction.atomic()` (Django errors
  otherwise), and it is a **silent no-op on SQLite** (file-level, not row-level locking) — it only becomes
  real on Postgres (ADR-005). Written now anyway, because the bug is invisible in dev and appears in
  production under load.

**D4 — Review outcome lives as **inline fields** on `ServiceRequest`, not a `Review` model.**
- Three options: **(a) inline fields**, **(b) a `Review` model**, **(c) nothing — read it from the audit
  log** (the fields are, after all, a **denormalisation** of data `AuditEvent` already holds).
- **(c) rejected on principle:** *an audit log should be deletable without changing application
  behaviour* — you'd lose history, not function. Reading it for domain data makes `servicing` depend on
  `audit`'s internals (against ADR-007's dependency direction) and forces domain-specific prose
  (`rejection_reason`) into a generic, untyped log.
- **Cardinality is provable, not assumed:** `SUBMITTED` is the initial state and the *only* entry to the
  review gate; both exits (`REJECTED` **terminal**, `READY_FOR_ASSIGNMENT`) never return; the single loop
  (`ASSIGNED` → decline → `READY_FOR_ASSIGNMENT`) lands **past** the gate. **No transition returns to
  `SUBMITTED`** ⇒ a request is reviewed **exactly 0 or 1 times**. One review ⇒ attributes, not rows.
- **Corroborated by ADR-003:** rejecting an `UNDER_REVIEW` ("claimed") state means review is an
  **instantaneous act**, not a process with a lifecycle. Duration and intermediate states earn an entity;
  instantaneous acts are attributes. The ADRs compose rather than contradict.
- **Confirmed 2026-08-02:** one coordinator's approve/reject **is** the complete gate, and **a rejected
  request is final** — no appeal path, no second clinical/financial sign-off foreseen.
- **Fields:** `reviewed_by` (FK, nullable), `reviewed_at` (nullable), `rejection_reason`.
- **Required companion — a `CheckConstraint`** (this *is* a single-row state rule, so per D3 the DB can
  and should enforce it): `status = SUBMITTED` ⇒ `reviewed_by`/`reviewed_at` **NULL**; `status ≠ SUBMITTED`
  ⇒ both **set**; `status = REJECTED` ⇒ `rejection_reason` **non-empty**. This converts three loose,
  mutually-dependent nullable columns into a **DB-enforced invariant** that holds even if the service
  layer is bypassed — strictly stronger than a `Review` model gives by default.
- **REVISIT TRIGGER (mechanically checkable):** the moment **any transition into `SUBMITTED`** is added to
  the D3 map — appeal, un-reject, "revise and resubmit" — cardinality becomes many and `Review` must
  become a model. The migration is mechanical (three columns → one row each, drop columns). *Most likely
  challenger: **"revise and resubmit"**, since terminal rejection is a deliberate choice.* **Multi-stage
  approval** (two *different* concurrent sign-offs) would also force (b), and cannot be represented inline
  at any price.

**D5 — `ServiceRequest` field decisions.**
- `client` FK → `settings.AUTH_USER_MODEL`, **`on_delete=PROTECT`**. `CASCADE` (correct for profiles,
  which are meaningless without their user) would silently erase service history — unacceptable in an
  audit-oriented system. `PROTECT` fails the delete **loudly**; de-identification (anonymise the person,
  keep the records) is a separate, deliberate operation.
- Plus `request_type` (FK), `title`, `description`, `status`, `created_at`, `updated_at`.

**D6 — Slice scope: `RequestType` + `ServiceRequest` + submit/approve/reject only.**
- `Assignment` is deferred to the next slice: it carries its **own** state machine (`PENDING → ACCEPTED /
  DECLINED`, §3 insight 1). Two interacting state machines in one migration means debugging both at once.

### ADR-011 — Asynchronous workflow: notification points, fulfilment ownership, fulfilment window
- **Context (2026-08-02):** Modelling the *implemented* state machine exposed that `TRANSITIONS` declares
  seven edges but only three can be performed. Three of the four gaps were the deliberate D6 deferral;
  the fourth — **`IN_PROGRESS → FULFILLED` had no owner at all** — was a real hole never covered by
  ADR-001/002, which settled only the *review* gate. Clarifying it surfaced the async/notification shape.
- **Key distinction (worth stating aloud):** *workflow-async* ≠ *technically-async*.
  - The client submits and does **not** wait for the outcome; review happens hours or days later. The
    state machine is **already** async in this sense **by construction** — no machinery required.
  - Only **notification delivery** needs technical async (external, flaky, retry-prone) — exactly the
    Celery use ADR-008 already authorised. So this ADR names **unbuilt work**, not an architecture change.

**D1 — "Request received, under review" is the HTTP response, not a notification.** Only outcomes the
  client cannot see synchronously get pushed.

**D2 — Notification points (client-facing, Celery tasks):** on **rejection**, and on **personnel
  acceptance**. *Open: whether fulfilment also notifies the client.*

**D3 — Notifications dispatch via `transaction.on_commit()`; audit writes stay inside the transaction.**
- Both hang off the same seam (a transition happened) but must fire at **different moments**:
  ```python
  with transaction.atomic():
      _record_transition(...)                               # INSIDE  -- commits atomically with status
      transaction.on_commit(lambda: notify_client.delay())  # AFTER   -- must not fire on rollback
  ```
- **Why:** enqueuing a Celery task inside the transaction produces one of two classic bugs — the worker
  picks the task up **before the commit lands** and reads a row that does not exist yet, or the
  transaction **rolls back** and the client has already been told about a rejection that never happened.
  The most common Django + Celery mistake; `on_commit` is the fix.

**D4 — `IN_PROGRESS → FULFILLED` is performed by the assigned personnel.** Closes the ADR-011 context gap.
- Consistent with the §4 principle: completion **is** a decision (someone asserts the work is done), not
  an automatic system event as §2's "System updates status" implied.
- **Consequence:** identifying *the assigned personnel* requires `Assignment`, so `fulfil_request()`
  cannot precede the Assignment slice. Confirms ADR-010 D6's ordering.

**D5 — Fulfilment is only accepted inside a scheduled window.**
- `ServiceRequest` gains **`scheduled_start`** + **`expected_duration`**; `fulfil_request()` is allowed
  only when `scheduled_start <= now <= scheduled_start + expected_duration + grace`.
  - **Too early** → "work has not started yet". **Too late** → "window closed, contact coordinator".
- **Note this is a *transition guard*, not a `CheckConstraint`** — it depends on `now()` and on the prior
  state, so per ADR-010 D3 it belongs in the service layer.
- **Open:** where `scheduled_start` comes from (client-requested at submission vs. coordinator-set at
  assignment); the grace period; whether `expected_duration` defaults from `RequestType`.

**D6 — Deploy scope for 2026-08-02: the review slice only.** Tests + admin + settings/env hygiene ship;
  `Assignment`, notifications (Celery + Redis broker + worker) and scheduling are **decided but not
  built**. Rationale: introducing a broker and a worker process for the first time on deploy day is the
  risk, not the code.

---

## 6. ServiceRequest state machine (current target)

| State | Meaning | Reachable from |
|---|---|---|
| `SUBMITTED` | Created by client, awaiting review | (initial) |
| `REJECTED` | Coordinator rejected (terminal) | `SUBMITTED` |
| `READY_FOR_ASSIGNMENT` | Approved; eligible personnel can be assigned | `SUBMITTED`, `ASSIGNED` (on decline) |
| `ASSIGNED` | Personnel assigned, awaiting their response | `READY_FOR_ASSIGNMENT` |
| `IN_PROGRESS` | Personnel accepted | `ASSIGNED` |
| `FULFILLED` | Work complete (terminal) | `IN_PROGRESS` |

*(Assignment has its own smaller machine: `PENDING → ACCEPTED / DECLINED`.)*

---

## 7. Open decisions (next up)

- [x] **User & role model** — **DECIDED, see ADR-006** (custom `AbstractUser`, email login,
      single-role enum + Groups RBAC + per-role `OneToOne` profiles). Must be built before first migration.
- [x] **App boundaries** — **DECIDED, see ADR-007**: `accounts` + `servicing` (request + recommendation
      + fulfilment, internally modular) + `audit`; notifications a module inside `servicing`.
- [x] **Recommendation layer & Celery** — **DECIDED, see ADR-008**: sync rule-based Strategy service,
      Celery-ready; Celery introduced now for notifications.
- [x] **`servicing` foundation** — **DECIDED, see ADR-010**: `RequestType` as a reference table, hybrid
      state representation (`status` column + append-only audit log), all transitions through the service
      layer with a transition map + `select_for_update()`, review outcome inline, `Assignment` deferred.
- [x] **Async workflow, notification points & fulfilment** — **DECIDED, see ADR-011**: personnel owns
      `IN_PROGRESS → FULFILLED` inside a scheduled window; notifications on rejection + acceptance,
      dispatched via `transaction.on_commit()`. Decided, **not built**.
- [ ] **State-machine gaps still open** *(surfaced 2026-08-02 when the implemented machine was modelled;
      gap #1, fulfilment ownership, was closed by ADR-011 D4)*:
      1. **No failure path after approval** — `REJECTED` is reachable only from `SUBMITTED`, so the model
         cannot express *work started and could not be completed*.
      2. **No cancellation** — a client whose need disappears has no exit, and `PROTECT` means the
         request cannot be deleted either.
      3. **Unbounded decline loop** — `ASSIGNED → READY_FOR_ASSIGNMENT → ASSIGNED → …` has no attempt
         counter and no escape, so a request nobody accepts cycles forever undetected.
- [ ] **Eligibility rules** — what makes personnel "eligible" (skills, availability, location)? *Its
      **home** is decided (ADR-010 D1): the rule lives as **data on `RequestType`** (e.g.
      `required_sector`), so eligibility is a **query**, not code. The rule set itself is still open.*
- [ ] **Coordinator ↔ request-type routing** *(deferred from profiles, 2026-08-01)* — a coordinator
      handles **multiple** request types, and `RequestType` is **shared `servicing` vocabulary** (a
      `ServiceRequest` has a type too). Model it in the **`servicing`** app alongside eligibility/routing
      — as a `RequestType` concept + a coordinator-side **association living in `servicing`**, *not* a
      field on `CoordinatorProfile` (that would create an `accounts → servicing` dependency **cycle**).
      `CoordinatorProfile` stays `department` + `region` for now. *Mechanism now decided (ADR-010 D1):
      a **`ManyToManyField`** from a `servicing`-side coordinator association to `RequestType`. Not yet
      built — deferred past the D6 slice.*
- [ ] **Audit strategy** — how `AuditEvent` records every action immutably. *Its **role** is decided
      (ADR-010 D2/D3): the write-history half of the hybrid, written in the same `transaction.atomic()`
      as the status change. Still open: its schema and how it references many models without a
      dependency tangle.*
- [ ] **External SaaS notification** — *delivery mechanism decided (Celery task, ADR-008)*; still open:
      which provider, payload/contract, idempotency & failure handling.
- [ ] **Settings/env hygiene** — move `SECRET_KEY`, `DEBUG`, `ALLOWED_HOSTS`, DB to env vars (per ADR-004).
- [ ] **Deployment stack (per ADR-005)** — Postgres, Gunicorn, WhiteNoise, health endpoint, Dockerfile.
- [ ] **CI/CD** — pipeline tool (e.g. GitHub Actions) + delivery to K8s (direct `kubectl` vs. GitOps/Argo CD).

---

*Last updated: 2026-08-02*
