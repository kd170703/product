"""Reproducible Word/Excel exports from the same filtered rows as the dashboard.

No queries are performed by the renderers: a published report is a materialized
snapshot, never a live query whose content changes after publication.
"""
from __future__ import annotations

import hashlib
import io
import json
from collections import OrderedDict
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo
from decimal import Decimal, InvalidOperation
from typing import Any

from .registry_parser import TOPICS

SECTION_NAMES = {
    "sociology": "Социология",
    "statistics": "Социально-экономические показатели",
    "fintest": "Финтест",
    "irfk": "ИРФК",
    "quiz": "Онлайн-зачёт по финансовой грамотности",
}
MAX_CHARTS_PER_TOPIC_SECTION = 2
STATE_NAMES = {"missing": "Нет данных", "na": "Неприменимо"}
PERIOD_NAMES = {"stock": "На дату", "flow": "За период", "cumulative": "Накопительный итог", "unknown": "Не уточнён"}


def _json_value(value: Any):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, set):
        return sorted(value)
    return str(value)


def serializable(value):
    return json.loads(json.dumps(value, default=_json_value, ensure_ascii=False))


def _get(row, *keys, default=""):
    for key in keys:
        value = row.get(key)
        if value is not None and value != "":
            return value
    return default


def normalize_row(row):
    """Accept selectors' stable schema and useful aliases for import snapshots."""
    row = dict(row)
    source = row.get("source") if isinstance(row.get("source"), dict) else {}
    provenance = row.get("provenance") or {}
    if not isinstance(provenance, dict):
        provenance = {"detail": str(provenance)}
    topics = row.get("topics") or row.get("topic_names") or []
    if isinstance(topics, str):
        topics = [topics]
    names = [str(t.get("name") or t.get("title") or t.get("slug") or "") if isinstance(t, dict) else str(t) for t in topics]
    value = row.get("value")
    state = row.get("data_state") or ("missing" if value is None else "value")
    return {
        **row,
        "indicator_code": _get(row, "indicator_code", "code"),
        "indicator_name": _get(row, "indicator_name", "indicator", "name", "title", default="Показатель"),
        "section": _get(row, "section", "category"),
        "topics": names,
        "value": value, "data_state": state,
        "unit": _get(row, "unit"), "territory": _get(row, "territory"),
        "period_label": _get(row, "period_label", "period"),
        "period_start": row.get("period_start"), "period_end": row.get("period_end"),
        "period_kind": _get(row, "period_kind", "metric_kind", default="unknown"),
        "group": _get(row, "group", "respondent_group"),
        "answer": _get(row, "answer", "answer_label"),
        "source_name": _get(row, "source_name", default=source.get("name", "")),
        "source_url": _get(row, "source_url", "url", default=source.get("url", "")),
        "publication_date": _get(row, "publication_date", "published_at"),
        "loaded_at": _get(row, "loaded_at", "created_at"),
        "observation_version_id": _get(row, "observation_version_id", "version_id"),
        "method_version_id": _get(row, "method_version_id", "formula_version_id"),
        "provenance": provenance,
    }


def display_value(row):
    if row.get("data_state") != "value":
        return STATE_NAMES.get(row.get("data_state"), "Нет данных")
    value = row.get("value")
    if value is None:
        return "Нет данных"
    try:
        number = Decimal(str(value))
        rendered = format(number, "f").rstrip("0").rstrip(".") if "." in format(number, "f") else format(number, "f")
        return rendered.replace(".", ",")
    except (InvalidOperation, ValueError):
        return str(value)


def _native_number(value):
    if value in (None, ""):
        return None
    try:
        d = Decimal(str(value))
        if not d.is_finite():
            return None
        return int(d) if d == d.to_integral_value() else float(d)
    except (InvalidOperation, ValueError, TypeError):
        return None


def _native_date(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, date):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _safe_url(value):
    from urllib.parse import urlsplit
    try:
        parts = urlsplit(str(value))
        return str(value) if parts.scheme in {"https", "http"} and parts.netloc and not parts.username else ""
    except ValueError:
        return ""


