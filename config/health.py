"""Kubernetes probe endpoints.

Two endpoints, not one, because liveness and readiness answer different
questions and wiring them the same way round is actively harmful:

  /healthz/live   "Is this process alive?"  Failure => kubelet RESTARTS the pod.
                  So it must depend on NOTHING external. If this checked the
                  database, a ten-second Postgres blip would restart every pod
                  at once, turning a recoverable outage into a thundering herd
                  of cold starts against an already-struggling database.

  /healthz/ready  "Can this process serve traffic right now?"  Failure => kubelet
                  pulls the pod OUT of the Service endpoints but leaves it
                  running. That is exactly the right response to an unreachable
                  database: stop routing here, let it recover, rejoin. It is
                  also what makes rolling deploys safe -- a pod that has booted
                  but cannot reach Postgres never receives a request.

Served by HealthCheckMiddleware rather than through config/urls.py; see the
middleware's docstring for why that placement is load-bearing.
"""

from django.db import DatabaseError, connection
from django.http import HttpResponse


def live(request):
    """Liveness: the WSGI worker is running and can build a response.

    Deliberately trivial. Anything this touches becomes a reason for
    Kubernetes to kill the container.
    """
    return HttpResponse("ok\n", content_type="text/plain")


def ready(request):
    """Readiness: the dependencies needed to serve a request are reachable.

    Only the database is checked. Redis is deliberately NOT checked: the web
    process only ever *enqueues* notifications via transaction.on_commit, so a
    broker outage degrades notifications but leaves the whole request/assign/
    accept workflow serviceable. Failing readiness for it would take the site
    down to protect a side effect.
    """
    try:
        # A real round trip. `connection.ensure_connection()` alone can be
        # satisfied by a pooled socket that is open but no longer usable.
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except DatabaseError as exc:
        # 503 is the honest code: the service exists but cannot serve now.
        # The body goes to whoever runs `kubectl describe pod`, so name the
        # failure rather than just saying "not ready".
        return HttpResponse(
            f"database unavailable: {exc}\n",
            content_type="text/plain",
            status=503,
        )
    return HttpResponse("ok\n", content_type="text/plain")


# Path -> view. Plain dict lookup so the middleware costs one hash per request.
PROBES = {
    "/healthz/live": live,
    "/healthz/ready": ready,
}


class HealthCheckMiddleware:
    """Answer probe requests before Django validates the Host header.

    This is the whole reason health checks are not just two entries in
    config/urls.py. kubelet addresses probes to the pod IP, so they arrive with
    `Host: 10.42.3.17:8000`. That is not in ALLOWED_HOSTS -- and it cannot be,
    because the IP changes with every pod. Django raises DisallowedHost, the
    probe sees 400, the pod is declared dead and restarted, and it does so
    forever: a CrashLoopBackOff whose cause looks nothing like its symptom.

    `request.path` comes from PATH_INFO and never calls `get_host()`, so
    matching on it here is safe before host validation has happened.

    This must be FIRST in MIDDLEWARE. Probe responses consequently skip the
    security headers added by SecurityMiddleware, which is fine -- no browser
    ever sees them.

    The alternative is to pin `httpHeaders: [{name: Host, value: <hostname>}]`
    on every probe in the Deployment. That works, but it puts a correctness
    requirement in the manifest where it is easy to omit on the next probe
    someone adds. Twenty lines here makes the app correct by default.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        probe = PROBES.get(request.path)
        if probe is not None:
            return probe(request)
        return self.get_response(request)
