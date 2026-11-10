from collections import defaultdict
from datetime import date
from decimal import Decimal
from django.db.models import Q,Prefetch
from monitoring.models import Observation,ManualCorrection,Source,Indicator,DataIssue,Methodology
from .periods import normalize_period_filters,matches_period
from .charts import build_chart_specs
from .presentation import clean_public_data


def display_number(value):
    if value is None:return 'Нет данных'
    text=format(Decimal(str(value)).normalize(),'f')
    whole,dot,fraction=text.partition('.')
    negative=whole.startswith('-')
    whole=f'{abs(int(whole)):,}'.replace(',',' ')
    if negative:whole='-'+whole
    return whole+(','+fraction if dot else '')


def filtered_observations(filters,section=None):
    filters=normalize_period_filters(filters)
    qs=Observation.objects.filter(indicator__is_active=True).select_related('indicator','current_version__publication__source','current_version__methodology','question_version','wave').prefetch_related('indicator__topics',Prefetch('corrections',queryset=ManualCorrection.objects.filter(active=True)))
    section=section or filters.get('section')
    if section:qs=qs.filter(indicator__section=section)
    if filters.get('topic'): qs=qs.filter(indicator__topics__slug=filters['topic'])
    if filters.get('territory'):qs=qs.filter(territory=filters['territory'])
    if filters.get('indicator'):qs=qs.filter(indicator__code=filters['indicator'])
    if filters.get('q'):qs=qs.filter(Q(indicator__name__icontains=filters['q'])|Q(answer__icontains=filters['q'])|Q(indicator__code__icontains=filters['q']))
    rows=[]
    for obs in qs.distinct():
        if not matches_period(obs,filters):continue
        v=obs.current_version
        if not v:continue
        corrections=list(obs.corrections.all());c=corrections[0] if corrections else None
        value=c.value if c else v.value
        state=c.data_state if c else v.data_state
        publication=v.publication
        provenance=v.provenance or {}
        source_url=provenance.get('url') or (publication.url if publication else '') or (publication.source.url if publication else '')
        source_url=source_url if str(source_url).startswith(('https://','http://')) else ''
        source_name=publication.source.name if publication else provenance.get('label') or 'Ручная запись'
        source_detail=' · '.join(str(provenance.get(x)) for x in ['file','sheet','cell','page'] if provenance.get(x))
        status='conflict' if c and c.conflict else v.status
        topics=[{'slug':t.slug,'name':t.name} for t in obs.indicator.topics.all()]
        rows.append({'id':obs.pk,'indicator_id':obs.indicator_id,'indicator_code':obs.indicator.code,'indicator_name':obs.indicator.name,'section':obs.indicator.section,'section_label':obs.indicator.get_section_display(),'topics':topics,'topics_display':', '.join(t['name'] for t in topics),'value':str(value) if value is not None else None,'original_value':str(v.value) if v.value is not None else None,'value_display':display_number(value) if state=='value' else ('Неприменимо' if state=='na' else 'Нет данных'),'data_state':state,'unit':v.unit,'territory':obs.territory,'period_start':obs.period_start.isoformat() if obs.period_start else None,'period_end':obs.period_end.isoformat() if obs.period_end else None,'period_label':obs.period_label,'period_kind':obs.period_kind,'period_kind_label':obs.get_period_kind_display(),'group':obs.group,'answer':obs.answer,'numerator':str(v.numerator) if v.numerator is not None and not c else None,'denominator':str(v.denominator) if v.denominator is not None and not c else None,'base':v.base,'source_name':source_name,'source_url':source_url,'source_detail':source_detail,'publication_id':publication.pk if publication else None,'publication_date':(v.publication_date or (publication.published_at if publication else None)).isoformat() if (v.publication_date or (publication.published_at if publication else None)) else None,'loaded_at':v.created_at.isoformat(),'last_checked':publication.source.last_checked.isoformat() if publication and publication.source.last_checked else None,'provenance':provenance,'status':status,'status_label':{'pending':'Исходные данные','verified':'Проверено','conflict':'Расхождение'}[status],'status_class':{'pending':'warning','verified':'success','conflict':'danger'}[status],'has_correction':bool(c),'correction_id':c.pk if c else None,'correction_reason':c.reason if c else '', 'correction_conflict':c.conflict if c else False,'version_id':v.pk,'observation_version_id':v.pk,'version_number':v.number,'method_version_id':v.methodology_id,'method':v.get_method_display(),'issues':v.issues,'question_version_id':obs.question_version_id,'wave_id':obs.wave_id})
    return clean_public_data(rows)


