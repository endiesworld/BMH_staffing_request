"""Tests for the Kubernetes probe endpoints (config/health.py)."""

from unittest import mock

from django.conf import settings
from django.db import DatabaseError
from django.test import SimpleTestCase, TestCase, override_settings


class LivenessTests(SimpleTestCase):
    """SimpleTestCase: liveness must not need a database, and neither must its test."""

    def test_live_returns_200(self):
        response = self.client.get("/healthz/live")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"ok\n")

    @override_settings(ALLOWED_HOSTS=["bmh.example.com"])
    def test_live_answers_a_probe_addressed_to_the_pod_ip(self):
        """The failure this middleware exists to prevent.

        kubelet sends probes to the pod IP, which is never in ALLOWED_HOSTS.
        Routed through urls.py this would be a 400 DisallowedHost, the probe
        would fail forever, and the pod would CrashLoopBackOff.
        """
        response = self.client.get("/healthz/live", headers={"host": "10.42.3.17:8000"})
        self.assertEqual(response.status_code, 200)

    @override_settings(ALLOWED_HOSTS=["bmh.example.com"])
    def test_a_normal_url_still_rejects_an_unknown_host(self):
        """Guard against the middleware disabling host checking generally."""
        response = self.client.get("/", headers={"host": "10.42.3.17:8000"})
        self.assertEqual(response.status_code, 400)


class ReadinessTests(TestCase):
    def test_ready_returns_200_when_the_database_answers(self):
        response = self.client.get("/healthz/ready")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"ok\n")

    def test_ready_returns_503_when_the_database_is_unreachable(self):
        """A pod that cannot reach Postgres must leave the Service endpoints,
        not be restarted -- so this is 503, and liveness stays 200."""
        with mock.patch(
            "config.health.connection.cursor",
            side_effect=DatabaseError("could not connect to server"),
        ):
            response = self.client.get("/healthz/ready")
            self.assertEqual(response.status_code, 503)
            self.assertIn(b"could not connect to server", response.content)

            # The critical pairing: the same outage must NOT fail liveness.
            self.assertEqual(self.client.get("/healthz/live").status_code, 200)

    def test_probes_are_not_ssl_redirected(self):
        """SECURE_SSL_REDIRECT must not break liveness.

        In production kubelet probes arrive over plain http with no
        X-Forwarded-Proto, so SecurityMiddleware would answer 301. kubelet
        treats a 3xx as a failure, kills the pod, and does it forever.
        HealthCheckMiddleware sitting above SecurityMiddleware is what prevents
        that -- this test fails the moment someone reorders MIDDLEWARE.

        MIDDLEWARE is re-passed unchanged so that Django rebuilds the handler;
        SecurityMiddleware caches SECURE_SSL_REDIRECT in __init__, so without
        that the override would not take effect.
        """
        with override_settings(
            MIDDLEWARE=settings.MIDDLEWARE, SECURE_SSL_REDIRECT=True
        ):
            for path in ("/healthz/live", "/healthz/ready"):
                with self.subTest(path=path):
                    response = self.client.get(path)
                    self.assertEqual(response.status_code, 200)

            # Sanity: the redirect really is switched on for everything else.
            self.assertEqual(self.client.get("/accounts/login/").status_code, 301)

    def test_health_middleware_precedes_security_middleware(self):
        """The ordering invariant both probe behaviours depend on."""
        order = list(settings.MIDDLEWARE)
        self.assertLess(
            order.index("config.health.HealthCheckMiddleware"),
            order.index("django.middleware.security.SecurityMiddleware"),
        )

    def test_probes_do_not_require_authentication(self):
        """They are unauthenticated by construction (no login here), but assert
        it: a future global login-required middleware placed above this one
        would silently 302 every probe."""
        for path in ("/healthz/live", "/healthz/ready"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)
