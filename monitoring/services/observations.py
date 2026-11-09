"""Единственный путь записи наблюдений: проверка, идемпотентность, версии."""
import hashlib, json
from datetime import date
from decimal import Decimal, InvalidOperation
from django.core.exceptions import ValidationError, PermissionDenied
from django.db import transaction
from django.utils import timezone
from monitoring.models import (Indicator, Observation, ObservationVersion, ManualCorrection, QuestionVersion, AnswerOption, Wave)


def require_editor(user):
    if not user or not user.is_authenticated or not (user.is_superuser or user.has_perm('monitoring.add_observation')):
        raise PermissionDenied('Для изменения данных нужна роль редактора.')


def decimal_or_none(value):
    if value is None or value == '': return None
    if isinstance(value,bool): raise ValidationError('Логическое значение не является числом.')
    try:
        number=Decimal(str(value).replace('\u00a0','').replace(' ','').replace(',','.'))
        if not number.is_finite(): raise ValueError()
        if abs(number) >= Decimal('1e20'): raise ValueError()
        if number.as_tuple().exponent < -8: number=number.quantize(Decimal('.00000001'))
        return number
    except (InvalidOperation,ValueError): raise ValidationError(f'Некорректное число: {value}')


def parse_date(value):
    if not value: return None
    if isinstance(value,date): return value
    try: return date.fromisoformat(str(value)[:10])
    except ValueError: raise ValidationError(f'Некорректная дата: {value}. Нужен YYYY-MM-DD.')


def validate_record(record, indicator=None):
    r=dict(record)
    code=r.get('indicator_code') or r.get('indicator_key')
    if not code: raise ValidationError('Не указан код показателя.')
    if indicator is None:
        try: indicator=Indicator.objects.get(code=code)
        except Indicator.DoesNotExist: raise ValidationError(f'Неизвестный показатель: {code}')
    r['indicator_code']=code
    r['value']=decimal_or_none(r.get('value'))
    r['data_state']=r.get('data_state') or ('missing' if r['value'] is None else 'value')
    if r['data_state'] not in ['value','missing','na']: raise ValidationError('Неизвестный статус значения.')
    if (r['data_state']=='value') != (r['value'] is not None): raise ValidationError('Число необходимо только для состояния «Значение».')
    for field in ['unit','territory','period_label']:
        if not str(r.get(field) or '').strip(): raise ValidationError(f'Не заполнено обязательное поле: {field}.')
    r['period_start']=parse_date(r.get('period_start'))
    r['period_end']=parse_date(r.get('period_end'))
    if r['period_start'] and r['period_end'] and r['period_start']>r['period_end']: raise ValidationError('Начало периода позже конца.')
    r['period_kind']=r.get('period_kind') or r.get('metric_kind') or 'unknown'
    if r['period_kind'] not in ['stock','flow','cumulative','unknown']: r['period_kind']='unknown'
    for f in ['numerator','denominator']: r[f]=decimal_or_none(r.get(f))
    if r['denominator'] is not None and r['denominator']<0: raise ValidationError('Знаменатель не может быть отрицательным.')
    if r['numerator'] is not None and (r['denominator'] is None or r['denominator']<=0): raise ValidationError('Для числителя нужен положительный знаменатель.')
    if r['numerator'] is not None and (r['numerator']<0 or r['numerator']>r['denominator']): raise ValidationError('Числитель должен быть от 0 до знаменателя.')
    issues=list(r.get('issues') or [])
    if not (r['period_start'] or r['period_end']): issues.append('Период не приведён к датам; требуется сверка.')
    if r['period_kind']=='unknown': issues.append('Тип периода требует проверки.')
    if indicator.unit and indicator.unit!=r['unit']: issues.append(f'Единица паспорта «{indicator.unit}» отличается от «{r["unit"]}».')
    r['issues']=list(dict.fromkeys(str(i) for i in issues))
    r['status']='conflict' if r.get('status')=='conflict' or r.get('verification')=='conflict' else ('pending' if r['issues'] or r.get('status')=='pending' or r.get('verification') in ['pending','review','needs_review','unverified'] else 'verified')
    return r


def _hash(obj): return hashlib.sha256(json.dumps(obj,ensure_ascii=False,sort_keys=True,default=str).encode()).hexdigest()


