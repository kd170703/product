"""Предпросмотр импорта отдельно от подтверждённой транзакционной записи."""
import csv,hashlib,io,json,zipfile
from pathlib import Path
from django.core.files.base import ContentFile
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from openpyxl import load_workbook
from monitoring.models import (ImportBatch,Source,Publication,Topic,TopicAlias,Indicator,Study,Wave,Question,QuestionVersion,DataIssue)
from .observations import require_editor,upsert_observation,validate_record
from .registry_parser import parse_registry

MAX_FILE=25*1024*1024


def generic_preview(path):
    path=Path(path)
    if path.suffix.lower()=='.json':
        value=json.loads(path.read_text(encoding='utf-8-sig'))
        if isinstance(value,list): value={'observations':value}
        if not isinstance(value,dict): raise ValidationError('JSON должен содержать observations.')
        return value
    if path.suffix.lower()=='.csv':
        with path.open(encoding='utf-8-sig',newline='') as f:
            rows=list(csv.DictReader(f))
    else:
        wb=load_workbook(path,data_only=True,read_only=True)
        ws=wb.active
        records=ws.iter_rows(values_only=True)
        header=[str(x or '').strip() for x in next(records)]
        rows=[dict(zip(header,row)) for row in records if any(x is not None for x in row)]
        wb.close()
    return {'schema_version':1,'observations':rows,'issues':[],'summary':{'observations':len(rows)}}


def validate_preview(payload):
    if not isinstance(payload,dict) or not isinstance(payload.get('observations'),list): raise ValidationError('Нет списка observations.')
    if len(payload['observations'])>100000: raise ValidationError('Слишком много записей. Разделите файл.')
    issues=payload.setdefault('issues',[])
    if not isinstance(issues,list): raise ValidationError('issues должен быть списком.')
    payload['errors']=[]
    seen_dimensions=set()
    if not payload['observations']:payload['errors'].append({'row':0,'message':'В файле нет наблюдений для импорта.'})
    for issue in issues:
        if isinstance(issue,dict) and issue.get('severity')=='error':payload['errors'].append({'row':0,'message':issue.get('message') or issue.get('description') or str(issue)})
    proposed={i.get('key') or i.get('code'):i for i in payload.get('indicators',[])}
    for n,raw in enumerate(payload['observations'],1):
        try:
            code=raw.get('indicator_code') or raw.get('indicator_key')
            indicator=Indicator.objects.filter(code=code).first()
            if indicator is None and code in proposed:
                spec=proposed[code]; indicator=Indicator(code=code,unit=spec.get('unit') or '',section=spec.get('section','statistics'),name=spec.get('name',code))
            checked=validate_record({**raw,'unit':raw.get('unit') or 'Не уточнено'},indicator)
            dimensions=(checked['indicator_code'],checked['territory'],str(checked['period_start']),str(checked['period_end']),checked['period_label'] if not checked['period_start'] and not checked['period_end'] else '',checked['period_kind'],str(raw.get('group') or ''),str(raw.get('answer') or ''),str(raw.get('question_version_id') or raw.get('question_version') or ''),str(raw.get('study_key') or raw.get('wave_id') or ''))
            if dimensions in seen_dimensions:raise ValidationError('Повторяющиеся измерения внутри файла. Сверьте дубли до подтверждения импорта.')
            seen_dimensions.add(dimensions)
        except (ValidationError,TypeError,ValueError) as exc:
            payload['errors'].append({'row':n,'message':str(exc)})
    payload.setdefault('summary',{})['observations']=len(payload['observations'])
    payload['summary']['errors']=len(payload['errors'])
    return json.loads(json.dumps(payload,ensure_ascii=False,default=str))


