"""Domain checks using synthetic, non-confidential workbook data."""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from openpyxl import Workbook, load_workbook

from monitoring.services.registry_parser import parse_registry


class RegistryParserTests(TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "synthetic.xlsx"
        workbook = Workbook()
        sociology = workbook.active
        sociology.title = "1 Блок - Социсследование"
        sociology.append(["Исследование, 01.04.2025 - 30.04.2025"])
        sociology.append(["Тема", "Вопрос", "Ответ", "Значение", "Единицы", "Страница"])
        sociology.append(["СБЕРЕЖЕНИЯ", "В какой форме Вы храните Ваши сбережения?", "Долевое или накопительное страхование жизни", 5, "% среди имеющих сбережения", 37])
        sociology.append(["СТРАХОВАНИЕ*", 'Вопросы отсутствуют в первоначальной версии таблицы - предлагается добавить показатель "В какой форме Вы храните Ваши сбережения?"', "Делают взносы в долевое или накопительное страхование жизни", 5, "% среди респондентов, имеющих сбережения", 37])
        sociology.append(["ЛИЧНЫЙ БЮДЖЕТ", "Порядок расходов", "Жилье", 1, "место в ранжированном списке, ранг", 34])
        sociology.append(["ВОВЛЕЧЕНИЕ ГРАЖДАН В ПРОЦЕССЫ", "Вопросы отсутствуют в первоначальной версии таблицы", "Нет вопроса", "-", "-", "-"])
        statistics = workbook.create_sheet("2 Блок - Статистика")
        statistics.append(["Статистика"])
        statistics.append(["Тема", "Показатель", "Единицы", "IV кв. 2024 / 2024", "Источник", "IV кв. 2025 / 2025", "Источник", "I кв. 2026", "Источник"])
        statistics.append(["ИНВЕСТИРОВАНИЕ", "Число счетов, млн ед.", "тыс. счетов", 12.5, '=HYPERLINK("https://example.org/source.xlsx","Источник")', 13.5, "Нет ссылки", "Данные отсутствуют", "Нет данных"])
        fintest = workbook.create_sheet("3 Блок - Финансовый тест")
        fintest.append(["Финансовый тест"])
        fintest.append(["Тема", "Вопрос", "август - декабрь 2024 года", None, "2025 год", None, "I полугодие 2026 года"])
        fintest.append([None, None, "Количество", "%", "Количество", "%", "Количество", "%"])
        fintest.append(["НАЛОГИ", "Синтетический вопрос", 0, 0, 100, 0.24, 10, 0])
        for coordinate in ("D4", "F4", "H4"):
            fintest[coordinate].number_format = "0%"
        workbook.save(self.path)
        self.preview = parse_registry(self.path)

    def test_repeat_parse_has_stable_keys_and_preserves_source(self):
        before = self.path.read_bytes()
        again = parse_registry(self.path)
        self.assertEqual(self.preview, again)
        self.assertEqual(before, self.path.read_bytes())
        keys = [row["key"] for row in self.preview["observations"]]
        self.assertEqual(len(keys), len(set(keys)))

    def test_repeated_insurance_rubric_links_one_value(self):
        observations = [o for o in self.preview["observations"] if o["section"] == "sociology"]
        self.assertEqual(len(observations), 2)
        insurance = next(o for o in observations if o["unit"] == "%")
        self.assertEqual(len(insurance["source_references"]), 2)
        definition = next(i for i in self.preview["indicators"] if i["key"] == insurance["indicator_key"])
        self.assertEqual(set(definition["topics"]), {"savings", "insurance"})
        self.assertTrue(any(o["unit"] == "ранг" for o in observations))

    def test_zero_denominator_is_distinct_from_true_zero(self):
        observations = [o for o in self.preview["observations"] if o["section"] == "fintest"]
        undefined = next(o for o in observations if o["denominator"] == "0")
        true_zero = next(o for o in observations if o["denominator"] == "10")
        self.assertEqual(undefined["data_state"], "na")
        self.assertIsNone(undefined["value"])
        self.assertEqual(undefined["raw_value"], 0)
        self.assertEqual(true_zero["data_state"], "value")
        self.assertEqual(true_zero["value"], "0")

    def test_rounded_share_does_not_invent_correct_count(self):
        observations = [o for o in self.preview["observations"] if o["section"] == "fintest"]
        share = next(o for o in observations if o["denominator"] == "100")
        self.assertEqual(share["value"], "24.00")
        self.assertEqual(share["raw_value"], 0.24)
        self.assertIsNone(share["numerator"])
        self.assertIsNone(share["correct_count"])
        self.assertEqual(share["territory"], "Не уточнено")

    def test_conflicting_unit_and_ambiguous_period_are_review_issues(self):
        codes = {issue["code"] for issue in self.preview["issues"]}
        self.assertIn("unit_conflict", codes)
        self.assertIn("ambiguous_period", codes)
        row = next(o for o in self.preview["observations"] if o["section"] == "statistics")
        self.assertIsNone(row["period_start"])
        self.assertEqual(row["source"]["url"], "https://example.org/source.xlsx")

    def test_new_wave_keeps_study_identity_and_question_versions(self):
        original_study = next(s for s in self.preview["studies"] if s["section"] == "sociology")
        workbook = load_workbook(self.path)
        workbook.worksheets[0]["A1"] = "Исследование, 01.04.2026 - 30.04.2026"
        workbook.save(self.path)
        revised = parse_registry(self.path)
        revised_study = next(s for s in revised["studies"] if s["section"] == "sociology")
        self.assertEqual(original_study["key"], revised_study["key"])
        self.assertEqual(original_study["name"], revised_study["name"])
        self.assertNotEqual(original_study["wave"], revised_study["wave"])
        original_questions = {q["key"] for q in self.preview["questions"] if q["section"] == "sociology"}
        revised_questions = {q["key"] for q in revised["questions"] if q["section"] == "sociology"}
        self.assertEqual(original_questions, revised_questions)

    def test_business_tax_reference_uses_explicit_answer_alias_without_copy(self):
        workbook = load_workbook(self.path)
        sheet = workbook.worksheets[0]
        sheet.append(["НАЛОГИ", "Налоговая осведомленность", "Имеют представление о налогах и обязательных платежах", 78, "% респондентов", 69])
        sheet.append(["БИЗНЕС И ПРЕДПРИНИМАТЕЛЬСТВО", "Налоговая осведомленность", "Связанный показатель по налоговой осведомленности: имеют представление о налогах и обязательных платежах", 78, "% респондентов", 69])
        workbook.save(self.path)
        revised = parse_registry(self.path)
        definition = next(i for i in revised["indicators"] if i["name"] == "Налоговая осведомленность")
        observations = [o for o in revised["observations"] if o["indicator_key"] == definition["key"]]
        self.assertEqual(len(observations), 1)
        self.assertEqual(set(definition["topics"]), {"tax", "business"})
        self.assertEqual(len(observations[0]["source_references"]), 2)
        self.assertIn("raw_answer", observations[0]["source_references"][1])

    def test_stock_quarter_label_does_not_invent_a_stock_date(self):
        workbook = load_workbook(self.path)
        sheet = workbook.worksheets[1]
        sheet.append(["СБЕРЕЖЕНИЯ", "Сумма вкладов. Остатки средств на счетах", "руб.", 10, "https://example.org/table.xlsx", 20, "https://example.org/table.xlsx", 30, "https://example.org/table.xlsx"])
        workbook.save(self.path)
        revised = parse_registry(self.path)
        definition = next(i for i in revised["indicators"] if i["name"].startswith("Сумма вкладов"))
        obs = next(o for o in revised["observations"] if o["indicator_key"] == definition["key"] and o["period_label"] == "I кв. 2026")
        self.assertEqual(obs["metric_kind"], "stock")
        self.assertIsNone(obs["period_start"])
        self.assertIsNone(obs["period_end"])
        self.assertTrue(any(i["code"] == "stock_date_unconfirmed" and i.get("observation_key") == obs["key"] for i in revised["issues"]))
