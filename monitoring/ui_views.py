"""Authenticated Russian workspace; observation writes go through domain services."""
import json
from urllib.parse import urlencode
from functools import wraps
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db.models import Count
from django.http import JsonResponse
from .services.periods import normalize_period_filters,classify_period
from .services.presentation import clean_public_data,clean_public_text
from .services.charts import build_chart_specs
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST
from .models import (Indicator, Observation, Source, Topic, TopicAlias, ImportBatch, LoadRun, DataIssue, ReportSnapshot, SECTIONS)
from .forms import (IndicatorForm, ObservationForm, SourceForm, CorrectionForm, ResolutionForm, ImportForm, TopicForm, TopicAliasForm, GptForm)
from .services.observations import require_editor, upsert_observation, apply_correction, resolve_correction
from .services.selectors import filtered_observations, dashboard_payload

SECTION_DESCRIPTIONS = {
    'sociology': 'Результаты исследований, распределения ответов и волны опросов.',
    'fintest': 'Опубликованные результаты по вопросам, темам и сопоставимым периодам.',
    'statistics': 'Официальная статистика: динамика, периоды и исходные публикации.',
    'irfk': 'Компоненты индекса и версии методики расчёта финансовой культуры.',
    'quiz': 'Результаты онлайн-зачёта по финансовой грамотности.',
}


def viewer_required(view):
    @wraps(view)
    @login_required
    def wrapped(request, *args, **kwargs):
        if not request.user.is_superuser and not request.user.has_perm('monitoring.view_observation'):
            raise PermissionDenied('Для доступа нужна роль просмотра, редактирования или администрирования.')
        return view(request, *args, **kwargs)
    return wrapped


def editor_required(view):
    @wraps(view)
    @login_required
    def wrapped(request, *args, **kwargs):
        require_editor(request.user)
        return view(request, *args, **kwargs)
    return wrapped


def _permission(request, name):
    if not request.user.is_superuser and not request.user.has_perm('monitoring.' + name):
        raise PermissionDenied('Недостаточно прав для изменения этого справочника.')


def _filters(data):
    allowed = ['q','topic','territory','start','end','section','indicator','period_mode','period_year','period_quarter','period','year','quarter']
    return normalize_period_filters({key:str(data.get(key,'')).strip() for key in allowed if str(data.get(key,'')).strip()})


def _filter_context(filters):
    years=set(range(2024,timezone.localdate().year+2))
    for period in Observation.objects.filter(indicator__is_active=True).values('period_label','period_start','period_end','period_kind').distinct():
        year=classify_period(period).get('year')
        if year:years.add(year)
    return {'filters':filters,'has_filters':any(v and not (k=='period_mode' and v=='all') for k,v in filters.items()),'topics':Topic.objects.all(),'period_years':sorted(years,reverse=True),'territories':Observation.objects.filter(indicator__is_active=True).values_list('territory',flat=True).distinct().order_by('territory')}


def _error_text(error):
    if isinstance(error, ValidationError):
        return '; '.join(error.messages)
    return str(error)


@viewer_required
def dashboard(request, section=None):
    if section and section not in dict(SECTIONS):
        from django.http import Http404
        raise Http404('Раздел не найден.')
    filters = _filters(request.GET)
    if section:filters['section']=section
    payload = dashboard_payload(filters, section)
    rows = payload.get('rows')
    if rows is None:
        rows = filtered_observations(filters, section)
    page = Paginator(rows, 50).get_page(request.GET.get('page'))
    query = request.GET.copy()
    query.pop('page', None)
    context = {**payload, **_filter_context(filters), 'active': section or 'dashboard', 'page_title': dict(SECTIONS).get(section, 'Обзор мониторинга'), 'page_description': SECTION_DESCRIPTIONS.get(section, 'Данные, источники и квартальные отчёты в одном рабочем пространстве.'), 'rows': list(page.object_list), 'page_obj': page, 'pagination_query':query.urlencode(),'report_query':urlencode({k:v for k,v in filters.items() if not k.startswith('_')}),'rows_limited':False}
    if section == 'fintest':
        accuracy = payload.get('accuracy', {})
        context['limitation'] = accuracy.get('limitation') or ('Сводная доля правильных ответов: ' + str(accuracy.get('display', '')) + ' %. Расчёт по точным числителям и знаменателям.')
    if section == 'quiz' and not rows:
        context['limitation'] = 'Источник онлайн-зачёта пока не предоставлен. Добавьте опубликованные результаты вручную или импортируйте согласованную выгрузку.'
    return render(request, 'monitoring/dashboard.html', context)


