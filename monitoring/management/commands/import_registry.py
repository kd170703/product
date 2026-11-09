from pathlib import Path
import json
from django.core.management.base import BaseCommand,CommandError
from django.contrib.auth import get_user_model
from django.core.files import File
from monitoring.models import DataIssue,Indicator,Observation,ObservationVersion
from monitoring.services.observations import _hash
from monitoring.services.importer import create_preview,confirm_batch

class Command(BaseCommand):
    help='Предпросмотр и импорт исходного реестра XLSX. Оригинал сохраняется без изменений.'
    def add_arguments(self,p):
        p.add_argument('file');p.add_argument('--username',required=True);p.add_argument('--preview-only',action='store_true');p.add_argument('--audit')
    def handle(self,*args,**o):
        try:user=get_user_model().objects.get(username=o['username'])
        except get_user_model().DoesNotExist:raise CommandError('Пользователь не найден. Создайте редактора или superuser.')
        with Path(o['file']).open('rb') as f:batch=create_preview(File(f,name=Path(o['file']).name),user)
        self.stdout.write(json.dumps({'batch_id':batch.pk,'summary':batch.preview.get('summary'),'errors':batch.preview.get('errors',[])[:10]},ensure_ascii=False))
        if not o['preview_only']:self.stdout.write(json.dumps(confirm_batch(batch,user),ensure_ascii=False))
        if o.get('audit'):
            data=json.loads(Path(o['audit']).read_text(encoding='utf-8'))
            issues=data.get('issues',[]) if isinstance(data,dict) else data
            for item in issues:
                code=item.get('key') or item.get('code','audit')
                key=item.get('indicator_key')
                DataIssue.objects.get_or_create(code=code,batch=None,defaults={'description':item.get('message') or item.get('description') or str(item),'severity':item.get('severity','warning'),'references':item.get('references') or [item.get('source') or {}],'indicator':Indicator.objects.filter(code=key).first() if key else None})
                message=item.get('message') or item.get('description') or str(item)
                for cell in item.get('target_cells',[]):
                    for obs in Observation.objects.filter(current_version__provenance__sheet=cell['sheet'],current_version__provenance__cell=cell['cell']).select_related('current_version'):
                        v=obs.current_version
                        if message in v.issues:continue
                        copy={f.name:getattr(v,f.name) for f in ObservationVersion._meta.fields if f.name not in ['id','number','created_at','created_by','reason','content_hash','status','issues']}
                        copy['provenance']={**v.provenance,'_source_hash':v.provenance.get('_source_hash',v.content_hash)}
                        new=ObservationVersion.objects.create(**copy,number=v.number+1,status='conflict',issues=v.issues+[message],content_hash=_hash({'prior':v.content_hash,'audit':code}),created_by=user,reason='Сопоставление Word и Excel: '+code)
                        obs.current_version=new;obs.save(update_fields=['current_version'])
            self.stdout.write(f'Добавлено вопросов сверки: {len(issues)}')
