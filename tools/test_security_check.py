"""Offline tests for the standalone security-check command."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from security_check import assess, read_env


class SecurityCheckTests(unittest.TestCase):
    def prepare(self, root: Path, *, password="not-an-example-password"):
        (root / "media45").mkdir()
        (root / "media45" / "settings.py").write_text(
            "DEBUG = False\nSESSION_COOKIE_SECURE = True\nCSRF_COOKIE_SECURE = True\nSESSION_COOKIE_HTTPONLY = True\n")
        (root / "compose.yaml").write_text('ports: ["${MEDIA45_BIND_ADDRESS:-127.0.0.1}:3000:8000"]\n')
        (root / ".env").write_text("SECRET_KEY=" + "x" * 50 + "\nPOSTGRES_PASSWORD=" + password +
                                  "\nMEDIA45_HOST=example.org\nMEDIA45_ORIGIN=https://example.org\n")
        (root / ".env").chmod(0o600)

    def test_safe_sample(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            self.prepare(root)
            errors, _, _ = assess(root)
            self.assertEqual(errors, [])

    def test_example_database_password_rejected(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            self.prepare(root, password="REPLACE_WITH_PASSWORD")
            errors, _, _ = assess(root)
            self.assertTrue(any("POSTGRES_PASSWORD" in x for x in errors))

    def test_open_binding_warns(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            self.prepare(root)
            env = root / ".env"
            env.write_text(env.read_text() + "MEDIA45_BIND_ADDRESS=0.0.0.0\n")
            errors, warnings, _ = assess(root)
            self.assertEqual(errors, [])
            self.assertTrue(any("nicht auf Loopback" in x for x in warnings))

    def test_bad_origin_rejected(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            self.prepare(root)
            env = root / ".env"
            env.write_text(env.read_text().replace("https://example.org", "http://example.org"))
            errors, _, _ = assess(root)
            self.assertTrue(any("HTTPS-Origin" in x for x in errors))

    def test_comments_and_quoted_values(self):
        with TemporaryDirectory() as d:
            p = Path(d) / ".env"
            p.write_text('A="hello world"\nB=ok # note\n# ignored\n')
            self.assertEqual(read_env(p), {"A": "hello world", "B": "ok"})


if __name__ == "__main__":
    unittest.main()