def source_location(row):
    p = row.get("provenance") or {}
    return ", ".join(str(p[k]) for k in ("file", "filename", "sheet", "page", "table", "cell", "code") if p.get(k) not in (None, ""))


def _topic_rows(rows):
    groups = OrderedDict((name, []) for _, name in TOPICS)
    remaining = []
    aliases = {slug: name for slug, name in TOPICS}
    # Values are stored once; a thematic report may show one observation under
    # several topics. Its stable row/version IDs remain visible in the export.
    for row in rows:
        assigned = False
        for topic in row["topics"]:
            name = aliases.get(topic, topic)
            for actual in groups:
                if actual.casefold() == name.casefold():
                    groups[actual].append(row)
                    assigned = True
                    break
        if not assigned:
            remaining.append(row)
    if remaining:
        groups["Без тематической рубрики"] = remaining
    return groups


def _filters_description(filters):
    labels = {"start": "Начало периода", "end": "Конец периода", "indicator": "Код показателя", "period_start": "Начало периода", "period_end": "Конец периода", "territory": "Территория", "topic": "Тема", "section": "Раздел", "q": "Поиск", "period": "Период"}
    topic_names = dict(TOPICS)
    def label_value(key, value):
        if key == "section": return SECTION_NAMES.get(value, value)
        if key == "topic": return topic_names.get(value, value)
        return value
    return "; ".join(f"{labels.get(k, k)}: {label_value(k, v)}" for k, v in filters.items() if v not in (None, "", [])) or "Все доступные периоды и территории"


def _chart_groups(rows):
    groups = OrderedDict()
    for row in rows:
        if row["data_state"] != "value" or row.get("status") == "conflict" or row.get("correction_conflict") or _native_number(row["value"]) is None:
            continue
        if row["section"] == "statistics" and (
            not row.get("period_end") or not row.get("unit") or row["unit"] == "Не уточнено"
            or any("единиц" in str(issue).casefold() for issue in row.get("issues", []))
        ):
            continue
        key = (row["indicator_code"] or row["indicator_name"], row["unit"], row["territory"], row["period_kind"], row["group"], row.get("question_version_id"))
        groups.setdefault(key, []).append(row)
    return groups.values()


def _chart_png(rows):
    """Draw one comparable indicator; never mix units, groups, or territories."""
    import matplotlib
    matplotlib.use("Agg")
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.ticker import MaxNLocator
    import textwrap

    first = rows[0]
    categorical = rows[0].get("section") == "sociology" and any(row.get("answer") for row in rows)
    # Distribution comparisons require one period. A chart is faceted by period
    # by the caller, and multiple-choice sums are never forced to 100 percent.
    rows = sorted(rows, key=lambda row: (str(row.get("period_end") or row["period_label"]), row["answer"]))
    values = [_native_number(row["value"]) for row in rows]
    labels = [str(row["answer"] or row["period_label"] or row.get("period_end") or "Без периода") for row in rows]
    fig = Figure(figsize=(9, max(3.4, min(10, 1.7 + len(rows) * .38))) if categorical else (9, 4.3), dpi=130)
    canvas = FigureCanvasAgg(fig)
    ax = fig.add_subplot(111)
    if categorical:
        ax.barh(range(len(rows)), values, color="#246a92", height=.65)
        ax.set_yticks(range(len(rows)), ["\n".join(textwrap.wrap(label, 44)) for label in labels])
        ax.invert_yaxis()
        ax.set_xlabel(first["unit"])
        ax.xaxis.set_major_locator(MaxNLocator(6))
    else:
        if len(rows) == 1:
            ax.bar(range(len(rows)), values, color="#246a92", width=.45)
        else:
            ax.plot(range(len(rows)), values, color="#246a92", marker="o", linewidth=2)
        ax.set_xticks(range(len(rows)), ["\n".join(textwrap.wrap(label, 18)) for label in labels], rotation=25 if len(rows) > 4 else 0)
        ax.set_ylabel(first["unit"])
        ax.yaxis.set_major_locator(MaxNLocator(6))
    ax.grid(axis="x" if categorical else "y", alpha=.18)
    ax.set_axisbelow(True)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    period_text = ", ".join(dict.fromkeys(row["period_label"] or str(row.get("period_end") or "") for row in rows))
    source_text = "; ".join(dict.fromkeys(row["source_name"] or row["source_url"] or source_location(row) or "Источник не указан" for row in rows))
    fig.suptitle("\n".join(textwrap.wrap(first["indicator_name"], 95)), fontsize=12, fontweight="bold")
    fig.text(.02, .01, "\n".join(textwrap.wrap(f"{first['unit']} · {first['territory']} · {period_text}\nИсточник: {source_text}", 125)), fontsize=8)
    fig.tight_layout(rect=(0, .12, 1, .89))
    buf = io.BytesIO()
    canvas.print_png(buf)
    return buf.getvalue()


