"""Meaningful first-version acceptance checks with synthetic values only."""
from decimal import Decimal
import io
from pathlib import Path
from tempfile import TemporaryDirectory

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from openpyxl import Workbook

from monitoring.models import Indicator, Observation, ObservationVersion, ManualCorrection
from monitoring.services.importer import create_preview, confirm_batch
from monitoring.services.observations import (
    apply_correction, resolve_correction, upsert_observation, validate_record, review_observation,
)
from monitoring.services.selectors import display_number, filtered_observations, weighted_accuracy


class CoreDataTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.viewer = get_user_model().objects.create_user("core-viewer")
        cls.editor = get_user_model().objects.create_user("core-editor")
        cls.admin = get_user_model().objects.create_superuser("core-admin", password="synthetic-test-only")
        cls.viewer.user_permissions.add(*Permission.objects.filter(content_type__app_label="monitoring", codename__startswith="view_"))
        cls.editor.user_permissions.add(*Permission.objects.filter(content_type__app_label="monitoring", codename__in=["add_observation", "add_indicator", "change_indicator", "add_source", "change_source"]))

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.media = override_settings(MEDIA_ROOT=Path(self.tmp.name))
        self.media.enable()
        self.addCleanup(self.media.disable)
        self.indicator = Indicator.objects.create(code="SYNTHETIC-CORE", name="Синтетический показатель", section="statistics", unit="ед.", period_kind="flow")

    def record(self, **changes):
        result = {"indicator_code": self.indicator.code, "value": "10", "data_state": "value", "unit": "ед.", "territory": "Тестовая территория", "period_label": "I квартал 2025", "period_start": "2025-01-01", "period_end": "2025-03-31", "period_kind": "flow", "provenance": {"url": "https://example.org/statistics.csv", "cell": "D3"}}
        result.update(changes)
        return result

    @staticmethod
    def workbook(value):
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "2 Блок - Статистика"
        sheet.append(["Синтетический реестр"])
        sheet.append(["Тема", "Показатель", "Единица", "IV кв. 2024 / 2024", "Источник", "IV кв. 2025 / 2025", "Источник", "I кв. 2026", "Источник"])
        sheet.append(["СБЕРЕЖЕНИЯ", "Синтетическая серия импорта", "ед.", value, "https://example.org/table.xlsx", 20, "https://example.org/table.xlsx", 30, "https://example.org/table.xlsx"])
        result = io.BytesIO()
        workbook.save(result)
        return result.getvalue()

    def import_workbook(self, content):
        upload = SimpleUploadedFile("synthetic-registry.xlsx", content, content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        batch = create_preview(upload, self.editor)
        self.assertEqual(batch.preview["errors"], [])
        return batch, confirm_batch(batch, self.editor)

    def test_reupload_same_registry_does_not_duplicate_observations_or_versions(self):
        content = self.workbook(10)
        first_batch, first = self.import_workbook(content)
        self.assertEqual(first["versions_created"], 3)
        count = ObservationVersion.objects.count()
        second_batch, second = self.import_workbook(content)
        # Storage may rename the second file; original provenance must stay stable.
        self.assertNotEqual(first_batch.file.name, second_batch.file.name)
        self.assertEqual(second["versions_created"], 0)
        self.assertEqual(second["unchanged"], 3)
        self.assertEqual(Observation.objects.count(), 3)
        self.assertEqual(ObservationVersion.objects.count(), count)
        self.assertEqual(confirm_batch(second_batch, self.editor), second)

    def test_same_source_after_manual_review_does_not_erase_review_or_create_version(self):
        obs,_=upsert_observation(self.record(status="pending"),method="auto")
        reviewed=review_observation(obs,"Проверено по первоисточнику",self.editor)
        _,changed=upsert_observation(self.record(status="pending"),method="auto")
        obs.refresh_from_db()
        self.assertFalse(changed)
        self.assertEqual(obs.current_version_id,reviewed.pk)
        self.assertEqual(obs.current_version.status,"verified")
        self.assertEqual(obs.versions.count(),2)

    def test_conflicting_duplicate_dimensions_block_preview(self):
        from monitoring.services.importer import validate_preview
        payload=validate_preview({"observations":[self.record(value="10"),self.record(value="11")]})
        self.assertEqual(len(payload["errors"]),1)
        self.assertIn("Повторяющиеся измерения",payload["errors"][0]["message"])

    def test_revised_publication_creates_version_and_preserves_original(self):
        self.import_workbook(self.workbook(10))
        target = Observation.objects.get(period_label="IV кв. 2024 / 2024")
        original_id = target.current_version_id
        self.import_workbook(self.workbook(12))
        target.refresh_from_db()
        self.assertEqual(Observation.objects.count(), 3)
        self.assertEqual(target.versions.count(), 2)
        self.assertEqual(target.current_version.value, Decimal("12"))
        self.assertEqual(ObservationVersion.objects.get(pk=original_id).value, Decimal("10"))

    def test_automatic_revision_preserves_manual_choice_until_explicit_resolution(self):
        obs, _ = upsert_observation(self.record(), method="auto")
        correction = apply_correction(obs, "13", "value", "Подтверждено первичным документом", self.editor)
        upsert_observation(self.record(value="11"), method="auto")
        correction.refresh_from_db()
        self.assertTrue(correction.active)
        self.assertTrue(correction.conflict)
        row = filtered_observations({"indicator": self.indicator.code})[0]
        self.assertEqual(Decimal(row["value"]), Decimal("13"))
        self.assertEqual(Decimal(row["original_value"]), Decimal("11"))
        self.assertEqual(row["status"], "conflict")
        resolve_correction(correction, "correction", "Оставить подтвержденную поправку", self.editor)
        correction.refresh_from_db()
        self.assertTrue(correction.active)
        self.assertFalse(correction.conflict)
        upsert_observation(self.record(value="14"), method="auto")
        correction.refresh_from_db()
        self.assertTrue(correction.conflict)
        resolve_correction(correction, "source", "Принять исправленный первичный источник", self.editor)
        row = filtered_observations({"indicator": self.indicator.code})[0]
        self.assertFalse(row["has_correction"])
        self.assertEqual(Decimal(row["value"]), Decimal("14"))
        self.assertEqual(ManualCorrection.objects.count(), 1)
        self.assertEqual(obs.versions.count(), 3)

    def test_zero_missing_and_not_applicable_are_distinct(self):
        for period, start, end, value, state in [
            ("I квартал", "2025-01-01", "2025-03-31", "0", "value"),
            ("II квартал", "2025-04-01", "2025-06-30", None, "missing"),
            ("III квартал", "2025-07-01", "2025-09-30", None, "na"),
        ]:
            upsert_observation(self.record(period_label=period, period_start=start, period_end=end, value=value, data_state=state), method="auto")
        rows = {row["data_state"]: row for row in filtered_observations({"indicator": self.indicator.code})}
        self.assertEqual(rows["value"]["value_display"], "0")
        self.assertEqual(rows["missing"]["value_display"], "Нет данных")
        self.assertEqual(rows["na"]["value_display"], "Неприменимо")
        with self.assertRaises(ValidationError):
            upsert_observation(self.record(value="0", data_state="missing"), method="auto")

    def test_roles_restrict_writes_and_administration(self):
        self.client.force_login(self.viewer)
        self.assertEqual(self.client.get(reverse("monitoring:dashboard")).status_code, 200)
        self.assertEqual(self.client.get(reverse("monitoring:observation_create")).status_code, 403)
        with self.assertRaises(PermissionDenied):
            upsert_observation(self.record(), user=self.viewer, method="manual")
        self.client.force_login(self.editor)
        self.assertEqual(self.client.get(reverse("monitoring:observation_create")).status_code, 200)
        self.assertEqual(self.client.get(reverse("monitoring:source_create")).status_code, 200)
        self.assertEqual(self.client.get(reverse("monitoring:topic_create")).status_code, 403)
        self.assertNotEqual(self.client.get("/admin/").status_code, 200)
        obs, _ = upsert_observation(self.record(), user=self.editor, method="manual")
        self.assertIsNotNone(obs.pk)
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get("/admin/").status_code, 200)
        self.assertEqual(self.client.get(reverse("monitoring:topic_create")).status_code, 200)

    def test_explicit_unverified_record_is_not_silently_verified(self):
        result = validate_record(self.record(verification="unverified"), self.indicator)
        self.assertEqual(result["status"], "pending")

    def test_publication_date_is_versioned_even_when_value_is_same(self):
        obs, _ = upsert_observation(self.record(publication_date="2025-04-10"), method="auto")
        obs, changed = upsert_observation(self.record(publication_date="2025-04-11"), method="auto")
        self.assertTrue(changed)
        self.assertEqual(obs.versions.count(), 2)
        self.assertEqual(str(obs.current_version.publication_date), "2025-04-11")


