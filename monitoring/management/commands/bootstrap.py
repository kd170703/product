from django.core.management.base import BaseCommand
from django.contrib.auth.models import Group,Permission
from django.utils import timezone
from monitoring.models import Topic,Source,Indicator,Methodology
from monitoring.services.registry_parser import TOPICS
from monitoring.services.adapters import PILOT_SOURCES
from monitoring.services.workers import next_due

PILOT_CODES={
'cbr_deposits':'statistics-1b7998ed9bdc00979be2',
'cbr_cards':'statistics-11c84a45744e957b8a96',
'rosstat_income':'statistics-bff71aaa4044235797c4',
'fns_social':'statistics-8ce8b86021089b039f87'}
PILOT_NAMES={
'cbr_deposits':'Объём вкладов физических лиц без учёта счетов эскроу',
'cbr_cards':'Количество расчётных и кредитных карт: определение требует сверки',
'rosstat_income':'Среднедушевые денежные доходы населения Москвы',
'fns_social':'Число налогоплательщиков, заявивших социальные налоговые вычеты'}

class Command(BaseCommand):
    help='Создать справочники, три роли и зарегистрировать пилотные источники; без паролей и вымышленных наблюдений.'
    def handle(self,*args,**options):
        for n,(slug,name) in enumerate(TOPICS,1):Topic.objects.get_or_create(slug=slug,defaults={'name':name,'order':n})
        viewer,_=Group.objects.get_or_create(name='Просмотр')
        editor,_=Group.objects.get_or_create(name='Редактирование')
        admin,_=Group.objects.get_or_create(name='Администрирование')
        perms=Permission.objects.filter(content_type__app_label='monitoring')
        viewer.permissions.set(perms.filter(codename__startswith='view_'))
        editor.permissions.set(perms.filter(codename__startswith='view_')|perms.filter(codename__in=['add_observation','add_indicator','change_indicator','add_source','change_source','add_importbatch','add_reportsnapshot','add_manualcorrection','add_topic','change_topic','add_topicalias','change_topicalias']))
        # Immutable history/publication/snapshot rows remain read-only in admin for everyone.
        admin.permissions.set(perms|Permission.objects.filter(content_type__app_label='auth'))
        for spec in PILOT_SOURCES:
            spec={**spec,'mapping':dict(spec['mapping'])}
            adapter=spec['adapter'];spec['mapping']['indicator_code']=PILOT_CODES[adapter]
            # Descriptive parser holds are kept in mapping; semantic blocking is enforced by each adapter.
            source,_=Source.objects.get_or_create(name=spec.pop('name'),defaults={**spec,'enabled':True,'frequency':'monthly'})
            if source.next_due is None:source.next_due=next_due(source);source.save(update_fields=['next_due'])
            ind,_=Indicator.objects.get_or_create(code=PILOT_CODES[adapter],defaults={'name':PILOT_NAMES[adapter],'section':'statistics','unit':source.unit,'period_kind':'stock' if adapter.startswith('cbr_') else 'flow','source':source})
            topic={'cbr_deposits':'savings','cbr_cards':'money','rosstat_income':'budget','fns_social':'tax'}[adapter]
            ind.topics.add(Topic.objects.get(slug=topic))
        Source.objects.get_or_create(name='Согласованная папка · социология и финтест',defaults={'adapter':'folder','frequency':'monthly','mapping':{'folder':'','format':'registry'},'enabled':False})
        Methodology.objects.get_or_create(code='irfk',version=1,defaults={'name':'ИРФК — проект методики','approved':False,'description':'Для полного расчёта требуется согласовать финальную методику, состав компонентов, нормирование и веса. Документы с исправлениями не являются утверждением.'})
        self.stdout.write(self.style.SUCCESS('Справочники и роли созданы. Наблюдения импортируются из исходного реестра.'))