def _add_hyperlink(paragraph, label, url):
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.opc.constants import RELATIONSHIP_TYPE as RT
    url = _safe_url(url)
    if not url:
        paragraph.add_run(label)
        return
    rel_id = paragraph.part.relate_to(url, RT.HYPERLINK, is_external=True)
    link = OxmlElement("w:hyperlink")
    link.set(qn("r:id"), rel_id)
    run = OxmlElement("w:r")
    properties = OxmlElement("w:rPr")
    color = OxmlElement("w:color")
    color.set(qn("w:val"), "12608A")
    properties.append(color)
    underline = OxmlElement("w:u")
    underline.set(qn("w:val"), "single")
    properties.append(underline)
    run.append(properties)
    text = OxmlElement("w:t")
    text.text = label
    run.append(text)
    link.append(run)
    paragraph._p.append(link)


def render_docx(rows, filters=None, metadata=None):
    from docx import Document
    from docx.shared import Inches, Pt, Cm
    from docx.oxml.ns import qn
    rows = [normalize_row(row) for row in rows]
    filters, metadata = filters or {}, metadata or {}
    doc = Document()
    section = doc.sections[0]
    section.top_margin = section.bottom_margin = Cm(2)
    section.left_margin, section.right_margin = Cm(2), Cm(1.5)
    for style in doc.styles:
        if style.type == 1:
            style.font.name = "Times New Roman"
            style.font.size = Pt(14)
            style._element.get_or_add_rPr().rFonts.set(qn("w:eastAsia"), "Times New Roman")
    doc.add_heading("Мониторинг финансовой культуры", 0)
    doc.add_paragraph("Квартальный отчёт · " + str(metadata.get("period_label") or filters.get("period") or "выбранные данные"))
    doc.add_paragraph(_filters_description(filters))
    doc.add_paragraph(f"Сформирован: {metadata.get('generated_at', datetime.now(ZoneInfo("Europe/Moscow")).isoformat())}")
    doc.add_paragraph(f"Наблюдений в срезе: {len(rows)}. Снимок данных: {metadata.get('checksum', 'не опубликован')}.")
    if metadata.get("limitations"):
        doc.add_paragraph("Ограничения: " + str(metadata["limitations"]))
    doc.add_paragraph("Записи с конфликтами сохранены в таблицах с отметками и не включаются в диаграммы. Непроверенные записи требуют подтверждения сотрудником.")
    doc.add_paragraph("Внутри каждой темы сохранён порядок блоков исходной сводной таблицы: социология, социально-экономические показатели, финтест. ИРФК и онлайн-зачёт приведены отдельными дополнительными блоками. Для читаемости подробности каждого блока расположены в собственной редактируемой таблице.")
    doc.add_paragraph("Диаграммы показывают до двух сопоставимых рядов в каждом тематическом блоке; полный отфильтрованный набор наблюдений сохранён в таблицах Word и Excel.")
    doc.add_paragraph("Ноль — измеренное значение; «Нет данных» — значение отсутствует; «Неприменимо» — показатель не применяется. Проценты, опубликованные в округлённом виде, не заменяются вымышленными числителями.")
    for topic_name, topic_rows in _topic_rows(rows).items():
        doc.add_heading(topic_name, 1)
        if not topic_rows:
            doc.add_paragraph("В выбранном срезе данные отсутствуют.")
            continue
        for section_code, section_name in SECTION_NAMES.items():
            section_rows = [row for row in topic_rows if row["section"] == section_code]
            if not section_rows:
                continue
            doc.add_heading(section_name, 2)
            table = doc.add_table(rows=1, cols=6)
            table.style = "Table Grid"
            for cell, title in zip(table.rows[0].cells, ["Показатель / ответ", "Значение", "Единица", "Период / территория", "Источник", "Версия"]):
                cell.text = title
            for row in section_rows:
                cells = table.add_row().cells
                cells[0].text = row["indicator_name"] + ("\n" + row["answer"] if row["answer"] else "") + ("\nГруппа: " + row["group"] if row["group"] else "")
                cells[1].text = display_value(row)
                cells[2].text = row["unit"]
                cells[3].text = f"{row['period_label']}\n{row['territory']}\n{PERIOD_NAMES.get(row['period_kind'], row['period_kind'])}"
                _add_hyperlink(cells[4].paragraphs[0], row["source_name"] or row["source_url"] or "Исходный файл", row["source_url"])
                if source_location(row):
                    cells[4].add_paragraph(source_location(row))
                if row["publication_date"]:
                    cells[4].add_paragraph("Публикация: " + str(row["publication_date"]))
                cells[5].text = str(row["observation_version_id"] or row.get("id") or "")
                if row.get("status"):
                    cells[5].add_paragraph("Статус: " + {"pending": "Требует проверки", "verified": "Проверено", "conflict": "Есть расхождение"}.get(row["status"], str(row["status"])))
                if row.get("issues"):
                    cells[5].add_paragraph("Вопросы проверки: " + "; ".join(str(issue) for issue in row["issues"]))
                if row.get("correction_id") or row.get("manual_correction"):
                    cells[5].add_paragraph("Применена ручная поправка; исходное значение сохранено в истории.")
                # No w:shd is introduced: editable source-aligned tables have no fill.
                for cell in cells:
                    for p in cell.paragraphs:
                        for run in p.runs:
                            run.font.name = "Times New Roman"
                            run.font.size = Pt(14)
            comparable_facets = []
            for group in _chart_groups(section_rows):
                facets = OrderedDict()
                for row in group:
                    facet = str(row.get("period_end") or row["period_label"]) if row["section"] == "sociology" and row["answer"] else "series"
                    facets.setdefault(facet, []).append(row)
                comparable_facets.extend(facets.values())
            for facet_rows in comparable_facets[:MAX_CHARTS_PER_TOPIC_SECTION]:
                try:
                    doc.add_picture(io.BytesIO(_chart_png(facet_rows)), width=Inches(6.5))
                except (ValueError, TypeError) as exc:
                    raise ValueError(f"Не удалось построить диаграмму «{facet_rows[0]['indicator_name']}»: {exc}") from exc
    doc.add_heading("Источники и версии", 1)
    sources = OrderedDict()
    for row in rows:
        key = (row["source_name"], row["source_url"], source_location(row))
        sources.setdefault(key, row)
    for row in sources.values():
        p = doc.add_paragraph()
        _add_hyperlink(p, row["source_name"] or row["source_url"] or "Исходный файл", row["source_url"])
        if source_location(row):
            p.add_run(" · " + source_location(row))
    doc.add_paragraph("Версии наблюдений: " + ", ".join(str(v) for v in metadata.get("data_version_ids", []) if v != ""))
    doc.add_paragraph("Версии методики: " + json.dumps(metadata.get("method_versions", []), ensure_ascii=False, default=_json_value))
    output = io.BytesIO()
    doc.save(output)
    return output.getvalue()