@viewer_required
def observation_detail(request, pk):
    obs = get_object_or_404(Observation.objects.select_related('indicator', 'current_version__publication__source', 'question_version', 'wave__study'), pk=pk,indicator__is_active=True)
    if request.method == 'POST':
        require_editor(request.user)
    correction_form = CorrectionForm(auto_id="correction_%s")
    resolution_form = ResolutionForm(auto_id="resolution_%s")
    active_correction = obs.corrections.filter(active=True).select_related('created_by', 'based_on').first()
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'correct':
            correction_form = CorrectionForm(request.POST, auto_id="correction_%s")
            if correction_form.is_valid():
                try:
                    apply_correction(obs, correction_form.cleaned_data['value'], correction_form.cleaned_data['data_state'], correction_form.cleaned_data['reason'], request.user)
                    from .services.calculations import recompute_formulas
                    recompute_formulas()
                    messages.success(request, 'Ручная поправка сохранена отдельно от исходного значения.')
                    return redirect('monitoring:observation_detail', pk=pk)
                except ValidationError as exc:
                    correction_form.add_error(None, _error_text(exc))
        elif action == 'resolve' and active_correction:
            resolution_form = ResolutionForm(request.POST, auto_id="resolution_%s")
            if resolution_form.is_valid():
                try:
                    resolve_correction(active_correction, resolution_form.cleaned_data['choice'], resolution_form.cleaned_data['reason'], request.user)
                    from .services.calculations import recompute_formulas
                    recompute_formulas()
                    messages.success(request, 'Выбор применяемого значения зафиксирован в истории.')
                    return redirect('monitoring:observation_detail', pk=pk)
                except ValidationError as exc:
                    resolution_form.add_error(None, _error_text(exc))
        elif action == 'review':
            from .services.observations import review_observation
            try:
                review_observation(obs, request.POST.get('reason', '').strip(), request.user)
                from .services.calculations import recompute_formulas
                recompute_formulas()
                messages.success(request, 'Проверка сохранена новой версией наблюдения.')
                return redirect('monitoring:observation_detail', pk=pk)
            except ValidationError as exc:
                messages.error(request, _error_text(exc))
        else:
            messages.error(request, 'Неизвестное действие с наблюдением.')
    rows = filtered_observations({'indicator': obs.indicator.code})
    row = next((r for r in rows if r['id'] == obs.pk), None)
    return render(request, 'monitoring/observation_detail.html', {'active': obs.indicator.section, 'observation': obs, 'row': row, 'versions': obs.versions.select_related('created_by', 'publication__source', 'methodology'), 'corrections': obs.corrections.select_related('created_by', 'resolved_by', 'based_on').order_by('-created_at'), 'active_correction': active_correction, 'correction_form': correction_form, 'resolution_form': resolution_form, 'provenance_text': json.dumps(clean_public_data(obs.current_version.provenance),ensure_ascii=False,indent=2) if obs.current_version else ''})


@editor_required
def observation_create(request):
    form = ObservationForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        r = dict(form.cleaned_data)
        r['indicator_code'] = r.pop('indicator').code
        question = r.pop('question_version', None)
        wave = r.pop('wave', None)
        if question:
            r['question_version_id'] = question.pk
        if wave:
            r['wave_id'] = wave.pk
        r['provenance'] = {'url': r.pop('source_url', ''), 'detail': r.pop('source_detail', ''), 'label': 'Ручной ввод'}
        r['status'] = 'pending'
        try:
            obs, changed = upsert_observation(r, user=request.user, method='manual')
            from .services.calculations import recompute_formulas
            recompute_formulas()
            messages.success(request, 'Наблюдение сохранено.' if changed else 'Такое наблюдение уже сохранено; дубль не создан.')
            return redirect('monitoring:observation_detail', pk=obs.pk)
        except (ValidationError, Indicator.DoesNotExist) as exc:
            form.add_error(None, _error_text(exc))
    return render(request, 'monitoring/form.html', {'form': form, 'page_title': 'Добавить наблюдение', 'page_description': 'Укажите опубликованное значение, период и источник. История изменений сохранится.', 'active': 'dashboard', 'back_url': reverse('monitoring:dashboard')})


