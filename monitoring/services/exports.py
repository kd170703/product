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
    labels = {"start": "Начало периода", "end": "Конец периода", "indicator": "Код показателя", "period_start": "Начало периода", "period_end": "Конец периода", "territory": "Территория", "topic": "Тема", "section": "Раздел", "q": "Поиск", "period": "Период", "period_mode":"Тип периода", "period_year":"Год", "period_quarter":"Квартал", "year":"Год", "quarter":"Квартал", "chart_type":"Вид диаграммы"}
    topic_names = dict(TOPICS)
    def label_value(key, value):
        if key == "section": return SECTION_NAMES.get(value, value)
        if key == "topic": return topic_names.get(value, value)
        if key == "period_mode": return {"all":"Все периоды", "quarter":"Квартал", "year":"Год", "date":"Диапазон дат"}.get(value,value)
        if key == "chart_type": return {"auto":"Автоматически", "line":"Линейная", "bar":"Столбчатая"}.get(value,value)
        return value
    return "; ".join(f"{labels.get(k, k)}: {label_value(k, v)}" for k, v in filters.items() if v not in (None, "", []) and not k.startswith("_")) or "Все доступные периоды и территории"


def _chart_groups(rows):
    from .charts import build_chart_specs
    return [spec['rows'] for spec in build_chart_specs(rows)]