def render_xlsx(rows, filters=None, metadata=None):
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment
    from openpyxl.utils import get_column_letter
    rows = [normalize_row(row) for row in rows]
    filters, metadata = filters or {}, metadata or {}
    wb = Workbook()
    summary = wb.active
    summary.title = "Сводка"
    summary.append(["Мониторинг финансовой культуры"])
    summary.append(["Фильтры", _filters_description(filters)])
    summary.append(["Наблюдений", len(rows)])
    summary.append(["Снимок", metadata.get("checksum", "")])
    summary.append([])
    summary.append(["Раздел", "Наблюдений", "Значений", "Нет данных", "Неприменимо"])
    for code, name in SECTION_NAMES.items():
        subset = [row for row in rows if row["section"] == code]
        summary.append([name, len(subset), sum(row["data_state"] == "value" for row in subset), sum(row["data_state"] == "missing" for row in subset), sum(row["data_state"] == "na" for row in subset)])
    summary.append([])
    summary.append(["Примечание", "Разные единицы не суммируются. Одна запись может относиться к нескольким темам."])
    ws = wb.create_sheet("Наблюдения")
    headers = ["ID", "Код", "Показатель", "Раздел", "Темы", "Значение", "Состояние", "Единица", "Территория", "Начало периода", "Конец периода", "Период", "Тип периода", "Группа", "Вариант ответа", "Числитель", "Знаменатель", "База расчёта", "Источник", "Ссылка", "Файл / лист / ячейка", "Дата публикации", "Дата загрузки", "Версия данных", "Версия методики", "Статус проверки", "Вопросы проверки", "Ручная поправка"]
    ws.append(headers)
    for row in rows:
        ws.append([
            row.get("id"), row["indicator_code"], row["indicator_name"], SECTION_NAMES.get(row["section"], row["section"]), "; ".join(row["topics"]),
            _native_number(row["value"]) if row["data_state"] == "value" else None,
            "Значение" if row["data_state"] == "value" else STATE_NAMES.get(row["data_state"], row["data_state"]),
            row["unit"], row["territory"], _native_date(row["period_start"]), _native_date(row["period_end"]), row["period_label"],
            PERIOD_NAMES.get(row["period_kind"], row["period_kind"]), row["group"], row["answer"], _native_number(row.get("numerator")), _native_number(row.get("denominator")),
            _native_number(row.get("base")) if _native_number(row.get("base")) is not None else str(row.get("base") or ""),
            row["source_name"], _safe_url(row["source_url"]), source_location(row), _native_date(row["publication_date"]), _native_date(row["loaded_at"]),
            row["observation_version_id"], row["method_version_id"], row.get("status", ""),
            "; ".join(str(issue) for issue in row.get("issues", [])), str(row.get("correction_id") or row.get("manual_correction") or ""),
        ])
        for idx in (10, 11, 22, 23):
            ws.cell(ws.max_row, idx).number_format = "DD.MM.YYYY" if idx != 23 else "DD.MM.YYYY HH:MM"
        if _safe_url(row["source_url"]):
            ws.cell(ws.max_row, 20).hyperlink = row["source_url"]
            ws.cell(ws.max_row, 20).style = "Hyperlink"
    sources = wb.create_sheet("Источники")
    sources.append(["Источник", "Ссылка", "Место в исходном файле", "Дата публикации"])
    seen = set()
    for row in rows:
        key = (row["source_name"], row["source_url"], source_location(row), str(row["publication_date"]))
        if key in seen:
            continue
        seen.add(key)
        sources.append([row["source_name"], _safe_url(row["source_url"]), source_location(row), _native_date(row["publication_date"])])
        sources.cell(sources.max_row, 4).number_format = "DD.MM.YYYY"
        if _safe_url(row["source_url"]):
            sources.cell(sources.max_row, 2).hyperlink = row["source_url"]
            sources.cell(sources.max_row, 2).style = "Hyperlink"
    meta = wb.create_sheet("Метаданные")
    meta.append(["Параметр", "Значение"])
    for key, value in {"filters": filters, **metadata}.items():
        meta.append([str(key), json.dumps(value, ensure_ascii=False, default=_json_value) if isinstance(value, (dict, list)) else str(value)])
    for sheet in wb:
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions
        for cell in sheet[1]:
            cell.font = Font(name="Calibri", bold=True)
            cell.alignment = Alignment(wrap_text=True, vertical="top")
        for col in range(1, sheet.max_column + 1):
            letter = get_column_letter(col)
            longest = max((len(str(cell.value or "")) for cell in sheet[letter]), default=10)
            sheet.column_dimensions[letter].width = max(13, min(55, longest + 2))
        for record in sheet.iter_rows(min_row=2):
            for cell in record:
                cell.alignment = Alignment(wrap_text=True, vertical="top")
                # Avoid workbook formula injection from labels/documents.
                if cell.data_type == "f":
                    cell.data_type = "s"
    output = io.BytesIO()
    wb.save(output)
    return output.getvalue()


