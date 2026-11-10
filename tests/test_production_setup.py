"""Exercise the server setup as an operator would, without Docker or a database."""

import os
from pathlib import Path
import shutil
import stat
import subprocess
from tempfile import TemporaryDirectory
import unittest


@unittest.skipUnless(os.name == "posix" and shutil.which("sh"), "Linux server setup")
class ProductionSetupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix="product deployment ")
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name)
        (self.project / "scripts").mkdir()
        repository = Path(__file__).resolve().parents[1]
        for relative in ("scripts/setup-server.sh", ".env.prod.example"):
            shutil.copy2(repository / relative, self.project / relative)

    def setup_server(self, *arguments):
        return subprocess.run(
            ["sh", str(self.project / "scripts/setup-server.sh"), *arguments],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )

    def test_first_setup_creates_private_secrets_without_logging_values(self):
        result = self.setup_server("monitoring.example.ru")
        self.assertEqual(result.returncode, 0, result.stderr)
        secret_dir = self.project / ".secrets"
        values = {
            name: (secret_dir / name).read_text(encoding="utf-8").strip()
            for name in ("django_secret_key", "db_password", "backup_password")
        }
        self.assertEqual(len(set(values.values())), len(values))
        for value in values.values():
            self.assertGreaterEqual(len(value), 64)
            self.assertNotIn(value, result.stdout + result.stderr)
        self.assertEqual((secret_dir / "openai_api_key").read_bytes(), b"")
        self.assertEqual(stat.S_IMODE(secret_dir.stat().st_mode), 0o700)
        for path in secret_dir.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o444)
        self.assertEqual(stat.S_IMODE((self.project / ".env.prod").stat().st_mode), 0o600)

    def test_rerun_preserves_existing_config_and_every_secret_byte(self):
        first = self.setup_server("monitoring.example.ru")
        self.assertEqual(first.returncode, 0, first.stderr)
        config = self.project / ".env.prod"
        config.write_bytes(config.read_bytes() + b"\n# Operator's custom configuration\n")
        api_key = self.project / ".secrets/openai_api_key"
        api_key.chmod(0o600)
        api_key.write_bytes(b"existing-owner-key\n")
        expected_config = config.read_bytes()
        expected_secrets = {
            path.name: path.read_bytes() for path in (self.project / ".secrets").iterdir()
        }

        for arguments in ((), ("another.example.ru",)):
            with self.subTest(arguments=arguments):
                result = self.setup_server(*arguments)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(config.read_bytes(), expected_config)
                for name, value in expected_secrets.items():
                    self.assertEqual((self.project / ".secrets" / name).read_bytes(), value)
                    self.assertNotIn(value.decode().strip(), result.stdout + result.stderr)

    def test_missing_or_unsafe_domain_fails_before_creating_secrets(self):
        for arguments in (
            (),
            ("https://monitoring.example.ru",),
            ("monitoring.example.ru/path",),
            ("monitoring.example.ru:443",),
            ("monitoring.example.ru\nAPP_IMAGE=untrusted",),
        ):
            with self.subTest(arguments=arguments):
                result = self.setup_server(*arguments)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((self.project / ".env.prod").exists())
                self.assertFalse((self.project / ".secrets").exists())

    def test_secret_directory_symlink_is_rejected_without_changing_target(self):
        with TemporaryDirectory(prefix="existing secrets ") as directory:
            target = Path(directory)
            target.chmod(0o750)
            marker = target / "do-not-change"
            marker.write_bytes(b"existing secret data\n")
            (self.project / ".secrets").symlink_to(target, target_is_directory=True)
            result = self.setup_server("monitoring.example.ru")
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o750)
            self.assertEqual(marker.read_bytes(), b"existing secret data\n")
            self.assertEqual({path.name for path in target.iterdir()}, {"do-not-change"})
            self.assertFalse((self.project / ".env.prod").exists())