@editor_required
def indicator_edit(request, pk=None):
    _permission(request, 'change_indicator' if pk else 'add_indicator')
    instance = get_object_or_404(Indicator,pk=pk,is_active=True) if pk else None
    form = IndicatorForm(request.POST or None, instance=instance)
    if instance and instance.observations.exists():
        form.fields['code'].disabled = True
        form.fields['code'].help_text = 'Код показателя с наблюдениями сохраняется постоянным.'
    if request.method == 'POST' and form.is_valid():
        indicator = form.save()
        messages.success(request, 'Паспорт показателя сохранён.')
        return redirect(reverse('monitoring:sources') + '?tab=indicators')
    return render(request, 'monitoring/form.html', {'form': form, 'page_title': 'Паспорт показателя' if pk else 'Новый показатель', 'page_description': 'Определение, единицы и тематические связи хранятся отдельно от значений.', 'active': 'sources', 'back_url': reverse('monitoring:sources') + '?tab=indicators'})


@viewer_required
def sources(request):
    tab = request.GET.get('tab', 'sources')
    if tab not in ['sources', 'indicators', 'topics', 'issues', 'logs']:
        tab = 'sources'
    from django.core.paginator import Paginator
    issue_qs=DataIssue.objects.select_related('indicator','batch').order_by('resolved','-pk')
    severity=request.GET.get('severity','')
    if severity in ['info','warning','error']:issue_qs=issue_qs.filter(severity=severity)
    issue_page=Paginator(issue_qs,50).get_page(request.GET.get('page'))
    for issue in issue_page.object_list:issue.description=clean_public_text(issue.description);issue.references=clean_public_data(issue.references)
    query=request.GET.copy();query.pop('page',None)
    context = {'active': 'sources', 'tab': tab, 'sources': Source.objects.all(), 'indicators': Indicator.objects.filter(is_active=True).select_related('source').prefetch_related('topics')[:1000], 'topics': Topic.objects.prefetch_related('aliases'), 'aliases': TopicAlias.objects.select_related('topic'), 'issues': issue_page, 'issue_page':issue_page, 'issue_query':query.urlencode(), 'severity':severity, 'runs': LoadRun.objects.select_related('source', 'requested_by')[:100], 'source_count': Source.objects.count(), 'issue_count': DataIssue.objects.filter(resolved=False).count()}
    return render(request, 'monitoring/sources.html', context)


@editor_required
def source_edit(request, pk=None):
    _permission(request, 'change_source' if pk else 'add_source')
    instance = get_object_or_404(Source, pk=pk) if pk else None
    form = SourceForm(request.POST or None, instance=instance)
    if request.method == 'POST' and form.is_valid():
        source = form.save(commit=False)
        if source.enabled and source.frequency != 'manual':
            source.next_due = timezone.now()
        else:
            source.next_due = None
        source.save()
        messages.success(request, 'Источник и параметры проверки сохранены.')
        return redirect('monitoring:sources')
    return render(request, 'monitoring/form.html', {'form': form, 'page_title': 'Настройка источника' if pk else 'Новый источник', 'page_description': 'Адрес, загрузчик и расписание проверки. Состояние загрузки доступно в журнале.', 'active': 'sources', 'back_url': reverse('monitoring:sources')})


@require_POST
@editor_required
def enqueue_source(request, pk):
    from .services.workers import enqueue_source as enqueue
    source = get_object_or_404(Source, pk=pk)
    try:
        run = enqueue(source, user=request.user)
        messages.success(request, 'Проверка добавлена в очередь. Результат появится в журнале обновлений.')
    except ValidationError as exc:
        messages.error(request, _error_text(exc))
    return redirect(reverse('monitoring:sources') + '?tab=logs')