class AccuracyAndDisplayTests(TestCase):
    def row(self, code="Q1", numerator="2", denominator="10", **changes):
        result = {"section": "fintest", "data_state": "value", "status": "verified", "indicator_code": code, "question_version_id": 1, "territory": "Тестовая территория", "group": "Все", "answer": "Доля верных", "period_start": "2025-01-01", "period_end": "2025-03-31", "numerator": numerator, "denominator": denominator}
        result.update(changes)
        return result

    def test_exact_result_uses_sum_of_counts_rather_than_average_percentages(self):
        result = weighted_accuracy([self.row(), self.row(code="Q2", numerator="90", denominator="100")])
        self.assertEqual(Decimal(result["value"]), Decimal("92") / Decimal("110") * 100)
        self.assertEqual(Decimal(result["numerator"]), Decimal("92"))
        self.assertEqual(Decimal(result["denominator"]), Decimal("110"))

    def test_published_rounded_percentage_never_becomes_exact_count(self):
        result = weighted_accuracy([self.row(numerator=None), self.row(code="Q2")])
        self.assertIsNone(result["value"])
        self.assertIn("числ", result["limitation"])

    def test_overlapping_periods_of_same_question_cannot_be_added(self):
        result = weighted_accuracy([self.row(), self.row(period_end="2025-06-30")])
        self.assertIsNone(result["value"])
        self.assertIn("ересек", result["limitation"])

    def test_distinct_question_versions_do_not_make_overlaps_safe(self):
        result = weighted_accuracy([self.row(), self.row(question_version_id=2)])
        self.assertIsNone(result["value"])

    def test_unresolved_conflict_does_not_enter_published_aggregate(self):
        result = weighted_accuracy([self.row(status="conflict")])
        self.assertIsNone(result["value"])

    def test_incompatible_populations_are_not_pooled(self):
        for altered in [self.row(code="Q2", territory="Другая территория"), self.row(code="Q2", group="Возраст 18–25")]:
            with self.subTest(altered=altered):
                result = weighted_accuracy([self.row(), altered])
                self.assertIsNone(result["value"])

    def test_negative_fraction_keeps_its_sign(self):
        self.assertEqual(display_number(Decimal("-0.5")), "-0,5")
        self.assertEqual(display_number(Decimal("-1234.50")), "-1 234,5")