@transaction.atomic
def upsert_observation(record, publication=None, user=None, method='auto'):
    if method in ['manual','gpt']: require_editor(user)
    indicator=Indicator.objects.get(code=record.get('indicator_code') or record.get('indicator_key'))
    r=validate_record(record,indicator)
    qv=None
    if r.get('question_version_id'): qv=QuestionVersion.objects.get(pk=r['question_version_id'],question__indicator=indicator)
    elif hasattr(indicator,'question'): qv=indicator.question.versions.order_by('-version').first()
    option=None
    answer=str(r.get('answer') or '')[:1500]
    if qv and answer:
        option,_=AnswerOption.objects.get_or_create(question_version=qv,code=_hash(answer)[:32],defaults={'label':answer})
    wave=None
    if r.get('wave_id'): wave=Wave.objects.get(pk=r['wave_id'])
    dim={'indicator':indicator.pk,'question_version':qv.pk if qv else None,'option':option.pk if option else answer,'wave':wave.pk if wave else None,'territory':r['territory'],'start':r['period_start'],'end':r['period_end'],'kind':r['period_kind'],'group':r.get('group') or ''}
    # Raw label is a dimension only when there are no machine-readable dates.
    if not r['period_start'] and not r['period_end']: dim['raw_period']=r['period_label']
    obs,_=Observation.objects.get_or_create(dimension_key=_hash(dim),defaults={'indicator':indicator,'question_version':qv,'answer_option':option,'wave':wave,'territory':r['territory'],'period_start':r['period_start'],'period_end':r['period_end'],'period_label':r['period_label'],'period_kind':r['period_kind'],'group':r.get('group') or '','answer':answer})
    obs=Observation.objects.select_for_update().get(pk=obs.pk)
    provenance=r.get('provenance') or r.get('source') or {}
    if isinstance(provenance,str): provenance={'text':provenance}
    if r.get('raw_value') is not None: provenance={**provenance,'raw_value':r['raw_value']}
    if r.get('source_references'): provenance={**provenance,'source_references':r['source_references']}
    if r.get('answer_count') is not None: provenance={**provenance,'answer_count':r['answer_count']}
    content={'value':r['value'],'data_state':r['data_state'],'unit':r['unit'],'numerator':r['numerator'],'denominator':r['denominator'],'base':str(r['base']) if r.get('base') is not None else '','provenance':provenance,'issues':r['issues'],'publication_checksum':publication.checksum if publication else None,'status':r['status'],'publication_date':r.get('publication_date') or (publication.published_at if publication else None),'methodology_id':r.get('methodology_id')}
    digest=_hash(content)
    if obs.current_version and (obs.current_version.content_hash==digest or obs.current_version.provenance.get('_source_hash')==digest): return obs,False
    if obs.current_version and publication and obs.current_version.publication and publication.source_id!=obs.current_version.publication.source_id:
        r['status']='conflict'; r['issues'].append('Разные источники дают наблюдение с одинаковыми измерениями. Требуется сверка.')
    version=ObservationVersion.objects.create(observation=obs,number=(obs.current_version.number+1 if obs.current_version else 1),value=r['value'],data_state=r['data_state'],unit=r['unit'],numerator=r['numerator'],denominator=r['denominator'],base=str(r['base']) if r.get('base') is not None else '',publication=publication,publication_date=parse_date(r.get('publication_date')) or (publication.published_at if publication else None),provenance=provenance,method=method,status=r['status'],issues=r['issues'],content_hash=digest,created_by=user,reason=r.get('reason') or '',methodology_id=r.get('methodology_id'))
    obs.current_version=version;obs.save(update_fields=['current_version'])
    obs.corrections.filter(active=True).update(conflict=True)
    return obs,True


@transaction.atomic
def apply_correction(obs,value,data_state,reason,user):
    require_editor(user)
    obs=Observation.objects.select_for_update().get(pk=obs.pk)
    if not obs.current_version: raise ValidationError('Нет исходной версии.')
    if not str(reason).strip(): raise ValidationError('Укажите обоснование поправки.')
    value=decimal_or_none(value)
    if data_state not in ['value','missing','na'] or ((data_state=='value') != (value is not None)): raise ValidationError('Проверьте число и состояние данных.')
    obs.corrections.filter(active=True).update(active=False,resolved_by=user,resolved_at=timezone.now(),resolution='Заменена новой ручной поправкой')
    return ManualCorrection.objects.create(observation=obs,based_on=obs.current_version,value=value,data_state=data_state,reason=reason,created_by=user)


@transaction.atomic
def resolve_correction(correction,choice,reason,user):
    require_editor(user)
    if choice not in ['source','correction'] or not reason.strip(): raise ValidationError('Нужны решение и обоснование.')
    correction=ManualCorrection.objects.select_for_update().get(pk=correction.pk,active=True)
    correction.active=(choice=='correction'); correction.conflict=False
    correction.resolution=reason;correction.resolved_by=user;correction.resolved_at=timezone.now()
    if correction.active: correction.based_on=correction.observation.current_version
    correction.save()
    return correction


@transaction.atomic
def review_observation(obs,reason,user):
    require_editor(user)
    if not reason.strip():raise ValidationError('Укажите обоснование проверки и решение по замечаниям.')
    obs=Observation.objects.select_for_update().get(pk=obs.pk)
    v=obs.current_version
    if not v:raise ValidationError('Нет исходной версии.')
    provenance={**v.provenance,'_source_hash':v.provenance.get('_source_hash',v.content_hash),'review':{'author_id':user.pk,'reason':reason,'at':timezone.now().isoformat()}}
    new=ObservationVersion.objects.create(observation=obs,number=v.number+1,value=v.value,data_state=v.data_state,unit=v.unit,numerator=v.numerator,denominator=v.denominator,base=v.base,publication=v.publication,publication_date=v.publication_date,provenance=provenance,method='manual',methodology=v.methodology,status='verified',issues=v.issues,content_hash=_hash({'previous':v.content_hash,'review':provenance['review']}),created_by=user,reason=reason)
    obs.current_version=new;obs.save(update_fields=['current_version'])
    return new