def weighted_accuracy(rows):
    usable=[r for r in rows if r['section']=='fintest' and r.get('data_state')=='value']
    if not usable:return {'value':None,'limitation':'Нет результатов финтеста для выбранных фильтров.'}
    if any(r.get('status')=='conflict' or r.get('correction_conflict') for r in usable):return {'value':None,'limitation':'Сначала разрешите расхождения: конфликтующие результаты не агрегируются.'}
    if len({r['territory'] for r in usable})>1:return {'value':None,'limitation':'Для сводной доли выберите одну территорию: пересечение совокупностей не подтверждено.'}
    if len({r['group'] for r in usable})>1:return {'value':None,'limitation':'Для сводной доли выберите одну группу респондентов.'}
    question_versions=defaultdict(set)
    for r in usable:question_versions[r['indicator_code']].add(r.get('question_version_id'))
    if any(len(v)>1 for v in question_versions.values()):return {'value':None,'limitation':'Разные версии одного вопроса не объединяются без подтверждения сопоставимости.'}
    if any(r.get('numerator') is None or r.get('denominator') is None for r in usable):
        return {'value':None,'limitation':'Сводная точная доля недоступна: нужны исходные количества правильных ответов и всех ответов. Опубликованные округлённые проценты не восстанавливают числитель.'}
    dimensions=defaultdict(list)
    for r in usable:dimensions[(r['indicator_code'],r.get('question_version_id'),r['territory'],r['group'],r['answer'])].append(r)
    for group in dimensions.values():
        periods=sorted([(r['period_start'],r['period_end']) for r in group],key=str)
        if any(not a or not b for a,b in periods):return {'value':None,'limitation':'Периоды требуют уточнения.'}
        for (a,b),(c,d) in zip(periods,periods[1:]):
            if c<=b:return {'value':None,'limitation':'Пересекающиеся периоды одного вопроса не суммируются.'}
    den=sum(Decimal(r['denominator']) for r in usable)
    if den<=0:return {'value':None,'limitation':'Нулевой знаменатель: сводная доля неприменима.'}
    result=sum(Decimal(r['numerator']) for r in usable)/den*100
    return {'value':str(result),'display':display_number(result),'numerator':str(sum(Decimal(r['numerator']) for r in usable)),'denominator':str(den),'limitation':''}


def dashboard_payload(filters,section=None):
    filters=normalize_period_filters(filters)
    rows=filtered_observations(filters,section)
    available=build_chart_specs(rows)
    # Preview is bounded; the constructor can display every chart in the selection.
    charts=[];counts=defaultdict(int)
    for spec in available:
        if counts[spec['section']]>=2:continue
        charts.append({k:v for k,v in spec.items() if k!='rows'});counts[spec['section']]+=1
    warnings=[filters['_period_error']] if filters.get('_period_error') else []
    if section=='irfk' and not Methodology.objects.filter(approved=True,code='irfk').exists():warnings.append('Полный ИРФК не рассчитан: необходимы согласованная версия методики и полный набор компонентов.')
    return {'rows':rows,'stats':{'indicators':len(set(r['indicator_code'] for r in rows)),'observations':len(rows),'sources':len(set(r['source_name'] for r in rows))},'charts':charts,'charts_total':len(available),'warnings':warnings,'accuracy':weighted_accuracy(rows),'limitation':'Возрастные срезы появятся при загрузке соответствующих групп респондентов.' if section=='sociology' and not any('возраст' in r['group'].casefold() for r in rows) else ''}
