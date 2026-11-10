"""Chart specifications from any selected records, without aggregation or ORM."""
from collections import OrderedDict
from decimal import Decimal,InvalidOperation
from .periods import classify_period,period_sort_key
from .presentation import clean_public_text


def _number(row):
    if row.get('data_state')!='value' or row.get('value') is None:return None
    try:
        d=Decimal(str(row['value']))
        return float(d) if d.is_finite() else None
    except (InvalidOperation,ValueError):return None


def _family(row):
    if row.get('period_kind')=='stock':return 'stock'
    if row.get('section')=='statistics':
        kind=classify_period(row)['kind']
        # Mixed IV-quarter/year headings remain one explicitly labelled family.
        return 'quarter' if kind=='mixed' else kind
    return 'published-shares' if row.get('unit')=='%' else classify_period(row)['kind']


def _period_identity(row):
    return (str(row.get('period_start') or ''),str(row.get('period_end') or ''),'' if row.get('period_start') or row.get('period_end') else str(row.get('period_label') or ''))


def build_chart_specs(rows,chart_type='auto'):
    if chart_type not in ['auto','line','bar']:chart_type='auto'
    grouped=OrderedDict()
    for raw in rows:
        if _number(raw) is None:continue
        row=dict(raw)
        key=(row.get('indicator_code') or row.get('indicator_name'),row.get('unit'),row.get('territory'),row.get('group',''),row.get('question_version_id'),row.get('period_kind','unknown'),_family(row))
        grouped.setdefault(key,[]).append(row)
    specs=[]
    for key,records in grouped.items():
        records=sorted(records,key=lambda r:(period_sort_key(r),str(r.get('answer') or ''),str(r.get('id') or '')))
        periods=OrderedDict()
        for r in records:periods.setdefault(_period_identity(r),r)
        first=records[0]
        answers=list(dict.fromkeys(str(r.get('answer') or '') for r in records))
        dynamic=len(periods)>1
        has_duplicate=False
        if dynamic:
            labels=[p.get('period_label') or p.get('period_end') or p.get('period_start') or 'Без периода' for p in periods.values()]
            positions={k:n for n,k in enumerate(periods)}
            datasets=[]
            for answer in answers:
                values=[None]*len(labels);ids=[[] for _ in labels]
                for r in records:
                    if str(r.get('answer') or '')!=answer:continue
                    pos=positions[_period_identity(r)]
                    if values[pos] is not None:
                        # Keep separate source observations visible instead of averaging them.
                        has_duplicate=True
                        continue
                    values[pos]=_number(r);ids[pos]=[r['id']] if r.get('id') is not None else []
                datasets.append({'label':clean_public_text(answer) or first.get('indicator_name','Значение'),'values':values,'observation_ids':ids})
            if has_duplicate:
                labels=[(r.get('period_label') or 'Без периода')+' · '+(r.get('source_name') or 'Источник')+(f' · №{r["id"]}' if r.get('id') else '') for r in records]
                datasets=[{'label':first.get('indicator_name','Значение'),'values':[_number(r) for r in records],'observation_ids':[[r['id']] if r.get('id') else [] for r in records]}]
            kind=chart_type if chart_type!='auto' else ('bar' if has_duplicate else 'line')
        else:
            labels=[r.get('answer') or r.get('indicator_name') or r.get('indicator_code') or 'Значение' for r in records]
            if len(set(labels))!=len(labels):
                labels=[label+' · '+(r.get('source_name') or 'Источник')+(f' · №{r["id"]}' if r.get('id') else '') for label,r in zip(labels,records)]
                has_duplicate=True
            datasets=[{'label':first.get('period_label') or first.get('indicator_name','Значение'),'values':[_number(r) for r in records],'observation_ids':[[r['id']] if r.get('id') else [] for r in records]}]
            # Lines imply time; one-period answer distributions remain bars.
            kind='bar'
        sources=list(dict.fromkeys(clean_public_text(r.get('source_name') or 'Источник не указан') for r in records))
        urls=list(dict.fromkeys(r.get('source_url') for r in records if str(r.get('source_url') or '').startswith(('http://','https://'))))
        notes=[]
        if has_duplicate:notes.append('Совпадающие периоды разных записей показаны отдельно, без усреднения.')
        if any(classify_period(r).get('qualification') for r in records):notes.append('Подписи периодов сохранены как в источнике; значения не пересчитаны в другие периоды.')
        if len({classify_period(r)['kind'] for r in records})>1:notes.append('Показаны опубликованные доли за подписанные периоды; периоды не суммируются.')
        specs.append({'title':clean_public_text(first.get('indicator_name') or first.get('indicator_code') or 'Показатель'),'indicator_code':first.get('indicator_code'),'section':first.get('section'),'kind':kind,'dynamic':dynamic and not has_duplicate,'unit':first.get('unit',''),'territory':first.get('territory',''),'group':first.get('group',''),'period':'; '.join(dict.fromkeys(r.get('period_label') or '' for r in records)),'source_name':'; '.join(sources),'source_url':urls[0] if len(urls)==1 else '', 'source_urls':urls,'labels':labels,'x_values':[period_sort_key(r)[0] for r in periods.values()] if dynamic and not has_duplicate and all(period_sort_key(r)[0]<100000000 for r in periods.values()) else [],'values':datasets[0]['values'],'datasets':datasets,'observation_ids':[r['id'] for r in records if r.get('id') is not None],'rows':records,'notes':' '.join(notes)})
    return sorted(specs,key=lambda s:(not s['dynamic'],str(s.get('section')),str(s['title'])))
