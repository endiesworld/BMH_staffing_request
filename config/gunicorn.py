"""Gunicorn configuration.

`manage.py runserver` is single-threaded, reloads on file changes, and is
documented as unsuitable for production. Gunicorn is the WSGI server that
actually runs the app: it forks a pool of worker processes, each handling
requests one at a time, and restarts any that die.

Values are env-driven so a Deployment can tune them without a rebuild.
"""

import os

# Listen on all interfaces. Inside a container "localhost" means the container
# itself, so binding to 127.0.0.1 would make the port unreachable from the
# Service -- the pod would pass no probes and serve nothing.
bind = f"0.0.0.0:{os.environ.get('PORT', '8000')}"

# Each worker is a full OS process with its own memory and its own database
# connections. The usual starting point is (2 x cores) + 1, but in Kubernetes
# the pod's CPU *limit* is what matters, not the node's core count -- os.cpu_count()
# reports the node's, so autoscaling off it over-provisions badly. Set it
# explicitly and scale by adding pods instead.
#
# Remember the arithmetic from CONN_MAX_AGE: pods x workers = held Postgres
# connections, against a default max_connections of 100.
workers = int(os.environ.get("GUNICORN_WORKERS", "3"))

# Sync workers: one request per worker at a time. Correct for this app -- the
# code is ordinary blocking Django, and gevent/eventlet workers would require
# monkey-patching the world for no benefit while nothing here is IO-bound in a
# way that would profit.
worker_class = "sync"

# A worker silent this long is assumed hung and is killed and replaced. Note
# this is a hard ceiling on any single request: a slow report that legitimately
# takes longer will be killed mid-flight, which is a reason to move such work to
# Celery rather than to raise this number.
timeout = int(os.environ.get("GUNICORN_TIMEOUT", "30"))

# Keep the connection open briefly for reuse. Low on purpose: the tunnel, not a
# browser, is the client here, and idle keep-alive workers are workers not
# serving anyone.
keepalive = 5

# Recycle workers periodically. Cheap insurance against a slow memory leak in
# any dependency: a leaking worker is replaced before it grows large enough to
# be OOM-killed by the kubelet. The jitter stops all workers recycling on the
# same request count, which would empty the pool at once.
max_requests = int(os.environ.get("GUNICORN_MAX_REQUESTS", "1000"))
max_requests_jitter = 100

# Logs go to stdout/stderr, never to files. Container filesystems are
# ephemeral, so a log file vanishes with the pod, and `kubectl logs` reads the
# streams. "-" means stdout.
accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("GUNICORN_LOG_LEVEL", "info")

# Health probes hit /healthz/* every few seconds forever. Left in, they would
# drown the access log and make it useless for seeing real traffic.
class _SkipProbes(object):
    def filter(self, record):
        return "/healthz/" not in record.getMessage()


logconfig_dict = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {"skip_probes": {"()": _SkipProbes}},
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stdout",
            "filters": ["skip_probes"],
        },
    },
    "root": {"handlers": ["console"], "level": loglevel.upper()},
    "loggers": {
        "gunicorn.access": {
            "handlers": ["console"],
            "level": loglevel.upper(),
            "propagate": False,
        },
    },
}

# preload_app stays False. It would fork workers from one already-imported app,
# saving memory via copy-on-write -- but it also opens the database connection
# before the fork, so every worker would inherit and share the same socket,
# which corrupts under concurrent use.
preload_app = False