@editor_required
def topic_edit(request, pk=None):
    _permission(request, 'change_topic' if pk else 'add_topic')
    form = TopicForm(request.POST or None, instance=get_object_or_404(Topic, pk=pk) if pk else None)
    if pk:
        form.fields['slug'].disabled = True
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, 'Тематическая рубрика сохранена.')
        return redirect(reverse('monitoring:sources') + '?tab=topics')
    return render(request, 'monitoring/form.html', {'form': form, 'page_title': 'Тематическая рубрика', 'active': 'sources', 'back_url': reverse('monitoring:sources') + '?tab=topics'})


@editor_required
def alias_create(request):
    _permission(request, 'add_topicalias')
    form = TopicAliasForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, 'Вариант названия сопоставлен с канонической темой.')
        return redirect(reverse('monitoring:sources') + '?tab=topics')
    return render(request, 'monitoring/form.html', {'form': form, 'page_title': 'Соответствие названий тем', 'active': 'sources', 'back_url': reverse('monitoring:sources') + '?tab=topics'})


@require_POST
@editor_required
def resolve_issue(request, pk):
    issue = get_object_or_404(DataIssue, pk=pk)
    reason = request.POST.get('resolution', '').strip()
    if not reason:
        messages.error(request, 'Для закрытия вопроса необходимо обоснование.')
    else:
        issue.resolved = True
        issue.resolution = f'{request.user.username}, {timezone.localtime().isoformat()}: {reason}'
        issue.save(update_fields=['resolved', 'resolution'])
        messages.success(request, 'Решение по вопросу проверки сохранено. Значения наблюдений не изменены.')
    return redirect(reverse('monitoring:sources') + '?tab=issues')


@viewer_required
def imports(request):
    form = ImportForm(request.POST or None, request.FILES or None)
    if request.method == 'POST':
        require_editor(request.user)
        if form.is_valid():
            from .services.importer import create_preview
            try:
                kind = form.cleaned_data['kind']
                batch = create_preview(form.cleaned_data['file'], request.user, kind='registry' if kind == 'registry' else 'generic', source=form.cleaned_data['source'] if kind == 'source' else None)
                return redirect('monitoring:import_detail', pk=batch.pk)
            except ValidationError as exc:
                form.add_error(None, _error_text(exc))
    return render(request, 'monitoring/imports.html', {'active': 'imports', 'form': form, 'batches': ImportBatch.objects.select_related('source', 'created_by')[:60]})


@viewer_required
def import_detail(request, pk):
    batch = get_object_or_404(ImportBatch.objects.select_related('source', 'created_by', 'publication'), pk=pk)
    if request.method == 'POST':
        require_editor(request.user)
        if request.POST.get('action') == 'confirm':
            from .services.importer import confirm_batch
            try:
                result = confirm_batch(batch, request.user)
                messages.success(request, f'Импорт завершён. Создано версий: {result.get("versions_created", 0)}; без изменений: {result.get("unchanged", 0)}.')
                return redirect('monitoring:import_detail', pk=pk)
            except ValidationError as exc:
                messages.error(request, _error_text(exc))
    preview = batch.preview or {}
    preview=clean_public_data(preview)
    observations = preview.get('observations', [])
    indicators = {i.get('key') or i.get('code'): i.get('name') for i in preview.get('indicators', [])}
    names = dict(Indicator.objects.filter(is_active=True).values_list('code','name'))
    rows = []
    from django.core.paginator import Paginator
    preview_page=Paginator(observations,100).get_page(request.GET.get('page'))
    for i, raw in enumerate(preview_page, preview_page.start_index()):
        row = dict(raw)
        code = row.get('indicator_code') or row.get('indicator_key')
        row['indicator_name'] = indicators.get(code) or names.get(code) or code or 'Не сопоставлен'
        row['number'] = i
        row['source_display'] = json.dumps(row.get('source') or row.get('provenance') or {}, ensure_ascii=False)
        rows.append(row)
    return render(request, 'monitoring/import_detail.html', {'active': 'gpt' if batch.kind == 'gpt' else 'imports', 'batch': batch, 'preview': preview, 'preview_rows': rows, 'observations_count': len(observations), 'preview_page':preview_page, 'rows_limited':False, 'issue_total':len(preview.get('issues',[])), 'issues': preview.get('issues', [])[:100], 'errors': preview.get('errors', [])})


