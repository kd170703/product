"""Расчёты только по согласованным формулам; Python eval не используется."""
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from monitoring.models import Formula
from .selectors import filtered_observations
from .observations import upsert_observation


def calculate_expression(expression,components):
    op=expression.get('operation')
    multiplier=Decimal(str(expression.get('multiplier','1')))
    if op=='sum':return sum(Decimal(c['value']) for c in components)*multiplier
    if op=='ratio':
        if len(components)!=2:raise ValueError('Отношение требует ровно два компонента.')
        den=Decimal(components[1]['value'])
        if den==0:raise ValueError('Нулевой знаменатель.')
        return Decimal(components[0]['value'])/den*multiplier
    if op=='weighted_ratio':
        if any(c.get('numerator') is None or c.get('denominator') is None for c in components):raise ValueError('Нет точных числителей или знаменателей.')
        den=sum(Decimal(c['denominator']) for c in components)
        if den<=0:raise ValueError('Нулевой знаменатель.')
        return sum(Decimal(c['numerator']) for c in components)/den*multiplier
    raise ValueError('Неизвестная операция. Поддерживаются sum, ratio, weighted_ratio.')


def _dimension(row):
    return (row['territory'],row['period_start'],row['period_end'],row['period_kind'],row['group'],row['answer'])


def recompute_formulas():
    result={'versions_created':0,'limitations':[]}
    latest={}
    for f in Formula.objects.filter(methodology__approved=True).select_related('indicator','methodology').order_by('indicator_id','-methodology__version'):
        latest.setdefault(f.indicator.code,f)
    # A dependency is computed before formulas that consume it. Cycles are never evaluated.
    ordered=[];visiting=set();done=set()
    def visit(code):
        if code in done:return
        if code in visiting:raise ValueError('Циклическая зависимость формул: '+code)
        visiting.add(code)
        for dep in latest[code].expression.get('components',[]):
            if dep in latest:visit(dep)
        visiting.remove(code);done.add(code);ordered.append(latest[code])
    try:
        for code in latest:visit(code)
    except ValueError as exc:
        result['limitations'].append(str(exc));return result
    for f in ordered:
        codes=f.expression.get('components',[])
        if not codes or len(codes)!=len(set(codes)):
            result['limitations'].append(f'{f.indicator.code}: не определены независимые компоненты.');continue
        groups=defaultdict(lambda:defaultdict(list));labels={}
        for row in filtered_observations({}):
            if row['indicator_code'] in codes:
                key=_dimension(row);groups[key][row['indicator_code']].append(row);labels[key]=row['period_label']
            elif row['indicator_code']==f.indicator.code and row.get('provenance',{}).get('formula_id'):
                key=_dimension(row);groups[key];labels[key]=row['period_label']
        if not groups:result['limitations'].append(f'{f.indicator.code}: нет проверенных компонентов.')
        for key,by_code in groups.items():
            reasons=[]
            if any(len(by_code.get(c,[]))!=1 for c in codes):reasons.append('Неполный или неоднозначный набор компонентов.')
            components=[by_code[c][0] for c in codes if len(by_code.get(c,[]))==1]
            if any(c['status']!='verified' or c['data_state']!='value' or c.get('correction_conflict') for c in components):reasons.append('Есть непроверенные, отсутствующие или конфликтующие компоненты.')
            value=None
            if not reasons:
                try:value=calculate_expression(f.expression,components)
                except (ValueError,InvalidOperation,KeyError) as exc:reasons.append(str(exc))
            if reasons:result['limitations'].append(f'{f.indicator.code}: {" ".join(reasons)}')
            record={'indicator_code':f.indicator.code,'value':str(value) if value is not None else None,'data_state':'value' if value is not None else 'missing','unit':f.indicator.unit,'territory':key[0],'period_start':key[1],'period_end':key[2],'period_kind':key[3],'period_label':labels[key],'group':key[4],'answer':key[5],'methodology_id':f.methodology_id,'provenance':{'formula_id':f.pk,'methodology_version':f.methodology.version,'component_versions':[c['version_id'] for c in components]},'status':'pending' if reasons else 'verified','issues':reasons}
            _,changed=upsert_observation(record,method='auto');result['versions_created']+=int(changed)
    return result
