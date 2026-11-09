"""Read the supplied three-sheet registry without modifying its source workbook.

This module deliberately has no Django/ORM dependency.  Uncertain source periods,
published rounded ratios and missing counts remain uncertain in the preview.
"""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import parse_qs, unquote, urlparse

from openpyxl import load_workbook


TOPICS = [
    ("money", "Деньги и операции с ними"),
    ("internet", "Безопасность в интернете"),
    ("fraud", "Защита от финансового мошенничества"),
    ("consumer", "Потребительское поведение"),
    ("savings", "Сбережения"),
    ("investing", "Инвестирование"),
    ("credit", "Кредитование"),
    ("tax", "Налоги"),
    ("business", "Бизнес и предпринимательство"),
    ("pension", "Пенсия"),
    ("budget", "Ведение личного семейного бюджета"),
    ("insurance", "Страхование"),
    ("civic", "Вовлечение граждан в процессы управления городом"),
]


def _normal(value):
    return re.sub(r"\s+", " ", str(value or "").replace("ё", "е")).strip().casefold()


_ALIASES = { _normal(name): key for key, name in TOPICS }
_ALIASES.update({
    _normal("ДЕНЬГИ И ОПЕРАЦИИ"): "money",
    _normal("ЗАЩИТА ОТ МОШЕННИЧЕСТВА"): "fraud",
    _normal("НАЛОГИ И ВЫЧЕТЫ"): "tax",
    _normal("ЛИЧНЫЙ БЮДЖЕТ"): "budget",
    _normal("ВЕДЕНИЕ ЛИЧНОГО (СЕМЕЙНОГО) БЮДЖЕТА"): "budget",
    _normal("СТРАХОВАНИЕ*"): "insurance",
    _normal("ВОВЛЕЧЕНИЕ ГРАЖДАН В ПРОЦЕССЫ"): "civic",
})

# Explicit wording correspondences for the registry's cross-theme references.
# Equal numbers alone are never used as evidence that answers are identical.
_ANSWER_ALIASES = {
    _normal("Связанный показатель по налоговой осведомленности: имеют представление о налогах и обязательных платежах"):
        "Имеют представление о налогах и обязательных платежах",
    _normal("Считают граждан и бизнес в России недостаточно информированными о налогах"):
        "Считают плохо информированными",
    _normal("Оценивают собираемость налогов в России как среднюю"):
        "Скорее средняя",
}


def _key(prefix, *parts):
    return prefix + "-" + hashlib.sha256("|".join(_normal(p) for p in parts).encode()).hexdigest()[:20]


