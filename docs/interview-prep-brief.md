# BMH Service Request & Fulfilment Hub — Interview Prep Brief

> A living design document. We add to it as we make decisions. It doubles as your
> interview talking-point script: every decision below is something you can defend out loud.

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
- **Open wrinkle:** admin-created coordinators bypass the self-registration service, so **both** their
  `CoordinatorProfile` **and** their `COORDINATOR` group assignment (ADR-006 role↔Group sync) must be
  handled at the **admin layer** (resolve when we build groups).

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
- [ ] **Eligibility rules** — what makes personnel "eligible" (skills, availability, location)?
- [ ] **Coordinator ↔ request-type routing** *(deferred from profiles, 2026-08-01)* — a coordinator
      handles **multiple** request types, and `RequestType` is **shared `servicing` vocabulary** (a
      `ServiceRequest` has a type too). Model it in the **`servicing`** app alongside eligibility/routing
      — as a `RequestType` concept + a coordinator-side **association living in `servicing`**, *not* a
      field on `CoordinatorProfile` (that would create an `accounts → servicing` dependency **cycle**).
      `CoordinatorProfile` stays `department` + `region` for now.
- [ ] **Audit strategy** — how `AuditEvent` records every action immutably.
- [ ] **External SaaS notification** — *delivery mechanism decided (Celery task, ADR-008)*; still open:
      which provider, payload/contract, idempotency & failure handling.
- [ ] **Settings/env hygiene** — move `SECRET_KEY`, `DEBUG`, `ALLOWED_HOSTS`, DB to env vars (per ADR-004).
- [ ] **Deployment stack (per ADR-005)** — Postgres, Gunicorn, WhiteNoise, health endpoint, Dockerfile.
- [ ] **CI/CD** — pipeline tool (e.g. GitHub Actions) + delivery to K8s (direct `kubectl` vs. GitOps/Argo CD).

---

*Last updated: 2026-08-01*