def create_preview(file,user,kind='registry',source=None):
    require_editor(user)
    content=file.read()
    if len(content)>MAX_FILE: raise ValidationError('Размер файла превышает 25 МБ.')
    name=Path(getattr(file,'name','import.xlsx')).name
    if Path(name).suffix.lower() not in ['.xlsx','.csv','.json']: raise ValidationError('Допустимы XLSX, CSV и JSON.')
    if Path(name).suffix.lower()=='.xlsx':
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                members=archive.infolist()
                if len(members)>10000 or sum(x.file_size for x in members)>120*1024*1024:raise ValidationError('Слишком большой распакованный Excel. Разделите файл.')
        except zipfile.BadZipFile:raise ValidationError('Некорректный файл XLSX.')
    batch=ImportBatch(kind=kind,created_by=user,source=source,checksum=hashlib.sha256(content).hexdigest())
    batch.file.save(name,ContentFile(content),save=False)
    batch.save()
    try:
        if kind=='registry':
            payload=parse_registry(batch.file.path)
            previous=ImportBatch.objects.filter(checksum=batch.checksum,kind=kind,status='confirmed').order_by('pk').first()
            canonical_name=(previous.preview.get('files',[{}])[0].get('name') if previous and previous.preview.get('files') else name)
            def stable_file_names(obj):
                if isinstance(obj,dict):
                    for key,value in obj.items():
                        if key=='file' and isinstance(value,str):obj[key]=canonical_name
                        else:stable_file_names(value)
                elif isinstance(obj,list):
                    for item in obj:stable_file_names(item)
            stable_file_names(payload)
            for item in payload.get('files',[]):item['name']=canonical_name
        elif source is not None:
            from .adapters import parse_file
            payload={'observations':parse_file(batch.file.path,source.adapter,{**source.mapping,'territory':source.territory,'unit':source.unit}),'issues':[]}
        else: payload=generic_preview(batch.file.path)
        batch.preview=validate_preview(payload);batch.save(update_fields=['preview'])
    except Exception as exc:
        batch.status='error';batch.result={'error':str(exc)};batch.save(update_fields=['status','result'])
        raise ValidationError(f'Не удалось разобрать файл: {exc}')
    return batch


def create_gpt_preview(payload,user,file=None):
    require_editor(user)
    batch=ImportBatch(kind='gpt',created_by=user,preview=validate_preview(payload))
    if file:
        file.seek(0);batch.file.save(Path(file.name).name,file,save=False)
    batch.save()
    return batch


def register_definitions(payload,source):
    topic_map={}
    for n,t in enumerate(payload.get('topics',[]),1):
        key=t.get('key') or t.get('slug')
        topic,_=Topic.objects.get_or_create(slug=key,defaults={'name':t['name'],'order':n})
        topic_map[key]=topic
    topic_map.update({t.slug:t for t in Topic.objects.all()})
    for a in payload.get('topic_aliases',[]):
        key=a.get('topic_key') or a.get('topic')
        if key in topic_map: TopicAlias.objects.get_or_create(name=a.get('alias') or a.get('name'),defaults={'topic':topic_map[key]})
    studies={};waves={}
    for s in payload.get('studies',[]):
        key=s.get('key') or s['code']
        study,_=Study.objects.get_or_create(code=key,defaults={'name':s['name'],'description':s.get('notes') or ''})
        studies[key]=study
        label=s.get('wave')
        if label:
            wave,_=Wave.objects.get_or_create(study=study,code=label[:150],defaults={'name':label,'start':s.get('period_start'),'end':s.get('period_end'),'base':s.get('base')})
            waves[key]=wave
    for i in payload.get('indicators',[]):
        code=i.get('key') or i.get('code')
        ind,_=Indicator.objects.get_or_create(code=code,defaults={'name':i['name'],'unit':i.get('unit') or '', 'section':i.get('section') or i.get('category') or 'statistics','period_kind':i.get('metric_kind') or 'unknown','source':source,'definition':i.get('definition') or '', 'notes':i.get('notes') or ''})
        for t in i.get('topics',[]):
            if t in topic_map: ind.topics.add(topic_map[t])
    qvs={}
    for q in payload.get('questions',[]):
        ind=Indicator.objects.get(code=q['indicator_key'])
        study=studies.get(q.get('study_key'))
        question,_=Question.objects.get_or_create(indicator=ind,defaults={'study':study,'multiple_choice':True if q.get('multiple_choice') in [True,'yes'] else False if q.get('multiple_choice') in [False,'no'] else None})
        version,_=QuestionVersion.objects.get_or_create(question=question,version=q.get('version',1),defaults={'text':q['text'],'notes':q.get('notes') or ''})
        if version.text!=q['text']:
            # Existing question versions are never silently overwritten.
            raise ValidationError('Текст существующей версии вопроса изменён. Создайте новую версию вопроса.')
        qvs[q['key']]=version
    return waves,qvs


