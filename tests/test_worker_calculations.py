"""PostgreSQL integration tests: real parsers/storage/calculations; HTTP is mocked.

No browser request is used to schedule, claim, or execute any job. Workbook data
in this module is synthetic and does not represent official statistics.
"""
import hashlib
import io
import tempfile
from datetime import date, datetime, time, timedelta, timezone as dt_timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection, transaction
from django.test import TestCase, override_settings
from django.utils import timezone
from openpyxl import Workbook

from monitoring.models import Formula, Indicator, LoadRun, Methodology, Observation, ObservationVersion, Publication, Source
from monitoring.services.adapters import AdapterError, Download
from monitoring.services.calculations import calculate_expression, recompute_formulas
from monitoring.services.observations import apply_correction, upsert_observation
from monitoring.services.selectors import filtered_observations
from monitoring.services.workers import claim_run, enqueue_source, execute_run, next_due, schedule_due, worker_tick


class WorkerIntegrationTests(TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.media = override_settings(MEDIA_ROOT=Path(self.directory.name) / "media", IMPORT_WATCH_DIR=Path(self.directory.name) / "incoming")
        self.media.enable(); self.addCleanup(self.media.disable)
        self.indicator = Indicator.objects.create(code="annual_test", name="Синтетический годовой показатель", section="statistics", unit="чел.", period_kind="flow")
        self.source = Source.objects.create(name="Синтетический источник", adapter="xlsx", url="https://example.org/statistics.xlsx", unit="чел.", territory="Москва", frequency="monthly", schedule_day=5, schedule_time=time(9), enabled=True, mapping={"indicator_code": self.indicator.code})

    def workbook(self, value=10, **changes):
        record = {"indicator_code": self.indicator.code, "value": value, "unit": "чел.", "territory": "Москва", "period_start": "2025-01-01", "period_end": "2025-12-31", "period_kind": "flow", "period_label": "2025 год", **changes}
        workbook = Workbook(); sheet = workbook.active; sheet.title = "Данные"
        sheet.append(list(record)); sheet.append(list(record.values()))
        stream = io.BytesIO(); workbook.save(stream)
        return stream.getvalue()

    def download(self, content):
        return Download(content, self.source.url, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", hashlib.sha256(content).hexdigest())

    def execute(self, content=None, error=None):
        run = enqueue_source(self.source)
        claimed = claim_run()
        self.assertEqual(claimed.pk, run.pk)
        publications = [{"url": self.source.url, "title": "Годовая публикация", "published_at": "2026-02-15"}]
        with patch("monitoring.services.workers.discover", return_value=publications), patch("monitoring.services.workers.fetch_url", side_effect=error, return_value=self.download(content or self.workbook())):
            execute_run(claimed)
        claimed.refresh_from_db(); self.source.refresh_from_db()
        return claimed

    def test_database_is_postgresql(self):
        self.assertEqual(connection.vendor, "postgresql")

    def test_unique_active_queue_is_enforced_in_database(self):
        first = enqueue_source(self.source)
        second = enqueue_source(self.source)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(LoadRun.objects.count(), 1)
        with self.assertRaises(IntegrityError), transaction.atomic():
            LoadRun.objects.create(source=self.source, status="retry")

    def test_claim_and_execute_imports_real_synthetic_xlsx_without_browser(self):
        run = self.execute(self.workbook(17))
        self.assertEqual(run.status, "success", run.log)
        observation = Observation.objects.get(indicator=self.indicator)
        self.assertEqual(observation.current_version.value, Decimal(17))
        self.assertEqual(observation.current_version.status, "verified")
        self.assertEqual(observation.current_version.publication_date, date(2026, 2, 15))
        self.assertEqual(observation.period_start, date(2025, 1, 1))
        self.assertEqual(observation.period_end, date(2025, 12, 31))
        self.assertEqual(self.source.last_publication_date, date(2026, 2, 15))
        self.assertIsNotNone(self.source.last_checked)
        self.assertIsNotNone(self.source.last_success)
        self.assertIsNone(run.lease_until)
        self.assertTrue(Path(observation.current_version.publication.file.path).is_file())

    def test_same_checksum_reimport_is_idempotent(self):
        content = self.workbook(11)
        first = self.execute(content)
        second = self.execute(content)
        self.assertEqual((first.status, second.status), ("success", "success"))
        self.assertEqual(Publication.objects.count(), 1)
        self.assertEqual(Observation.objects.count(), 1)
        self.assertEqual(ObservationVersion.objects.count(), 1)
        self.assertEqual(second.result["versions_created"], 0)
        self.assertEqual(second.result["publications_created"], 0)
        self.assertIn("Без изменений", second.log)

    def test_changed_content_at_same_url_creates_version_without_duplicate_observation(self):
        self.execute(self.workbook(11))
        run = self.execute(self.workbook(13))
        self.assertEqual(run.status, "success", run.log)
        self.assertEqual(Observation.objects.count(), 1)
        self.assertEqual(Publication.objects.count(), 2)
        observation = Observation.objects.get()
        self.assertEqual(observation.current_version.number, 2)
        self.assertEqual(observation.current_version.value, Decimal(13))
        self.assertEqual(list(observation.versions.order_by("number").values_list("value", flat=True)), [Decimal(11), Decimal(13)])
        self.assertEqual(len(set(Publication.objects.values_list("url", flat=True))), 1)
        self.assertEqual(len(set(Publication.objects.values_list("checksum", flat=True))), 2)

    def test_manual_correction_survives_source_revision_and_conflict_is_visible(self):
        self.execute(self.workbook(11))
        observation = Observation.objects.get()
        user = get_user_model().objects.create_superuser(username="worker-editor", password="test-only-password")
        correction = apply_correction(observation, "14", "value", "Проверено владельцем", user)
        run = self.execute(self.workbook(13))
        self.assertEqual(run.status, "success", run.log)
        correction.refresh_from_db()
        self.assertTrue(correction.active)
        self.assertTrue(correction.conflict)
        row = filtered_observations({})[0]
        self.assertEqual(Decimal(row["value"]), Decimal(14))
        self.assertEqual(Decimal(row["original_value"]), Decimal(13))
        self.assertEqual(row["status"], "conflict")

    def test_network_failure_keeps_last_correct_data_and_records_exact_error(self):
        self.execute(self.workbook(11))
        original_id = Observation.objects.get().current_version_id
        last_success = self.source.last_success
        run = self.execute(error=AdapterError("HTTP 503 от источника example.org: Service Unavailable"))
        self.assertEqual(run.status, "retry")
        self.assertEqual(Observation.objects.get().current_version_id, original_id)
        self.assertEqual(self.source.last_success, last_success)
        self.assertIn("HTTP 503", self.source.last_error)
        self.assertEqual(self.source.status, "error")
        self.assertGreaterEqual(self.source.last_checked, last_success)

    def test_unsupported_structure_keeps_last_correct_data(self):
        self.execute(self.workbook(11))
        original_id = Observation.objects.get().current_version_id
        run = self.execute(b"not an xlsx file")
        self.assertEqual(run.status, "retry")
        self.assertIn("XLSX", run.log)
        self.assertEqual(Observation.objects.get().current_version_id, original_id)
        self.assertEqual(ObservationVersion.objects.count(), 1)
        self.assertEqual(Publication.objects.count(), 2)  # the rejected original is retained for diagnosis
        self.assertEqual(Publication.objects.filter(metadata__applied=True).count(), 1)

    def test_retry_backoff_and_terminal_failure(self):
        run = self.execute(error=AdapterError("HTTP 403 от источника"))
        self.assertEqual(run.attempts, 1)
        self.assertGreater(run.next_attempt_at, timezone.now())
        self.assertIsNone(claim_run())
        for attempt in (2, 3):
            LoadRun.objects.filter(pk=run.pk).update(next_attempt_at=timezone.now() - timedelta(seconds=1))
            run = self.execute(error=AdapterError("HTTP 403 от источника"))
            self.assertEqual(run.attempts, attempt)
            self.assertEqual(run.status, "retry" if attempt < 3 else "error")
        self.assertIsNotNone(run.finished_at)
        self.assertIsNone(run.lease_until)

    def test_advisory_lock_prevents_parallel_source_execution(self):
        other = connection.copy(alias="worker_lock_test")
        try:
            with other.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_lock(%s,%s)", [170703, self.source.pk])
            run = claim_run() if LoadRun.objects.exists() else None
            if run is None:
                enqueue_source(self.source); run = claim_run()
            with patch("monitoring.services.workers.fetch_url") as fetch:
                execute_run(run)
            run.refresh_from_db()
            self.assertEqual(run.status, "retry")
            self.assertIn("другим процессом", run.log)
            fetch.assert_not_called()
            self.assertFalse(Observation.objects.exists())
        finally:
            with other.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s,%s)", [170703, self.source.pk])
            other.close()

    def test_stale_lease_returns_to_queue_and_is_claimed_once(self):
        run = LoadRun.objects.create(source=self.source, status="running", attempts=1, lease_until=timezone.now() - timedelta(seconds=1))
        reclaimed = claim_run()
        self.assertEqual(reclaimed.pk, run.pk)
        self.assertEqual(reclaimed.attempts, 2)
        self.assertEqual(reclaimed.status, "running")
        self.assertIn("Истёк срок", reclaimed.log)
        self.assertIsNone(claim_run())
        self.assertGreater(reclaimed.lease_until, timezone.now())

    def test_stale_lease_exhausted_attempts_is_not_reclaimed(self):
        run = LoadRun.objects.create(source=self.source, status="running", attempts=3, lease_until=timezone.now() - timedelta(seconds=1))
        self.assertIsNone(claim_run())
        run.refresh_from_db()
        self.assertEqual(run.status, "error")
        self.assertIn("Истёк срок", run.log)

    def test_monthly_schedule_uses_moscow_timezone(self):
        before = datetime(2026, 11, 5, 5, 59, tzinfo=dt_timezone.utc)
        due = next_due(self.source, before)
        self.assertEqual(due.astimezone(dt_timezone.utc), datetime(2026, 11, 5, 6, 0, tzinfo=dt_timezone.utc))
        after = datetime(2026, 11, 5, 6, 0, tzinfo=dt_timezone.utc)
        due = next_due(self.source, after)
        self.assertEqual(due.astimezone(dt_timezone.utc), datetime(2026, 12, 5, 6, 0, tzinfo=dt_timezone.utc))

    def test_monthly_schedule_rolls_over_year(self):
        due = next_due(self.source, datetime(2026, 12, 31, 12, 0, tzinfo=dt_timezone.utc))
        self.assertEqual(due.astimezone(dt_timezone.utc), datetime(2027, 1, 5, 6, 0, tzinfo=dt_timezone.utc))
        self.source.frequency = "manual"
        self.assertIsNone(next_due(self.source))

    def test_schedule_and_worker_tick_operate_with_no_browser(self):
        self.source.next_due = timezone.now() - timedelta(seconds=1)
        self.source.save(update_fields=["next_due"])
        with patch("monitoring.services.workers.discover", return_value=[{"url": self.source.url, "title": "Годовой файл", "published_at": "2026-02-15"}]), patch("monitoring.services.workers.fetch_url", return_value=self.download(self.workbook(15))):
            tick = worker_tick()
        self.assertEqual(tick["scheduled"], 1)
        self.assertIsNotNone(tick["run"])
        self.assertEqual(LoadRun.objects.get().trigger, "schedule")
        self.assertEqual(LoadRun.objects.get().status, "success")
        self.assertEqual(Observation.objects.get().current_version.value, Decimal(15))

    def test_monthly_checks_do_not_create_monthly_observations_for_annual_file(self):
        content = self.workbook(21)
        self.source.next_due = timezone.now() - timedelta(seconds=1)
        self.source.save(update_fields=["next_due"])
        self.assertEqual(schedule_due(), 1)
        self.execute(content)
        self.source.next_due = timezone.now() - timedelta(seconds=1)
        self.source.save(update_fields=["next_due"])
        self.assertEqual(schedule_due(), 1)
        self.execute(content)
        self.assertEqual(Observation.objects.count(), 1)
        self.assertEqual(ObservationVersion.objects.count(), 1)
        observation = Observation.objects.get()
        self.assertEqual((observation.period_start, observation.period_end), (date(2025, 1, 1), date(2025, 12, 31)))
        self.assertEqual(LoadRun.objects.filter(trigger="schedule", status="success").count(), 2)


class CalculationIntegrationTests(TestCase):
    def setUp(self):
        self.a = Indicator.objects.create(code="component_a", name="А", section="statistics", unit="ед.", period_kind="flow")
        self.b = Indicator.objects.create(code="component_b", name="Б", section="statistics", unit="ед.", period_kind="flow")
        self.output = Indicator.objects.create(code="derived", name="Производный показатель", section="irfk", unit="ед.", period_kind="flow")
        self.methodology = Methodology.objects.create(code="irfk", version=1, name="Синтетическая методика", approved=True)

    def record(self, indicator, value, **extra):
        row = {"indicator_code": indicator.code, "value": str(value) if value is not None else None, "data_state": "value" if value is not None else "missing", "unit": indicator.unit, "territory": "Москва", "period_start": "2025-01-01", "period_end": "2025-12-31", "period_label": "2025 год", "period_kind": "flow", "status": "verified", **extra}
        return upsert_observation(row)[0]

    def formula(self, operation="sum", **extra):
        return Formula.objects.create(indicator=self.output, methodology=self.methodology, expression={"operation": operation, "components": [self.a.code, self.b.code], **extra})

    def test_sum_and_repeat_recomputation_are_exact_and_idempotent(self):
        self.record(self.a, "0.1"); self.record(self.b, "0.2"); self.formula()
        first = recompute_formulas(); second = recompute_formulas()
        observation = Observation.objects.get(indicator=self.output)
        self.assertEqual(observation.current_version.value, Decimal("0.3"))
        self.assertEqual(observation.current_version.methodology_id, self.methodology.pk)
        self.assertEqual(first["versions_created"], 1)
        self.assertEqual(second["versions_created"], 0)
        self.assertEqual(observation.versions.count(), 1)
        self.assertEqual(len(observation.current_version.provenance["component_versions"]), 2)

    def test_ratio_is_calculated_in_decimal(self):
        self.record(self.a, 1); self.record(self.b, 4); self.formula("ratio", multiplier="100")
        result = recompute_formulas()
        self.assertEqual(result["versions_created"], 1)
        self.assertEqual(Observation.objects.get(indicator=self.output).current_version.value, Decimal(25))

    def test_zero_numerator_is_a_valid_zero_result(self):
        self.record(self.a, 0); self.record(self.b, 4); self.formula("ratio")
        self.assertEqual(recompute_formulas()["versions_created"], 1)
        value = Observation.objects.get(indicator=self.output).current_version
        self.assertEqual((value.value, value.data_state), (Decimal(0), "value"))

    def test_zero_denominator_does_not_publish_infinite_index(self):
        self.record(self.a, 1); self.record(self.b, 0); self.formula("ratio")
        result = recompute_formulas()
        self.assertFalse(Observation.objects.filter(indicator=self.output, current_version__data_state="value").exists())
        self.assertIn("Нулевой знаменатель", " ".join(result["limitations"]))

    def test_missing_component_blocks_full_index(self):
        self.record(self.a, 1); self.formula()
        result = recompute_formulas()
        self.assertIn("неполный", " ".join(result["limitations"]).lower())
        self.assertFalse(Observation.objects.filter(indicator=self.output, current_version__data_state="value").exists())
        current = Observation.objects.get(indicator=self.output).current_version
        self.assertEqual((current.value, current.data_state, current.status), (None, "missing", "pending"))

    def test_missing_data_is_not_reinterpreted_as_zero(self):
        self.record(self.a, 1); self.record(self.b, None); self.formula()
        result = recompute_formulas()
        self.assertFalse(Observation.objects.filter(indicator=self.output, current_version__data_state="value").exists())
        self.assertTrue(result["limitations"])

    def test_unverified_component_not_used_for_full_index(self):
        self.record(self.a, 1); self.record(self.b, 2, status="pending"); self.formula()
        result = recompute_formulas()
        self.assertTrue(result["limitations"])
        self.assertFalse(Observation.objects.filter(indicator=self.output, current_version__data_state="value").exists())

    def test_draft_methodology_is_never_applied(self):
        self.methodology.approved = False; self.methodology.save()
        self.record(self.a, 1); self.record(self.b, 2); self.formula()
        self.assertEqual(recompute_formulas()["versions_created"], 0)
        self.assertFalse(Observation.objects.filter(indicator=self.output).exists())

    def test_latest_approved_methodology_used_instead_of_newer_draft(self):
        self.record(self.a, 1); self.record(self.b, 2); self.formula()
        draft = Methodology.objects.create(code="irfk", version=2, name="Проект", approved=False)
        Formula.objects.create(indicator=self.output, methodology=draft, expression={"operation": "sum", "components": [self.a.code, self.b.code], "multiplier": "10"})
        recompute_formulas()
        v = Observation.objects.get(indicator=self.output).current_version
        self.assertEqual(v.value, Decimal(3))
        self.assertEqual(v.methodology_id, self.methodology.pk)

    def test_dependent_value_creates_new_version_after_component_revision(self):
        self.record(self.a, 1); self.record(self.b, 2); self.formula(); recompute_formulas()
        self.record(self.a, 4); recompute_formulas()
        observation = Observation.objects.get(indicator=self.output)
        self.assertEqual(observation.current_version.value, Decimal(6))
        self.assertEqual(list(observation.versions.order_by("number").values_list("value", flat=True)), [Decimal(3), Decimal(6)])

    def test_different_periods_or_territories_are_not_combined(self):
        self.record(self.a, 1)
        self.record(self.b, 2, territory="Санкт-Петербург")
        self.record(self.b, 3, period_start="2026-01-01", period_end="2026-12-31", period_label="2026 год")
        self.formula()
        result = recompute_formulas()
        self.assertTrue(result["limitations"])
        self.assertFalse(Observation.objects.filter(indicator=self.output, current_version__data_state="value").exists())

    def test_weighted_ratio_requires_exact_counts_and_uses_total_denominator(self):
        self.record(self.a, "50", numerator="1", denominator="2")
        self.record(self.b, "90", numerator="9", denominator="10")
        self.formula("weighted_ratio", multiplier="100")
        recompute_formulas()
        self.assertEqual(Observation.objects.get(indicator=self.output).current_version.value, Decimal("83.33333333"))

    def test_previously_computed_index_is_not_verified_when_component_becomes_missing(self):
        self.record(self.a, 1); self.record(self.b, 2); self.formula(); recompute_formulas()
        self.record(self.b, None)
        result = recompute_formulas()
        observation = Observation.objects.get(indicator=self.output)
        self.assertTrue(result["limitations"])
        self.assertFalse(observation.current_version.status == "verified" and observation.current_version.data_state == "value", "Ранее рассчитанный индекс остался проверенным после исчезновения обязательного компонента")
        self.assertEqual(observation.versions.order_by("number").first().value, Decimal(3))

    def test_published_percentages_never_reverse_engineer_exact_counts(self):
        self.record(self.a, "33.3"); self.record(self.b, "66.7"); self.formula("weighted_ratio", multiplier="100")
        result = recompute_formulas()
        self.assertFalse(Observation.objects.filter(indicator=self.output, current_version__data_state="value").exists())
        self.assertIn("точных числителей", " ".join(result["limitations"]))

    def test_dependency_is_computed_before_formula_that_consumes_it(self):
        self.record(self.a, 1); self.record(self.b, 2)
        intermediate = Indicator.objects.create(code="intermediate", name="Промежуточный", section="irfk", unit="ед.", period_kind="flow")
        # Output has a lower database id than its dependency: insertion order is insufficient.
        Formula.objects.create(indicator=self.output, methodology=self.methodology, expression={"operation": "sum", "components": [intermediate.code, self.b.code]})
        Formula.objects.create(indicator=intermediate, methodology=self.methodology, expression={"operation": "sum", "components": [self.a.code, self.b.code]})
        result = recompute_formulas()
        self.assertEqual(result["versions_created"], 2)
        self.assertEqual(Observation.objects.get(indicator=intermediate).current_version.value, Decimal(3))
        self.assertEqual(Observation.objects.get(indicator=self.output).current_version.value, Decimal(5))

    def test_cycle_is_blocked_without_publishing_partial_index(self):
        intermediate = Indicator.objects.create(code="intermediate", name="Промежуточный", section="irfk", unit="ед.", period_kind="flow")
        Formula.objects.create(indicator=self.output, methodology=self.methodology, expression={"operation": "sum", "components": [intermediate.code]})
        Formula.objects.create(indicator=intermediate, methodology=self.methodology, expression={"operation": "sum", "components": [self.output.code]})
        result = recompute_formulas()
        self.assertEqual(result["versions_created"], 0)
        self.assertIn("Циклическая зависимость", " ".join(result["limitations"]))
        self.assertFalse(Observation.objects.filter(indicator__in=[self.output, intermediate]).exists())

    def test_unrecognized_formula_does_not_evaluate_code(self):
        self.record(self.a, 1); self.record(self.b, 2)
        self.formula("__import__('os').system('echo unsafe')")
        result = recompute_formulas()
        self.assertFalse(Observation.objects.filter(indicator=self.output, current_version__data_state="value").exists())
        self.assertIn("Неизвестная операция", " ".join(result["limitations"]))
