#!/bin/sh
# One image, three roles. Which one is chosen by the container's args, so the
# web Deployment, the worker Deployment and the migration Job all run the
# identical image digest -- no chance of the worker running different code from
# the web pods, which is exactly the drift that makes "works in web, fails in
# the worker" bugs so hard to see.
#
#   args: ["web"]      gunicorn, serves HTTP
#   args: ["worker"]   celery, drains the notification queue
#   args: ["migrate"]  applies migrations, then exits 0  (a Job, not a sidecar)
#
# `set -e` so any failure exits non-zero: a Job that fails must be visible as a
# failed Job, not a silent success.
set -e

case "$1" in
  web)
    # exec replaces this shell with gunicorn as PID 1, so SIGTERM from the
    # kubelet reaches gunicorn directly. Without exec, the shell is PID 1,
    # ignores the signal, and every rollout waits the full termination grace
    # period before the pod is killed -- deploys crawl for no visible reason.
    exec gunicorn config.wsgi:application --config config/gunicorn.py
    ;;

  worker)
    # --without-gossip/--without-mingle: worker-to-worker chatter that is pure
    # overhead when notifications are the only workload.
    exec celery -A config worker \
      --loglevel="${CELERY_LOG_LEVEL:-info}" \
      --concurrency="${CELERY_CONCURRENCY:-2}" \
      --without-gossip --without-mingle
    ;;

  migrate)
    # Deliberately NOT run at web startup. With more than one replica, every
    # pod would run migrations simultaneously against the same database --
    # racing on the same DDL. Django takes a lock per migration, so the usual
    # outcome is a slow deploy rather than corruption, but it is still several
    # pods fighting over schema changes at the worst possible moment. A Job
    # runs exactly once, and Phase 2 gates the Deployment behind it.
    exec python manage.py migrate --noinput
    ;;

  *)
    # Anything else runs verbatim: `kubectl exec ... -- python manage.py shell`,
    # one-off management commands, debugging.
    exec "$@"
    ;;
esac
