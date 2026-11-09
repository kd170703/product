from collections import defaultdict
from datetime import date
from decimal import Decimal
from django.db.models import Q,Prefetch
from monitoring.models import Observation,ManualCorrection,Source,Indicator,DataIssue,Methodology


def display_number(value):
    if value is None:return 'Нет данных'
    text=format(Decimal(str(value)).normalize(),'f')
    whole,dot,fraction=text.partition('.')
    negative=whole.startswith('-')
    whole=f'{abs(int(whole)):,}'.replace(',',' ')
    if negative:whole='-'+whole
    return whole+(','+fraction if dot else '')


def filtered_observations(filters,section=None):
    qs=Observation.objects.select_related('indicator','current_version__publication__source','current_version__methodology','question_version','wave').prefetch_related('indicator__topics',Prefetch('corrections',queryset=ManualCorrection.objects.filter(active=True)))
    section=section or filters.get('section')
    if section:qs=qs.filter(indicator__section=section)
    if filters.get('topic'): qs=qs.filter(indicator__topics__slug=filters['topic'])
    if filters.get('territory'):qs=qs.filter(territory=filters['territory'])
    if filters.get('indicator'):qs=qs.filter(indicator__code=filters['indicator'])
    if filters.get('q'):qs=qs.filter(Q(indicator__name__icontains=filters['q'])|Q(answer__icontains=filters['q'])|Q(indicator__code__icontains=filters['q']))
    # Only full periods inside the selected range. Stock observations use their date.
    for field,lookup in [('start','gte'),('end','lte')]:
        raw=filters.get(field)
        if raw:
            try: d=date.fromisoformat(raw)
            except (ValueError,TypeError):continue
            if field=='start':qs=qs.filter(Q(period_start__gte=d)|Q(period_start__isnull=True,period_end__gte=d))
            else:qs=qs.filter(period_end__lte=d)
    rows=[]
    for obs in qs.distinct():
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
        rows.append({'id':obs.pk,'indicator_id':obs.indicator_id,'indicator_code':obs.indicator.code,'indicator_name':obs.indicator.name,'section':obs.indicator.section,'section_label':obs.indicator.get_section_display(),'topics':topics,'topics_display':', '.join(t['name'] for t in topics),'value':str(value) if value is not None else None,'original_value':str(v.value) if v.value is not None else None,'value_display':display_number(value) if state=='value' else ('Неприменимо' if state=='na' else 'Нет данных'),'data_state':state,'unit':v.unit,'territory':obs.territory,'period_start':obs.period_start.isoformat() if obs.period_start else None,'period_end':obs.period_end.isoformat() if obs.period_end else None,'period_label':obs.period_label,'period_kind':obs.period_kind,'period_kind_label':obs.get_period_kind_display(),'group':obs.group,'answer':obs.answer,'numerator':str(v.numerator) if v.numerator is not None and not c else None,'denominator':str(v.denominator) if v.denominator is not None and not c else None,'base':v.base,'source_name':source_name,'source_url':source_url,'source_detail':source_detail,'publication_id':publication.pk if publication else None,'publication_date':(v.publication_date or (publication.published_at if publication else None)).isoformat() if (v.publication_date or (publication.published_at if publication else None)) else None,'loaded_at':v.created_at.isoformat(),'last_checked':publication.source.last_checked.isoformat() if publication and publication.source.last_checked else None,'provenance':provenance,'status':status,'status_label':{'pending':'Требует проверки','verified':'Проверено','conflict':'Расхождение'}[status],'status_class':{'pending':'warning','verified':'success','conflict':'danger'}[status],'has_correction':bool(c),'correction_id':c.pk if c else None,'correction_reason':c.reason if c else '', 'correction_conflict':c.conflict if c else False,'version_id':v.pk,'observation_version_id':v.pk,'version_number':v.number,'method_version_id':v.methodology_id,'method':v.get_method_display(),'issues':v.issues,'question_version_id':obs.question_version_id,'wave_id':obs.wave_id})
    return rows


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
    rows=filtered_observations(filters,section)
    charts=[];groups=defaultdict(list)
    for r in rows:
        if r['data_state']!='value' or r['status']=='conflict':continue
        # Unit conflicts and unknown dates cannot be shown as comparable time series.
        if r['section']=='statistics' and (not r['period_end'] or r['unit']=='Не уточнено' or (r['status']!='verified' and any('единиц' in x.casefold() for x in r['issues']))):continue
        key=(r['indicator_code'],r['unit'],r['territory'],r['group'],r['period_kind'],r.get('question_version_id'))
        groups[key].append(r)
    # Give each section space; one chart always contains a single indicator and unit.
    counts=defaultdict(int)
    for key,series in groups.items():
        cat=series[0]['section']
        if counts[cat]>=3 or len(charts)>=9:continue
        if cat=='sociology':
            # A distribution is one wave, no normalized pie for multiple choice.
            period=max((r['period_label'] for r in series),default='')
            points=[r for r in series if r['period_label']==period][:18]
            labels=[r['answer'] or r['indicator_name'] for r in points];kind='bar'
        else:
            answer=series[0]['answer']
            points=sorted([r for r in series if r['answer']==answer],key=lambda x:(x['period_end'] or x['period_label']))
            period='; '.join(dict.fromkeys(r['period_label'] for r in points));labels=[r['period_label'] for r in points];kind='line' if len(points)>1 else 'bar'
        if not points:continue
        first=points[0]
        charts.append({'title':first['indicator_name'],'kind':kind,'unit':first['unit'],'territory':first['territory'],'period':period,'source_name':first['source_name'],'source_url':first['source_url'],'labels':labels,'values':[float(r['value']) for r in points],'notes':'Опубликованные значения. База и ограничения доступны в карточке наблюдения.' if any(r['issues'] for r in points) else ''})
        counts[cat]+=1
    warnings=[]
    pending=sum(r['status']!='verified' for r in rows)
    if pending:warnings.append(f'{pending} наблюдений требуют проверки. Ограничения и исходные значения доступны в карточках.')
    if section=='irfk' and not Methodology.objects.filter(approved=True,code='irfk').exists():warnings.append('Полный ИРФК не рассчитан: необходимы согласованная версия методики и полный набор компонентов. Проекты и правки методики не считаются утверждением.')
    return {'rows':rows,'stats':{'indicators':len(set(r['indicator_code'] for r in rows)),'observations':len(rows),'review':pending,'sources':len(set(r['source_name'] for r in rows))},'charts':charts,'warnings':warnings,'accuracy':weighted_accuracy(rows),'limitation':'Возрастные срезы появятся при загрузке соответствующих групп респондентов.' if section=='sociology' and not any('возраст' in r['group'].casefold() for r in rows) else ''}
