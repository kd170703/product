"""Отдельный worker: очередь PostgreSQL, monthly scheduler, leases и advisory lock."""
import calendar,hashlib,json,traceback
from datetime import datetime,timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from django.conf import settings
from django.core.files.base import ContentFile
from django.core.exceptions import ValidationError
from django.db import transaction,connection,IntegrityError
from django.utils import timezone
from monitoring.models import Source,LoadRun,Publication,ImportBatch,DataIssue,Indicator
from .adapters import discover,fetch_url,parse_file,AdapterError
from .importer import register_definitions,validate_preview
from .observations import upsert_observation,validate_record,parse_date
from .calculations import recompute_formulas


def next_due(source,after=None):
    now=(after or timezone.now()).astimezone(ZoneInfo(source.timezone))
    hh=source.schedule_time
    if isinstance(hh,str):hh=datetime.strptime(hh,'%H:%M').time()
    if source.frequency=='manual':return None
    if source.frequency=='daily':
        due=now.replace(hour=hh.hour,minute=hh.minute,second=0,microsecond=0)
        return due if due>now else due+timedelta(days=1)
    day=min(source.schedule_day,calendar.monthrange(now.year,now.month)[1])
    due=now.replace(day=day,hour=hh.hour,minute=hh.minute,second=0,microsecond=0)
    if due<=now:
        y=now.year+(now.month==12);m=1 if now.month==12 else now.month+1
        due=due.replace(year=y,month=m,day=min(source.schedule_day,calendar.monthrange(y,m)[1]))
    return due


def enqueue_source(source,user=None,trigger='manual'):
    try:
        with transaction.atomic():return LoadRun.objects.create(source=source,requested_by=user,trigger=trigger)
    except IntegrityError:return LoadRun.objects.get(source=source,status__in=['queued','running','retry'])


def schedule_due(now=None):
    now=now or timezone.now();count=0
    for src in Source.objects.filter(enabled=True).exclude(frequency='manual'):
        if src.next_due is None:
            src.next_due=next_due(src,now-timedelta(seconds=1));src.save(update_fields=['next_due']);continue
        if src.next_due<=now:
            enqueue_source(src,trigger='schedule');src.next_due=next_due(src,now);src.save(update_fields=['next_due']);count+=1
    return count


@transaction.atomic
def claim_run():
    now=timezone.now()
    # A crashed worker's database lock is released; stale leases are retryable.
    for stale in LoadRun.objects.select_for_update(skip_locked=True).filter(status='running',lease_until__lt=now):
        stale.status='retry' if stale.attempts<3 else 'error';stale.next_attempt_at=now;stale.log+='\nИстёк срок исполнения. Задание возвращено в очередь.';stale.save()
    run=LoadRun.objects.select_for_update(skip_locked=True).filter(status__in=['queued','retry'],next_attempt_at__lte=now).order_by('created_at').first()
    if run:
        run.status='running';run.attempts+=1;run.started_at=now;run.lease_until=now+timedelta(minutes=15);run.save()
    return run


def save_publication(source,content,url,title='',published_at=None,metadata=None):
    checksum=hashlib.sha256(content).hexdigest()
    publication=Publication.objects.filter(source=source,url=url,checksum=checksum).first()
    if publication:return publication,False
    from urllib.parse import urlsplit
    name=Path(urlsplit(url).path).name if url.startswith(('http://','https://')) else Path(url).name
    if not name or '.' not in name:name='publication.xlsx'
    pub=Publication(source=source,url=url,title=title[:500] or name,checksum=checksum,published_at=parse_date(published_at),metadata=metadata or {})
    pub.file.save(name,ContentFile(content),save=False);pub.save()
    return pub,True


@transaction.atomic
def apply_payload(payload,publication):
    payload=validate_preview(payload)
    if payload.get('errors'):raise AdapterError(f'Проверка данных: {payload["errors"][:5]}')
    waves,qvs=register_definitions(payload,publication.source)
    by_issue={i.get('key'):i for i in payload.get('issues',[]) if isinstance(i,dict)}
    count=unchanged=0
    for raw in payload['observations']:
        r=dict(raw);r['indicator_code']=r.get('indicator_code') or r.get('indicator_key')
        if not Indicator.objects.filter(code=r['indicator_code'],is_active=True).exists():continue
        r['unit']=r.get('unit') or 'Не уточнено'
        r['issues']=[by_issue[x].get('message',x) if isinstance(x,str) and x in by_issue else str(x) for x in r.get('issues',[])]
        if r.get('question_key') in qvs:r['question_version_id']=qvs[r['question_key']].pk
        if r.get('study_key') in waves:r['wave_id']=waves[r['study_key']].pk
        _,changed=upsert_observation(r,publication=publication,method='auto');count+=int(changed);unchanged+=int(not changed)
    return {'versions_created':count,'unchanged':unchanged}