@viewer_required
def reports(request):
    filters = _filters(request.POST if request.method == 'POST' else request.GET)
    rows = filtered_observations(filters)
    if request.method == 'POST':
        require_editor(request.user)
        from .services.exports import generate_report
        if not rows:
            messages.error(request, 'Для выбранных фильтров нет наблюдений. Измените срез перед выпуском отчёта.')
        else:
            try:
                snapshot = generate_report(rows, filters, author=request.user)
                messages.success(request, 'Версия отчёта сохранена. Word и Excel содержат зафиксированный срез данных.')
                return redirect('monitoring:reports')
            except (ValidationError, ValueError) as exc:
                messages.error(request, _error_text(exc))
    return render(request, 'monitoring/reports.html', {'active': 'reports', **_filter_context(filters), 'rows_count': len(rows), 'reports': ReportSnapshot.objects.select_related('created_by')[:60]})


@viewer_required
def gpt(request):
    configured = bool(settings.OPENAI_API_KEY)
    form = GptForm(request.POST or None, request.FILES or None)
    if request.method == 'POST':
        require_editor(request.user)
        if not configured:
            form.add_error(None, 'Для извлечения администратор должен задать OPENAI_API_KEY и OPENAI_MODEL на сервере.')
        elif form.is_valid():
            from .services.gpt import extract_text, extract_records, ExtractionError, ConfigurationError
            from .services.importer import create_gpt_preview
            try:
                text = form.cleaned_data.get('text') or ''
                uploaded = form.cleaned_data.get('file')
                if uploaded:
                    text = extract_text(uploaded) + ('\n\nДополнительный текст:\n' + text if text else '')
                catalog = Indicator.objects.filter(is_active=True)
                if form.cleaned_data.get('section'):
                    catalog = catalog.filter(section=form.cleaned_data['section'])
                proposals = extract_records(text, form.cleaned_data['instruction'], catalog)
                batch = create_gpt_preview(proposals, request.user, file=uploaded)
                messages.info(request, 'Предложения извлечены. Проверьте каждую запись и вопросы перед подтверждением. В базу наблюдений пока ничего не записано.')
                return redirect('monitoring:import_detail', pk=batch.pk)
            except (ExtractionError, ConfigurationError, ValidationError) as exc:
                form.add_error(None, _error_text(exc))
    return render(request, 'monitoring/gpt.html', {'active': 'gpt', 'configured': configured, 'form': form, 'model_name': settings.OPENAI_MODEL if configured else ''})


def csrf_failure(request, reason=''):
    return render(request, 'monitoring/csrf_error.html', status=403)


@viewer_required
def chart_data(request):
    filters=_filters(request.GET)
    rows=filtered_observations(filters)
    raw_ids=request.GET.get('ids','')
    if raw_ids:
        try:
            selected={int(x) for x in raw_ids.split(',') if x}
            if len(selected)>10000:raise ValueError()
        except ValueError:return JsonResponse({'error':'Некорректный список наблюдений.'},status=400)
        rows=[r for r in rows if r['id'] in selected]
    specs=build_chart_specs(rows,request.GET.get('chart_kind','auto'))
    return JsonResponse({'charts':[{k:v for k,v in s.items() if k!='rows'} for s in specs],'observations_count':len(rows),'chart_count':len(specs),'message':'' if specs else 'В этой выборке нет числовых значений для диаграммы.'},json_dumps_params={'ensure_ascii':False})


@editor_required
def indicator_delete(request,pk):
    _permission(request,'delete_indicator')
    indicator=get_object_or_404(Indicator,pk=pk,is_active=True)
    if request.method=='POST':
        indicator.is_active=False;indicator.deleted_at=timezone.now();indicator.deleted_by=request.user
        indicator.save(update_fields=['is_active','deleted_at','deleted_by'])
        from .services.calculations import recompute_formulas
        recompute_formulas()
        messages.success(request,'Показатель удалён из реестра.')
        return redirect(reverse('monitoring:sources')+'?tab=indicators')
    return render(request,'monitoring/indicator_confirm_delete.html',{'active':'sources','indicator':indicator,'observations_count':indicator.observations.count()})
