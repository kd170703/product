"""Exercise backup safety without a Docker daemon or production credentials."""

import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

try:
    import fcntl
except ImportError:  # Run the shell integration tests inside Docker on Windows.
    fcntl = None


ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"


@unittest.skipUnless(fcntl is not None, "POSIX shell and flock are tested in Docker/CI")
class BackupCommandTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.bin = self.directory / "bin"
        self.bin.mkdir()
        self.backups = self.directory / "backups"
        self.backups.mkdir()
        self.repo = self.backups / "restic"
        self.repo.mkdir()
        self.log = self.directory / "commands.log"
        self.password = self.directory / "password"
        self.password.write_text("test-only-secret\n")
        self.environment = {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "BACKUP_SCRIPT_DIR": str(SCRIPTS),
            "BACKUP_ROOT": str(self.backups),
            "BACKUP_WORK": str(self.directory / "work"),
            "RESTIC_REPOSITORY": str(self.repo),
            "RESTIC_PASSWORD_FILE": str(self.password),
            "PGPASSWORD_FILE": str(self.password),
            "PGHOST": "test-database",
            "PGUSER": "test-user",
            "PGDATABASE": "test-database",
            "MOCK_COMMAND_LOG": str(self.log),
            "TZ": "Europe/Moscow",
        }
        self.executable(
            "restic",
            """#!/bin/sh
set -eu
printf '%s\n' "$*" >> "$MOCK_COMMAND_LOG"
if [ "$1 $2" = 'cat config' ]; then
    [ "${MOCK_BAD_PASSWORD:-0}" != 1 ] && [ -f "$RESTIC_REPOSITORY/config" ]
    exit $?
fi
case "$1" in
    init) printf 'existing-encrypted-configuration' > "$RESTIC_REPOSITORY/config" ;;
    backup) printf 'retained-snapshot' > "$RESTIC_REPOSITORY/snapshot" ;;
    check) [ "${MOCK_CHECK_FAILURE:-0}" != 1 ] ;;
    forget) touch "$RESTIC_REPOSITORY/retention-applied" ;;
    *) exit 2 ;;
esac
""",
        )
        self.executable(
            "pg_dump",
            """#!/bin/sh
set -eu
printf '%s\n' 'pg_dump' >> "$MOCK_COMMAND_LOG"
[ "${MOCK_DATABASE_FAILURE:-0}" != 1 ] || exit 1
for argument in "$@"; do
    case "$argument" in --file=*) printf 'consistent-database-snapshot' > "${argument#--file=}" ;; esac
done
""",
        )

    def executable(self, name, contents):
        path = self.bin / name
        path.write_text(contents)
        path.chmod(0o755)

    def run_script(self, name, *args, environment=None):
        return subprocess.run(
            ["sh", str(SCRIPTS / name), *args],
            env=environment or self.environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
        )

    def test_dump_precedes_media_snapshot_and_retention_follows_verification(self):
        result = self.run_script("backup-now.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.log.read_text().splitlines()
        dump = calls.index("pg_dump")
        backup = next(i for i, line in enumerate(calls) if line.startswith("backup "))
        check = next(i for i, line in enumerate(calls) if line.startswith("check "))
        prune = next(i for i, line in enumerate(calls) if line.startswith("forget "))
        self.assertLess(dump, backup)
        self.assertLess(backup, check)
        self.assertLess(check, prune)
        self.assertIn(" /media ", calls[backup])
        self.assertTrue((self.backups / "status/last_success").exists())
        self.assertFalse((self.directory / "work/snapshot").exists())
        self.assertNotIn("test-only-secret", result.stdout + result.stderr)

    def test_failed_database_dump_retains_previous_snapshot_and_timestamp(self):
        self.assertEqual(self.run_script("backup-now.sh").returncode, 0)
        timestamp = self.backups / "status/last_success"
        previous = timestamp.read_bytes()
        (self.repo / "retention-applied").unlink()
        result = self.run_script(
            "backup-now.sh",
            environment={**self.environment, "MOCK_DATABASE_FAILURE": "1"},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(timestamp.read_bytes(), previous)
        self.assertEqual((self.repo / "snapshot").read_text(), "retained-snapshot")
        self.assertFalse((self.repo / "retention-applied").exists())
        self.assertTrue((self.backups / "status/last_failure").exists())
        self.assertFalse((self.directory / "work/snapshot").exists())

    def test_failed_integrity_check_never_prunes_previous_backups(self):
        result = self.run_script(
            "backup-now.sh",
            environment={**self.environment, "MOCK_CHECK_FAILURE": "1"},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.repo / "retention-applied").exists())
        self.assertFalse((self.backups / "status/last_success").exists())

    def test_wrong_password_never_reinitialises_existing_repository(self):
        config = self.repo / "config"
        config.write_bytes(b"existing-encrypted-repository")
        result = self.run_script(
            "backup-now.sh",
            environment={**self.environment, "MOCK_BAD_PASSWORD": "1"},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(config.read_bytes(), b"existing-encrypted-repository")
        self.assertNotIn("init", self.log.read_text().splitlines())

    def test_concurrent_backup_exits_before_touching_database(self):
        with (self.backups / ".backup.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.run_script("backup-now.sh")
        self.assertEqual(result.returncode, 75)
        self.assertFalse(self.log.exists())

    def test_restore_requires_explicit_snapshot_and_staging_directory(self):
        latest = self.run_script("restore-backup.sh", "latest")
        self.assertEqual(latest.returncode, 2)
        wrong_target = self.run_script("restore-backup.sh", "abcdef12", "/media")
        self.assertEqual(wrong_target.returncode, 2)
        self.assertFalse(self.log.exists())

    def test_health_detects_stale_copy_and_stalled_scheduler(self):
        status = self.backups / "status"
        status.mkdir()
        now = int(time.time())
        (status / "heartbeat").write_text(str(now))
        (status / "last_success").write_text(str(now - 3600))
        self.assertEqual(self.run_script("backup-healthcheck.sh").returncode, 0)
        (status / "last_success").write_text(str(now - 93601))
        self.assertNotEqual(self.run_script("backup-healthcheck.sh").returncode, 0)
        (status / "last_success").write_text(str(now))
        (status / "heartbeat").write_text(str(now - 181))
        self.assertNotEqual(self.run_script("backup-healthcheck.sh").returncode, 0)

    def test_latest_failure_is_unhealthy_until_successful_retry(self):
        self.assertEqual(self.run_script("backup-now.sh").returncode, 0)
        status = self.backups / "status"
        (status / "heartbeat").write_text(str(int(time.time())))
        self.assertEqual(self.run_script("backup-healthcheck.sh").returncode, 0)
        failed = self.run_script(
            "backup-now.sh",
            environment={**self.environment, "MOCK_BAD_PASSWORD": "1"},
        )
        self.assertNotEqual(failed.returncode, 0)
        self.assertNotEqual(self.run_script("backup-healthcheck.sh").returncode, 0)
        self.assertEqual(self.run_script("backup-now.sh").returncode, 0)
        self.assertFalse((status / "last_failure").exists())
        self.assertEqual(self.run_script("backup-healthcheck.sh").returncode, 0)

    def test_failed_first_backup_is_not_hidden_by_initial_grace_period(self):
        (self.repo / "config").write_text("existing encrypted repository")
        failed = self.run_script(
            "backup-now.sh",
            environment={**self.environment, "MOCK_BAD_PASSWORD": "1"},
        )
        self.assertNotEqual(failed.returncode, 0)
        status = self.backups / "status"
        for name in ("heartbeat", "scheduler_started"):
            (status / name).write_text(str(int(time.time())))
        self.assertNotEqual(self.run_script("backup-healthcheck.sh").returncode, 0)

    def test_scheduler_rejects_invalid_time_before_running_job(self):
        result = self.run_script(
            "backup-entrypoint.sh",
            environment={**self.environment, "BACKUP_TIME": "29:00"},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.log.exists())

    def start_scheduler(self, initial_time):
        clock = self.directory / "clock"
        clock.write_text(initial_time)
        self.executable(
            "date",
            """#!/bin/sh
if [ "$1" = '+%H:%M' ]; then cat "$MOCK_CLOCK"; else exec /bin/date "$@"; fi
""",
        )
        self.executable("sleep", "#!/bin/sh\nexec /bin/sleep 0.02\n")
        process = subprocess.Popen(
            ["sh", str(SCRIPTS / "backup-entrypoint.sh"), "scheduler"],
            env={**self.environment, "BACKUP_TIME": "03:00", "MOCK_CLOCK": str(clock)},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        def stop():
            process.terminate()
            try:
                process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=3)

        self.addCleanup(stop)
        return clock, process

    def wait_until(self, condition):
        deadline = time.monotonic() + 3
        while not condition():
            if time.monotonic() > deadline:
                self.fail("Scheduler did not reach the expected state within three seconds")
            time.sleep(0.01)

    def test_background_scheduler_creates_first_copy_without_browser(self):
        _, process = self.start_scheduler("00:00")
        self.wait_until(lambda: (self.backups / "status/last_success").exists())
        self.assertIsNone(process.poll())
        self.assertEqual(self.log.read_text().splitlines().count("pg_dump"), 1)
        self.assertEqual(self.run_script("backup-healthcheck.sh").returncode, 0)

    def test_daily_scheduler_waits_for_clock_and_does_not_repeat_same_day(self):
        status = self.backups / "status"
        status.mkdir()
        previous_success = str(int(time.time()) - 86400)
        (status / "last_success").write_text(previous_success)
        clock, process = self.start_scheduler("02:59")
        self.wait_until(lambda: (status / "heartbeat").exists())
        time.sleep(0.08)
        self.assertFalse(self.log.exists())
        clock.write_text("03:00")
        self.wait_until(lambda: self.log.exists() and "pg_dump" in self.log.read_text())
        self.wait_until(
            lambda: (status / "last_success").read_text().strip() != previous_success
        )
        time.sleep(0.12)
        self.assertIsNone(process.poll())
        self.assertEqual(self.log.read_text().splitlines().count("pg_dump"), 1)