def _folder_payloads(source):
    root=settings.IMPORT_WATCH_DIR.resolve()
    sub=(root/str(source.mapping.get('folder',''))).resolve()
    if not sub.is_relative_to(root):raise AdapterError('Папка должна находиться внутри IMPORT_WATCH_DIR.')
    if not sub.is_dir():raise AdapterError('Согласованная папка ещё не создана: '+str(sub))
    paths=sorted(p for p in sub.iterdir() if p.suffix.lower() in ['.xlsx','.csv','.json'] and p.is_file() and p.resolve().is_relative_to(root))
    if not paths:raise AdapterError('В согласованной папке нет XLSX/CSV/JSON.')
    from .registry_parser import parse_registry
    from .importer import generic_preview
    for p in paths:
        if p.stat().st_size>25*1024*1024:raise AdapterError('Файл папки превышает 25 МБ.')
        content=p.read_bytes()
        pub,new=save_publication(source,content,'folder:'+p.name,p.name)
        if new:
            yield pub,parse_registry(pub.file.path) if source.mapping.get('format','registry')=='registry' else generic_preview(pub.file.path)
        elif not pub.metadata.get('applied'):
            yield pub,parse_registry(pub.file.path) if source.mapping.get('format','registry')=='registry' else generic_preview(pub.file.path)


def execute_run(run):
    source=run.source
    with connection.cursor() as cursor:
        cursor.execute('SELECT pg_try_advisory_lock(%s,%s)',[170703,source.pk]);locked=cursor.fetchone()[0]
    if not locked:
        run.status='retry';run.next_attempt_at=timezone.now()+timedelta(seconds=30);run.log+='\nИсточник уже обрабатывается другим процессом.';run.save();return run
    log=[];result={'versions_created':0,'unchanged':0,'files_checked':0,'publications_created':0}
    try:
        config={**source.mapping,'territory':source.territory,'unit':source.unit}
        # Holding a definition blocks writing, but does not present it as a network failure.
        if source.hold_reason:
            raise AdapterError('Импорт приостановлен: '+source.hold_reason)
        if source.adapter=='folder':items=_folder_payloads(source)
        else:
            candidates=discover(source)
            def online_items():
                for item in candidates:
                    run.lease_until=timezone.now()+timedelta(minutes=15);run.save(update_fields=['lease_until'])
                    download=fetch_url(item['url'])
                    pub,new=save_publication(source,download.content,item['url'],item.get('title',''),item.get('published_at'),{'etag':download.etag,'last_modified':download.last_modified,'final_url':download.url})
                    result['files_checked']+=1;result['publications_created']+=int(new)
                    if not new and pub.metadata.get('applied'):
                        log.append(f'Без изменений: {item["url"]} ({pub.checksum[:12]})');continue
                    records=parse_file(pub.file.path,source.adapter,config)
                    for record in records:
                        record['provenance']={**record.get('provenance',{}),'url':item['url'],'file':Path(pub.file.name).name}
                    yield pub,{'observations':records,'issues':[]}
            items=online_items()
        for pub,payload in items:
            applied=apply_payload(payload,pub)
            pub.metadata={**pub.metadata,'applied':True,'result':applied};pub.save(update_fields=['metadata'])
            result['versions_created']+=applied['versions_created'];result['unchanged']+=applied['unchanged']
            if pub.published_at and (not source.last_publication_date or pub.published_at>source.last_publication_date):source.last_publication_date=pub.published_at
            log.append(f'{pub.title}: новых версий {applied["versions_created"]}, без изменений {applied["unchanged"]}. SHA256 {pub.checksum}')
        result['calculations']=recompute_formulas()
        run.status='success';run.finished_at=timezone.now()
        source.last_success=run.finished_at;source.status='success';source.last_error=''
    except Exception as exc:
        message=str(exc)
        log.append(message)
        run.status='retry' if run.attempts<3 else 'error'
        run.next_attempt_at=timezone.now()+timedelta(seconds=min(300,30*2**(run.attempts-1)))
        if run.status=='error':run.finished_at=timezone.now()
        source.status='error';source.last_error=message
    finally:
        source.last_checked=timezone.now();source.save(update_fields=['last_checked','last_success','last_publication_date','status','last_error'])
        run.result=result;run.log+='\n'.join(log)+'\n';run.lease_until=None;run.save()
        with connection.cursor() as cursor:cursor.execute('SELECT pg_advisory_unlock(%s,%s)',[170703,source.pk])
    return run


def worker_tick():
    scheduled=schedule_due()
    run=claim_run()
    if run:execute_run(run)
    return {'scheduled':scheduled,'run':run.pk if run else None}
