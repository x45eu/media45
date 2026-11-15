"""Security invariants for the public Docker deployment template."""
from pathlib import Path
from django.conf import settings
from django.test import SimpleTestCase


class DeploymentSecurityTests(SimpleTestCase):
    def test_secure_application_defaults(self):
        self.assertFalse(settings.DEBUG)
        self.assertTrue(settings.SESSION_COOKIE_SECURE)
        self.assertTrue(settings.CSRF_COOKIE_SECURE)
        self.assertTrue(settings.SESSION_COOKIE_HTTPONLY)
        self.assertEqual(settings.X_FRAME_OPTIONS, "DENY")

    def test_default_docker_publish_uses_loopback(self):
        compose = (Path(settings.BASE_DIR) / "compose.yaml").read_text()
        self.assertIn("${MEDIA45_BIND_ADDRESS:-127.0.0.1}:3000:8000", compose)