def _chart_png(rows, chart_type="auto"):
    """Render the same comparable chart specification used by the application."""
    import matplotlib
    matplotlib.use('Agg')
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.ticker import MaxNLocator
    import textwrap
    rows = [normalize_row(row) for row in rows]
    first = rows[0]
    from .charts import build_chart_specs
    specs = build_chart_specs(rows, chart_type=chart_type)
    if not specs:
        raise ValueError('Для диаграммы нет числовых значений.')
    spec = specs[0]
    labels = [str(value) for value in spec.get('labels', [])]
    if spec.get('kind')!='line' and len(labels)==len(rows):
        labels=[(row['period_label'] or str(row.get('period_end') or 'Значение')) if not row['answer'] and label==row['indicator_name'] else label for row,label in zip(rows,labels)]
    datasets = spec.get('datasets') or [{'label':spec.get('title',''), 'values':spec.get('values',[])}]
    horizontal = spec.get('kind') != 'line' and len(datasets) == 1 and len(labels) > 1
    title = spec.get('title') or first['indicator_name']
    title_lines = textwrap.wrap(title,100)
    period_text = spec.get('period') or '; '.join(dict.fromkeys(row['period_label'] for row in rows))
    source_text = spec.get('source_name') or first['source_name'] or first['source_url'] or source_location(first) or 'Источник не указан'
    caption = f"{spec.get('unit',first['unit'])} · {spec.get('territory',first['territory'])} · {period_text}\nИсточник: {source_text}"
    caption_lines = [line for paragraph in caption.splitlines() for line in textwrap.wrap(paragraph,135)]
    title_height = .23 * max(1,len(title_lines)) + .16
    caption_height = .15 * max(1,len(caption_lines)) + .16
    plot_height = max(3.2,.9+sum(max(1,len(textwrap.wrap(label,44)))*.21 for label in labels)) if horizontal else 3.7
    height = title_height + caption_height + plot_height
    fig = Figure(figsize=(10,height),dpi=130)
    canvas = FigureCanvasAgg(fig)
    ax = fig.add_subplot(111)
    palette = ['#246a92','#d97c21','#548a53','#8c5fa8','#bd5662','#5e7b8e']
    if horizontal:
        numbers = [_native_number(value) for value in datasets[0].get('values', [])]
        bars = ax.barh(range(len(labels)), [float('nan') if value is None else value for value in numbers], color=palette[0],height=.65)
        ax.set_yticks(range(len(labels)), ['\n'.join(textwrap.wrap(label,44)) for label in labels])
        ax.invert_yaxis(); ax.set_xlabel(spec.get('unit',first['unit'])); ax.xaxis.set_major_locator(MaxNLocator(6))
        if len(labels) <= 16: ax.bar_label(bars, labels=['' if value is None else f'{value:g}' for value in numbers],padding=3,fontsize=8)
    else:
        x_positions=spec.get('x_values') if spec.get('kind')=='line' else None
        if not x_positions or len(x_positions)!=len(labels):x_positions=list(range(len(labels)))
        width = .75 / max(1,len(datasets))
        for index,dataset in enumerate(datasets):
            values = [_native_number(value) for value in dataset.get('values', [])]
            values = values[:len(labels)] + [None] * max(0,len(labels)-len(values))
            numbers = [float('nan') if value is None else value for value in values]
            color = palette[index % len(palette)]
            if spec.get('kind') == 'line':
                ax.plot(x_positions,numbers,color=color,marker='o',linewidth=2,label=dataset.get('label',''))
            else:
                positions = [n + (index-(len(datasets)-1)/2)*width for n in range(len(labels))]
                ax.bar(positions,numbers,width=width,color=color,label=dataset.get('label',''))
        tick_indices=list(range(len(labels))) if len(labels)<=8 else sorted({0,len(labels)-1,*range(0,len(labels),max(1,(len(labels)+6)//7))})
        ax.set_xticks([x_positions[i] for i in tick_indices],['\n'.join(textwrap.wrap(labels[i],20)) for i in tick_indices],rotation=25 if len(labels)>4 else 0)
        ax.set_ylabel(spec.get('unit',first['unit'])); ax.yaxis.set_major_locator(MaxNLocator(6))
        if len(datasets)>1: ax.legend(loc='best',fontsize=8)
    ax.grid(axis='x' if horizontal else 'y',alpha=.18); ax.set_axisbelow(True)
    for spine in ['top','right']: ax.spines[spine].set_visible(False)
    fig.suptitle('\n'.join(title_lines),fontsize=12,fontweight='bold',y=.99).set_in_layout(False)
    fig.text(.02,.015,'\n'.join(caption_lines),fontsize=8).set_in_layout(False)
    fig.tight_layout(rect=(0,caption_height/height,1,1-title_height/height))
    buf = io.BytesIO(); canvas.print_png(buf)
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


def _period_key(row):
    """Use the shared quarter parser; retain a deterministic legacy fallback."""
    try:
        from .periods import period_sort_key
        return period_sort_key(row)
    except ImportError:
        return (str(row.get('period_end') or row.get('period_start') or row.get('period_label') or ''),)


def _indicator_groups(rows):
    groups = OrderedDict()
    for row in rows:
        key = (row['indicator_code'] or row['indicator_name'], row['unit'], row['territory'], row['group'], row.get('question_version_id'))
        groups.setdefault(key, []).append(row)
    return list(groups.values())


def _plain_note(value):
    """Remove the former workflow badge from generated notes, not source values."""
    import re
    return re.sub(r'\bтребу(?:ет|ют)\s+проверки\b[.:;]?', '', str(value), flags=re.I).strip(' ·;,.')


def _value_with_unit(row):
    value = display_value(row)
    if row['data_state'] != 'value' or not row['unit']:
        return value
    return value + ('' if row['unit'] == '%' else ' ') + row['unit']


def _cell_sources(cell, rows):
    """Compact source references inside each editable content cell."""
    sources = OrderedDict()
    for row in rows:
        provenance = row.get('provenance') or {}
        key = (row['source_name'], row['source_url'], provenance.get('file', ''), provenance.get('sheet', ''), provenance.get('page', ''))
        sources.setdefault(key, []).append(row)
    for (name, url, filename, sheet, page), source_rows in sources.items():
        p = cell.add_paragraph()
        p.add_run('Источник: ')
        _add_hyperlink(p, name or url or 'Исходный файл', url)
        details = [str(value) for value in (filename, sheet, ('стр. ' + str(page)) if page else '') if value]
        cells = list(dict.fromkeys(str(row['provenance'].get('cell')) for row in source_rows if row['provenance'].get('cell')))
        if cells:
            details.append('ячейки ' + ', '.join(cells))
        if details:
            p.add_run(' · ' + ', '.join(details))


def _fill_content_cell(cell, rows, section_code):
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    cell.text = ''
    if not rows:
        cell.paragraphs[0].add_run('Данные отсутствуют')
        return
    rows = sorted(rows, key=_period_key)
    first = rows[0]
    from collections import Counter
    repeated = Counter((row['period_label'], row['answer']) for row in rows)
    title = cell.paragraphs[0]
    title.alignment = WD_ALIGN_PARAGRAPH.LEFT
    title.add_run(first['indicator_name']).bold = True
    context = [first['territory']]
    if first['group'] and first['group'].casefold() not in {'все', 'все респонденты', 'все участники'}:
        context.append(first['group'])
    cell.add_paragraph(' · '.join(context))
    if section_code == 'sociology':
        periods = OrderedDict()
        for row in rows:
            period_key = (row['period_label'], row.get('period_start'), row.get('period_end'))
            periods.setdefault(period_key, []).append(row)
        for (label, _, _), period_rows in periods.items():
            if label:
                cell.add_paragraph(label)
            for row in period_rows:
                answer = row['answer'] or 'Опубликованное значение'
                if repeated[(row['period_label'],row['answer'])]>1:answer += ' · '+(row['source_name'] or 'Источник')
                cell.add_paragraph(answer + ' — ' + _value_with_unit(row))
    elif section_code == 'fintest':
        for row in rows:
            denominator = row.get('denominator')
            if denominator in (None, ''):
                denominator = row.get('provenance', {}).get('answer_count', row.get('answer_count'))
            count = display_value({'value': denominator, 'data_state': 'value'}) if denominator not in (None, '') else 'количество не указано'
            label=row['period_label']
            if repeated[(label,row['answer'])]>1:label += ' · '+(row['source_name'] or 'Источник')
            cell.add_paragraph(f"{label} — {count} / {_value_with_unit(row)}")
    else:
        for row in rows:
            label = row['period_label'] or str(row.get('period_end') or '')
            if repeated[(row['period_label'],row['answer'])]>1:label += ' · '+(row['source_name'] or 'Источник')
            if row.get('answer'):
                label = row['answer'] + (' · ' + label if label else '')
            cell.add_paragraph(label + ' — ' + _value_with_unit(row))
    _cell_sources(cell, rows)


def _set_document_font(doc):
    from docx.shared import Pt
    from docx.oxml.ns import qn
    for style in doc.styles:
        if style.type == 1:
            style.font.name = 'Times New Roman'
            style.font.size = Pt(14)
            style.paragraph_format.space_after = Pt(3)
            style.paragraph_format.line_spacing = 1
            style._element.get_or_add_rPr().rFonts.set(qn('w:eastAsia'), 'Times New Roman')
    for p in doc.element.xpath('.//w:p'):
        for run in p.xpath('./w:r'):
            rpr = run.find(qn('w:rPr'))
            if rpr is None:
                from docx.oxml import OxmlElement
                rpr = OxmlElement('w:rPr'); run.insert(0, rpr)
            from docx.oxml import OxmlElement
            fonts = rpr.find(qn('w:rFonts'))
            if fonts is None:
                fonts = OxmlElement('w:rFonts'); rpr.append(fonts)
            for attr in ['ascii', 'hAnsi', 'cs', 'eastAsia']:
                fonts.set(qn('w:' + attr), 'Times New Roman')
            size = rpr.find(qn('w:sz'))
            if size is None:
                size = OxmlElement('w:sz'); rpr.append(size)
            size.set(qn('w:val'), '28')


def _table_header(row, titles):
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    for cell, title in zip(row.cells, titles):
        cell.text = title
        for p in cell.paragraphs:
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            for run in p.runs: run.bold = True
    # Repeated column headings remain visible after a page break.
    repeated = OxmlElement('w:tblHeader')
    row._tr.get_or_add_trPr().append(repeated)


def render_docx(rows, filters=None, metadata=None):
    """Mirror the supplied landscape, three-block monitoring report layout."""
    from docx import Document
    from docx.shared import Inches, Pt
    from docx.enum.section import WD_ORIENT
    from docx.enum.table import WD_TABLE_ALIGNMENT, WD_CELL_VERTICAL_ALIGNMENT
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from itertools import zip_longest

    rows = [normalize_row(row) for row in rows]
    filters, metadata = filters or {}, metadata or {}
    doc = Document()
    section = doc.sections[0]
    section.orientation = WD_ORIENT.LANDSCAPE
    # Page geometry and column proportions follow the supplied Word form.
    section.page_width, section.page_height = Inches(11.6930556), Inches(8.2680556)
    section.left_margin, section.right_margin = Inches(.4333333), Inches(.39375)
    section.top_margin, section.bottom_margin = Inches(.3944444), Inches(.39375)
    title = doc.add_paragraph('Синхронизация блока социологических исследований, блока статистических данных и вопросов Финансового теста в рамках мониторинга финансовой культуры')
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    title.runs[0].bold = True
    doc.add_paragraph('Мониторинг финансовой культуры · ' + str(metadata.get('period_label') or report_period_label(filters)))
    doc.add_paragraph('В рамках мониторинга финансового поведения и культуры используются показатели трёх блоков:')
    for text in [
        'Результаты социологических исследований: волны, вопросы и распределения ответов.',
        'Социально-экономические показатели выбранных территорий и периодов.',
        'Результаты Финансового теста: количество прохождений и опубликованная доля верных ответов.',
    ]:
        doc.add_paragraph(text, style='List Number')
    doc.add_paragraph(_filters_description(filters))

    table = doc.add_table(rows=0, cols=3)
    table.style = 'Table Grid'
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = False
    # Keep the submitted layout's 3.285 / 4.260 / 3.378 proportions while
    # fitting exactly within printable A4 landscape width.
    ratios = [3.2854167, 4.2604167, 3.3777778]
    available = section.page_width - section.left_margin - section.right_margin
    widths = [int(available * ratio / sum(ratios)) for ratio in ratios]
    for column, width in zip(table.columns, widths): column.width = width
    headers = [
        'Блок социологических исследований (вопросы)',
        'Блок статистических данных (показатели)',
        'Финансовый тест (вопросы)\n(количество прохождений / % верных ответов)',
    ]
    by_topic = _topic_rows(rows)
    for topic_name, topic_rows in by_topic.items():
        topic_row = table.add_row()
        merged = topic_row.cells[0].merge(topic_row.cells[2])
        merged.text = topic_name.upper()
        merged.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
        for p in merged.paragraphs:
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            for run in p.runs: run.bold = True
        _table_header(table.add_row(), headers)
        columns = [_indicator_groups([r for r in topic_rows if r['section'] == code]) for code in ['sociology', 'statistics', 'fintest']]
        entries = list(zip_longest(*columns, fillvalue=None)) or [(None, None, None)]
        for groups in entries:
            content_row = table.add_row()
            for index, (cell, group, code) in enumerate(zip(content_row.cells, groups, ['sociology', 'statistics', 'fintest'])):
                cell.width = widths[index]
                cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.TOP
                _fill_content_cell(cell, group or [], code)
    # The reporting form remains a single editable 3-column table. Sections not
    # present in the supplied form are appended without changing those columns.
    extra = [r for r in rows if r['section'] in {'irfk', 'quiz'}]
    if extra:
        doc.add_heading('Дополнительные разделы', 1)
        for code in ['irfk', 'quiz']:
            section_rows = [r for r in extra if r['section'] == code]
            if not section_rows: continue
            doc.add_heading(SECTION_NAMES[code], 2)
            for group in _indicator_groups(section_rows):
                first = group[0]
                doc.add_paragraph(first['indicator_name'], style='Heading 3')
                for row in sorted(group, key=_period_key):
                    doc.add_paragraph(f"{row['period_label']} · {row['territory']} — {_value_with_unit(row)}")
                    p = doc.add_paragraph()
                    _add_hyperlink(p, row['source_name'] or row['source_url'] or 'Исходный файл', row['source_url'])

    chart_specs = []
    from .charts import build_chart_specs
    for topic_name, topic_rows in by_topic.items():
        per_section = {}
        for spec in build_chart_specs(topic_rows, chart_type=filters.get('chart_type', 'auto')):
            spec_rows = spec.get('rows') or []
            code = spec_rows[0]['section'] if spec_rows else ''
            if per_section.get(code, 0) >= MAX_CHARTS_PER_TOPIC_SECTION: continue
            per_section[code] = per_section.get(code, 0) + 1
            chart_specs.append((topic_name, spec_rows))
    if chart_specs:
        doc.add_page_break()
        doc.add_heading('Приложение. Диаграммы', 1)
        doc.add_paragraph('Для обзорного приложения показано до двух диаграмм в каждом тематическом блоке. Полный выбранный набор данных включён в основную таблицу и Excel.')
        for topic_name, chart_rows in chart_specs:
            doc.add_heading(topic_name, 2)
            png=_chart_png(chart_rows,chart_type=filters.get("chart_type","auto"))
            from docx.image.image import Image
            dimensions=Image.from_blob(png)
            fit_width=min(9.8,5.9*dimensions.px_width/dimensions.px_height)
            doc.add_picture(io.BytesIO(png),width=Inches(fit_width))

    doc.add_heading('Источники и версии', 1)
    sources = OrderedDict()
    for row in rows:
        key = (row['source_name'], row['source_url'], source_location(row))
        sources.setdefault(key, row)
    for row in sources.values():
        p = doc.add_paragraph()
        _add_hyperlink(p, row['source_name'] or row['source_url'] or 'Исходный файл', row['source_url'])
        if source_location(row): p.add_run(' · ' + source_location(row))
    # Source conflicts remain inspectable without workflow-status badges in the
    # reporting table. No competing value is selected or recalculated here.
    conflict_notes = OrderedDict()
    for row in rows:
        if row.get('status') == 'conflict' or row.get('correction_conflict'):
            for note in row.get('issues', []):
                clean = _plain_note(note)
                if clean: conflict_notes.setdefault((row['indicator_name'], clean), None)
    if conflict_notes:
        doc.add_heading('Примечания к источникам', 2)
        for (name, note) in conflict_notes:
            doc.add_paragraph(name + ': ' + note)
    doc.add_paragraph('Версии наблюдений: ' + ', '.join(str(v) for v in metadata.get('data_version_ids', []) if v != ''))
    doc.add_paragraph('Версии методики: ' + json.dumps(metadata.get('method_versions', []), ensure_ascii=False, default=_json_value))
    _set_document_font(doc)
    # No cell shading is introduced; topic and block headers use bold text.
    for shade in doc.element.xpath('.//w:shd'): shade.getparent().remove(shade)
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
    headers = ["ID", "Код", "Показатель", "Раздел", "Темы", "Значение", "Состояние", "Единица", "Территория", "Начало периода", "Конец периода", "Период", "Тип периода", "Группа", "Вариант ответа", "Числитель", "Знаменатель", "База расчёта", "Источник", "Ссылка", "Файл / лист / ячейка", "Дата публикации", "Дата загрузки", "Версия данных", "Версия методики", "Сведения о значении", "Примечания", "Ручная поправка"]
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
            row["observation_version_id"], row["method_version_id"], "Расхождение источников" if row.get("status") == "conflict" or row.get("correction_conflict") else "",
            "; ".join(_plain_note(issue) for issue in row.get("issues", []) if _plain_note(issue)), str(row.get("correction_id") or row.get("manual_correction") or ""),
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
    if filters.get("period_mode") in {"quarter","year"} or filters.get("period_year") or filters.get("year"):
        from .periods import period_label_for_filters
        return period_label_for_filters(filters)
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