def _decimal(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = Decimal(str(value).replace("\u00a0", "").replace(" ", "").replace(",", "."))
        return format(result, "f") if result.is_finite() else None
    except (ValueError, InvalidOperation):
        return None


def _json_value(value):
    return value.isoformat() if isinstance(value, (date, datetime)) else value


def _link(cell):
    """Resolve both xlsx relationship hyperlinks and HYPERLINK formula strings."""
    raw = cell.hyperlink.target if cell.hyperlink else ""
    value = str(cell.value or "")
    if not raw:
        match = re.match(r'=HYPERLINK\(\s*"((?:[^"]|"")*)"', value, re.I)
        if match:
            raw = match.group(1).replace('""', '"')
        else:
            match = re.search(r"https?://[^\s<>]+", value)
            if match:
                raw = match.group()
    url = raw
    if url and "officeapps.live.com" in urlparse(url).netloc:
        url = parse_qs(urlparse(url).query).get("src", [url])[0]
    return {"url": url, "raw_url": raw, "label": value}


def parse_registry(path):
    """Return a source-backed, JSON-serializable registry import preview.

    Value strings are decimal, not binary floats.  No original counts of correct
    answers are inferred from published rounded percentages.  The caller decides
    whether a preview with warnings should be imported after human review.
    """
    path = Path(path)
    workbook = load_workbook(path, data_only=False)
    payload = {
        "schema_version": 1,
        "files": [{"name": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}],
        "topics": [{"key": key, "name": name} for key, name in TOPICS],
        "topic_aliases": [], "studies": [], "indicators": [], "questions": [],
        "observations": [], "issues": [], "notes": [], "workbook": [],
    }
    indicators = {}
    questions = {}
    aliases = {}

    def issue(code, message, sheet="", cell="", observation_key=None, severity="warning"):
        item = {"key": _key("issue", code, sheet, cell, observation_key), "code": code,
                "message": message, "severity": severity,
                "source": {"file": path.name, "sheet": sheet, "cell": cell}}
        if observation_key:
            item["observation_key"] = observation_key
        payload["issues"].append(item)
        return item["key"]

    def topic(raw, sheet, cell):
        canonical = _ALIASES.get(_normal(raw))
        if canonical:
            aliases[str(raw)] = canonical
        else:
            issue("unknown_topic", f"Неизвестная тема: {raw}", sheet, cell)
        return canonical

    def indicator(section, title, unit, topics, **extra):
        key = _key(section, title)
        if key not in indicators:
            indicators[key] = {"key": key, "section": section, "category": section,
                               "name": title, "unit": unit, "topics": list(filter(None, topics)), **extra}
        else:
            indicators[key]["topics"] = sorted(set(indicators[key]["topics"]) | set(filter(None, topics)))
        return key

    def question(section, title, topics, study_key, multiple_choice="unknown"):
        ikey = indicator(section, title, "%", topics)
        qkey = _key("question", section, title)
        if qkey not in questions:
            questions[qkey] = {"key": qkey, "indicator_key": ikey, "section": section,
                               "text": title, "version": 1, "version_key": _key("qv", section, title, "1"),
                               "topics": list(filter(None, topics)), "study_key": study_key,
                               "multiple_choice": multiple_choice, "answers": []}
        else:
            questions[qkey]["topics"] = sorted(set(questions[qkey]["topics"]) | set(filter(None, topics)))
        return ikey, qkey

    def observation(section, ikey, qkey, value, unit, source, period, **extra):
        raw = _json_value(value)
        number = _decimal(value)
        data_state = "value" if number is not None else "missing"
        if _normal(value) in {"неприменимо", "не применимо", "n/a"}:
            data_state = "na"
        base = {"section": section, "category": section, "indicator_key": ikey,
                "question_key": qkey, "question_version": 1 if qkey else None,
                "value": number, "raw_value": raw, "unit": unit,
                "data_state": data_state, "territory": "Москва", "group": "Все респонденты" if qkey else "",
                "answer": "", "base": None, "numerator": None, "denominator": None,
                "metric_kind": "unknown", "period_type": "unknown", "period_label": period,
                "period_start": None, "period_end": None, "publication_date": None,
                "ingestion_method": "manual", "verification": "unverified", "source": source,
                "issues": [], **extra}
        base["key"] = _key("obs", ikey, qkey, base["question_version"], base["territory"], period,
                           base["group"], base["answer"], unit)
        payload["observations"].append(base)
        return base

    for sheet in workbook:
        comments = [{"cell": c.coordinate, "text": c.comment.text, "author": c.comment.author}
                    for row in sheet for c in row if c.comment]
        hyperlinks = [{"cell": c.coordinate, **_link(c)} for row in sheet for c in row
                      if c.hyperlink or (c.data_type == "f" and str(c.value).upper().startswith("=HYPERLINK"))]
        payload["workbook"].append({"sheet": sheet.title, "rows": sheet.max_row, "columns": sheet.max_column,
                                    "merged_ranges": [str(r) for r in sheet.merged_cells.ranges],
                                    "state": sheet.sheet_state, "comments": comments, "hyperlinks": hyperlinks})
        name = _normal(sheet.title)
        if "соц" in name:
            header = str(sheet["A1"].value or "")
            match = re.search(r"(\d{2}\.\d{2}\.\d{4})\s*-\s*(\d{2}\.\d{2}\.\d{4})", header)
            start = datetime.strptime(match[1], "%d.%m.%Y").date().isoformat() if match else None
            end = datetime.strptime(match[2], "%d.%m.%Y").date().isoformat() if match else None
            period = f"{start} — {end}" if match else header
            # A new fieldwork wave must not silently become a new study.
            study_name = header[:match.start()].rstrip(" ,;:-") if match else header
            study_key = _key("study", "sociology", study_name)
            payload["studies"].append({"key": study_key, "name": study_name, "section": "sociology",
                                      "source_header": header,
                                      "wave": period, "period_start": start, "period_end": end, "base": None})
            existing_savings_insurance = None
            existing_sociology = {}
            for row in range(3, sheet.max_row + 1):
                raw_topic, title, answer, value, raw_unit, page = [sheet.cell(row, c).value for c in range(1, 7)]
                if raw_topic is None:
                    continue
                if str(raw_topic).startswith("*"):
                    payload["notes"].append({"text": str(raw_topic), "source": {"file": path.name, "sheet": sheet.title, "cell": f"A{row}"}})
                    continue
                if not title:
                    continue
                tkey = topic(raw_topic, sheet.title, f"A{row}")
                source = {"file": path.name, "sheet": sheet.title, "cell": f"D{row}", "page": page,
                          "url": "", "label": "Исходный Excel-реестр, социологическое исследование"}
                if "первоначальной версии" in str(title) and _decimal(value) is None:
                    payload["notes"].append({"code": "no_questions", "text": str(answer or title), "source": source})
                    issue("no_question", "В источнике прямо указано отсутствие вопроса; запись не создает вымышленный вопрос или нулевое наблюдение.", sheet.title, f"D{row}")
                    continue
                if "первоначальной версии" in str(title) and "страхован" in str(answer).casefold() and existing_savings_insurance:
                    # This is explicitly the same value repeated in a proposed second rubric.
                    prior = existing_savings_insurance
                    source["raw_answer"] = str(answer or "")
                    source["raw_question"] = str(title)
                    prior.setdefault("source_references", [prior["source"]]).append(source)
                    prior.setdefault("topic_keys", []).append("insurance")
                    indicators[prior["indicator_key"]]["topics"] = sorted(set(indicators[prior["indicator_key"]]["topics"]) | {"insurance"})
                    questions[prior["question_key"]]["topics"] = sorted(set(questions[prior["question_key"]]["topics"]) | {"insurance"})
                    payload["notes"].append({"code": "cross_topic_reference", "text": "Повторная строка про страхование жизни связана с исходным наблюдением о сбережениях; значение не скопировано.", "source": source, "observation_key": prior["key"]})
                    continue
                raw_answer = str(answer or "")
                if tkey == "business" and _normal(answer) in _ANSWER_ALIASES:
                    answer = _ANSWER_ALIASES[_normal(answer)]
                    source["raw_answer"] = raw_answer
                ikey, qkey = question("sociology", str(title), [tkey], study_key)
                if str(answer or "") not in questions[qkey]["answers"]:
                    questions[qkey]["answers"].append(str(answer or ""))
                unit = "ранг" if "ранг" in str(raw_unit) else "%" if "%" in str(raw_unit) else str(raw_unit or "")
                indicators[ikey]["unit"] = unit
                group = str(raw_unit).replace("% ", "", 1) if "среди" in str(raw_unit) or "согласившихся" in str(raw_unit) else "Все респонденты"
                dimension = (qkey, _normal(answer), _normal(group), unit)
                prior = existing_sociology.get(dimension)
                if prior is not None:
                    prior.setdefault("source_references", [prior["source"]]).append(source)
                    prior["topic_keys"] = sorted(set(prior.get("topic_keys", [])) | set(filter(None, [tkey])))
                    if prior["value"] != _decimal(value):
                        ik = issue("cross_topic_value_conflict", "Повторная тематическая ссылка содержит другое значение того же ответа. Обе исходные версии требуют сверки; приоритет не выбран.", sheet.title, f"D{row}", prior["key"])
                        payload["issues"][-1]["competing_values"] = [{"value": prior["value"], "source": prior["source"]}, {"value": _decimal(value), "source": source}]
                        prior["issues"].append(ik)
                        prior["verification"] = "conflict"
                    else:
                        payload["notes"].append({"code": "cross_topic_reference", "text": "Повторная тематическая ссылка на тот же вопрос/ответ связана с исходным наблюдением без копирования значения.", "source": source, "observation_key": prior["key"]})
                    continue
                obs = observation("sociology", ikey, qkey, value, unit, source, period,
                                  answer=str(answer or ""), group=group, raw_unit=raw_unit,
                                  period_type="wave", period_start=start, period_end=end,
                                  study_key=study_key, topic_keys=[tkey] if tkey else [], metric_kind="flow")
                existing_sociology[dimension] = obs
                obs["issues"].append(issue("unknown_base", "Численная база расчета и числитель в реестре не указаны; опубликованная доля сохранена без восстановления численностей.", sheet.title, f"D{row}", obs["key"], "info"))
                if "страхован" in str(answer).casefold() and "В какой форме" in str(title):
                    existing_savings_insurance = obs
            # Neither a sum of answer shares nor wording alone proves the sampling method.
            for qkey, q in questions.items():
                if q["section"] != "sociology":
                    continue
                shares = [Decimal(o["value"]) for o in payload["observations"] if o["question_key"] == qkey and o["value"] is not None and o["unit"] == "%"]
                if shares and sum(shares) > 100:
                    q["multiple_choice"] = "possible_or_overlapping_categories"
                    q["notes"] = "Доли суммарно превышают 100%; возможен множественный выбор или вложенные категории. Не нормировать до 100%."
        elif "статист" in name:
            for row in range(3, sheet.max_row + 1):
                raw_topic, title, raw_unit = [sheet.cell(row, c).value for c in range(1, 4)]
                if not title:
                    continue
                tkey = topic(raw_topic, sheet.title, f"A{row}")
                ikey = indicator("statistics", str(title), str(raw_unit or ""), [tkey])
                kind = "cumulative" if "с начала 2024" in str(title).casefold() else "stock" if "Остатки средств" in str(title) else "unknown"
                for column, source_column in [(4, 5), (6, 7), (8, 9)]:
                    cell = sheet.cell(row, column)
                    period = str(sheet.cell(2, column).value or "")
                    link = _link(sheet.cell(row, source_column))
                    source = {"file": path.name, "sheet": sheet.title, "cell": cell.coordinate,
                              "source_cell": sheet.cell(row, source_column).coordinate, **link}
                    obs = observation("statistics", ikey, None, cell.value, str(raw_unit or ""), source, period,
                                      topic_keys=[tkey] if tkey else [], metric_kind=kind)
                    if "/" in period:
                        obs["issues"].append(issue("ambiguous_period", "Заголовок совмещает IV квартал и год; точный период требует проверки публикации, автоматически год или квартал не выбран.", sheet.title, cell.coordinate, obs["key"]))
                    elif re.match(r"I кв\.\s*(\d{4})", period):
                        year = re.search(r"\d{4}", period).group()
                        if kind == "stock":
                            obs["period_type"] = "date_unconfirmed"
                            obs["issues"].append(issue("stock_date_unconfirmed", "Показатель является остатком на дату, но заголовок задает только квартал. Точную отчетную дату нельзя заменять квартальным интервалом; требуется первичная публикация.", sheet.title, cell.coordinate, obs["key"]))
                        else:
                            obs.update(period_start=f"{year}-01-01", period_end=f"{year}-03-31", period_type="quarter")
                    if not raw_unit:
                        obs["issues"].append(issue("missing_unit", "Единица измерения не заполнена в реестре.", sheet.title, f"C{row}", obs["key"]))
                    title_unit = "тыс. руб." if "тыс. руб." in str(title) else "млн ед." if "млн ед." in str(title) else None
                    if title_unit and ((title_unit == "тыс. руб." and raw_unit == "руб.") or (title_unit == "млн ед." and "тыс." in str(raw_unit))):
                        obs["issues"].append(issue("unit_conflict", f"В названии указано «{title_unit}», в колонке единиц — «{raw_unit}». Число сохранено как опубликовано; преобразование запрещено до проверки.", sheet.title, f"B{row}:C{row}", obs["key"]))
                    if "Среднедушевые" in str(title) and "insurance" in source["url"]:
                        obs["issues"].append(issue("source_mismatch", "Ссылка на доходы ведет на раздел страховой статистики Банка России; требуется корректная публикация Росстата.", sheet.title, source["source_cell"], obs["key"]))
                    if not source["url"] and obs["data_state"] == "value":
                        obs["issues"].append(issue("missing_source_url", "Число есть, но прямая ссылка на публикацию в реестре отсутствует.", sheet.title, source["source_cell"], obs["key"], "info"))
        elif "тест" in name:
            header = str(sheet["A1"].value or "Финансовый тест")
            study_name = re.split(r",\s*\d{2}\.\d{4}", header, maxsplit=1)[0].rstrip(" ,;:-")
            study_key = _key("study", "fintest", study_name)
            payload["studies"].append({"key": study_key, "name": study_name, "source_header": header,
                                      "section": "fintest", "wave": "", "base": None})
            issue("unconfirmed_territory", "Владельцем финтеста назван Департамент финансов Москвы, но территория проживания участников не указана. Территория сохранена как «Не уточнено», а не приравнена к Москве.", sheet.title, "A1")
            for row in range(4, sheet.max_row + 1):
                raw_topic, title = sheet.cell(row, 1).value, sheet.cell(row, 2).value
                if not title:
                    continue
                tkey = topic(raw_topic, sheet.title, f"A{row}")
                if _normal(title) == "вопросы отсутствуют":
                    payload["notes"].append({"code": "no_questions", "text": f"{raw_topic}: вопросы отсутствуют в источнике.", "source": {"file": path.name, "sheet": sheet.title, "cell": f"B{row}"}})
                    continue
                ikey, qkey = question("fintest", str(title), [tkey], study_key, "unknown")
                for ncol, pcol in [(3, 4), (5, 6), (7, 8)]:
                    period = str(sheet.cell(2, ncol).value or "")
                    count = sheet.cell(row, ncol).value
                    share = sheet.cell(row, pcol).value
                    if count is None and share is None:
                        continue
                    dates = ("2024-08-01", "2024-12-31") if "2024" in period else ("2025-01-01", "2025-12-31") if "2025" in period else ("2026-01-01", "2026-06-30") if "2026" in period else (None, None)
                    number = _decimal(share)
                    # The source workbook's fraction format is percentage. Preserve raw too.
                    percent = format(Decimal(number) * 100, "f") if number is not None else None
                    source = {"file": path.name, "sheet": sheet.title, "cell": sheet.cell(row, pcol).coordinate,
                              "count_cell": sheet.cell(row, ncol).coordinate, "url": "", "label": "Исходный Excel-реестр, финансовый тест"}
                    obs = observation("fintest", ikey, qkey, percent, "%", source, period,
                                      raw_value=share, raw_unit=sheet.cell(row, pcol).number_format,
                                      answer="Опубликованная доля верных ответов", denominator=_decimal(count),
                                      numerator=None, base=_decimal(count), answer_count=_decimal(count), correct_count=None,
                                      period_start=dates[0], period_end=dates[1], period_type="period", metric_kind="flow",
                                      study_key=study_key, topic_keys=[tkey] if tkey else [], published_rounded=True,
                                      territory="Не уточнено")
                    obs["issues"].append(issue("rounded_percentage", "В реестре есть количество прохождений и округленная доля; точное число верных ответов неизвестно. Не вычислять числитель и сводную точную долю по этим данным.", sheet.title, source["cell"], obs["key"], "info"))
                    if _decimal(count) == "0":
                        obs["issues"].append(issue("zero_denominator", "В источнике ноль прохождений и нулевая доля. Ноль сохранен буквально, но доля при нулевом знаменателе не определена; не трактовать как результат 0%.", sheet.title, source["count_cell"], obs["key"]))
                        obs["verification"] = "needs_review"
                        obs["value"] = None
                        obs["data_state"] = "na"
            for cell in [sheet["J1"]]:
                if cell.value:
                    payload["notes"].append({"text": str(cell.value), "source": {"file": path.name, "sheet": sheet.title, "cell": cell.coordinate}})
        else:
            issue("unsupported_sheet", "Лист не распознан; данные не импортированы автоматически.", sheet.title, "", severity="error")

    payload["indicators"] = list(indicators.values())
    payload["questions"] = list(questions.values())
    payload["topic_aliases"] = [{"alias": alias, "topic_key": key} for alias, key in aliases.items()]
    payload["summary"] = {"sheets": len(payload["workbook"]), "indicators": len(indicators),
                          "questions": len(questions), "observations": len(payload["observations"]),
                          "by_section": dict(Counter(o["section"] for o in payload["observations"])),
                          "issues": len(payload["issues"]), "issues_by_code": dict(Counter(i["code"] for i in payload["issues"]))}
    return payload


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("workbook")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    Path(args.output).write_text(json.dumps(parse_registry(args.workbook), ensure_ascii=False, indent=2), encoding="utf-8")