@transaction.atomic
def confirm_batch(batch,user):
    require_editor(user)
    batch=ImportBatch.objects.select_for_update().get(pk=batch.pk)
    if batch.status=='confirmed': return batch.result
    if batch.status!='preview': raise ValidationError('Этот импорт нельзя подтвердить.')
    payload=validate_preview(batch.preview)
    if payload.get('errors'): raise ValidationError(f'Исправьте {len(payload["errors"])} ошибок до импорта.')
    source=batch.source
    if source is None:
        source,_=Source.objects.get_or_create(name='Исходный Excel-реестр' if batch.kind=='registry' else ('Предложения GPT' if batch.kind=='gpt' else 'Ручной импорт'),defaults={'adapter':'registry' if batch.kind=='registry' else 'xlsx','frequency':'manual'})
    publication=batch.publication
    if publication is None and batch.file:
        content=batch.file.read();batch.file.close()
        checksum=hashlib.sha256(content).hexdigest()
        publication=Publication.objects.filter(source=source,url='',checksum=checksum).first()
        if not publication:
            publication=Publication(source=source,url='',title=Path(batch.file.name).name,checksum=checksum)
            publication.file.save(Path(batch.file.name).name,ContentFile(content),save=False);publication.save()
    waves,qvs=register_definitions(payload,source)
    by_issue={i.get('key'):i for i in payload.get('issues',[]) if isinstance(i,dict)}
    created=unchanged=skipped_deleted=0
    for raw in payload['observations']:
        r=dict(raw)
        r['indicator_code']=r.get('indicator_code') or r.get('indicator_key')
        if not Indicator.objects.filter(code=r['indicator_code'],is_active=True).exists():
            skipped_deleted+=1;continue
        r['unit']=r.get('unit') or 'Не уточнено'
        r['issues']=[by_issue[x].get('message',str(x)) if isinstance(x,str) and x in by_issue else str(x) for x in r.get('issues',[])]
        # A proposal is always explicitly reviewed; presence of source warnings remains visible.
        if batch.kind=='gpt': r['status']='pending'
        if r.get('question_key') in qvs:r['question_version_id']=qvs[r['question_key']].pk
        if r.get('study_key') in waves:r['wave_id']=waves[r['study_key']].pk
        _,changed=upsert_observation(r,publication=publication,user=user,method='gpt' if batch.kind=='gpt' else 'manual')
        created+=int(changed); unchanged+=int(not changed)
    for i in payload.get('issues',[]):
        if not isinstance(i,dict): i={'code':'warning','message':str(i)}
        DataIssue.objects.get_or_create(batch=batch,code=i.get('key') or i.get('code','warning'),defaults={'description':i.get('message') or i.get('description') or str(i),'severity':i.get('severity','warning'),'references':[i.get('source',{})]})
    from .calculations import recompute_formulas
    calculations=recompute_formulas()
    batch.source=source;batch.publication=publication;batch.status='confirmed';batch.confirmed_at=timezone.now();batch.result={'versions_created':created,'unchanged':unchanged,'skipped_deleted':skipped_deleted,'issues':len(payload.get('issues',[])),'calculations':calculations};batch.save()
    return batch.result