def report_period_label(filters):
    explicit=filters.get("period") or filters.get("period_label")
    if explicit:return str(explicit)
    start,end=filters.get("start"),filters.get("end")
    try:
        a=date.fromisoformat(str(start));b=date.fromisoformat(str(end))
        if a.year==b.year:
            import calendar
            if a.month==1 and a.day==1 and b.month==12 and b.day==31:return f"{a.year} год"
            if a.month in [1,4,7,10] and a.day==1 and b.month==a.month+2 and b.day==calendar.monthrange(b.year,b.month)[1]:
                return ["I","II","III","IV"][(a.month-1)//3]+f" квартал {a.year}"
        return a.strftime("%d.%m.%Y")+" — "+b.strftime("%d.%m.%Y")
    except (ValueError,TypeError):
        return "Все доступные периоды" if not start and not end else f"{start or 'Начало не задано'} — {end or 'Конец не задан'}"


def build_snapshot(rows, filters=None, author=None, method_versions=None):
    normalized = serializable([normalize_row(row) for row in rows])
    filters = serializable(dict(filters or {}))
    versions = sorted({str(row["observation_version_id"]) for row in normalized if row["observation_version_id"] not in (None, "")})
    methods = serializable(method_versions or sorted({str(row["method_version_id"]) for row in normalized if row["method_version_id"] not in (None, "")}))
    payload = {"schema_version": 1, "filters": filters, "rows": normalized, "data_version_ids": versions, "method_versions": methods, "period_label": report_period_label(filters)}
    payload["checksum"] = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    payload["generated_at"] = datetime.now(ZoneInfo("Europe/Moscow")).isoformat()
    payload["author"] = str(author) if author is not None else ""
    return payload


def generate_report(rows, filters, author=None):
    """Persist an immutable snapshot and both downloadable, materialized files."""
    from django.core.files.base import ContentFile
    from django.db import transaction
    from monitoring.models import ReportSnapshot, Methodology, Formula
    rows = list(rows)
    # Persist full formula definitions, approval flags and methodology versions.
    # Future edits of a methodology cannot rewrite the released report.
    methods = []
    for method in Methodology.objects.order_by("code", "version"):
        methods.append({"id": method.pk, "code": method.code, "version": method.version, "name": method.name, "description": method.description, "approved": method.approved, "source_publication_id": method.source_id, "formulas": list(Formula.objects.filter(methodology=method).values("indicator__code", "expression"))})
    snapshot = build_snapshot(rows, filters, author, method_versions=methods)
    metadata = {k: v for k, v in snapshot.items() if k != "rows"}
    docx_bytes = render_docx(snapshot["rows"], snapshot["filters"], metadata)
    xlsx_bytes = render_xlsx(snapshot["rows"], snapshot["filters"], metadata)
    # Model field names are deliberately explicit; content is written once.
    with transaction.atomic():
        report = ReportSnapshot.objects.create(
            title="Квартальный отчёт · " + report_period_label(filters),
            filters=snapshot["filters"], snapshot=snapshot, created_by=author,
            period_label=report_period_label(filters),
            territory=str(filters.get("territory") or "Все территории"),
        )
        stem = f"monitoring-{report.pk}-{snapshot['checksum'][:12]}"
        report.docx_file.save(stem + ".docx", ContentFile(docx_bytes), save=False)
        report.xlsx_file.save(stem + ".xlsx", ContentFile(xlsx_bytes), save=False)
        report.save(update_fields=["docx_file", "xlsx_file"])
    return report
