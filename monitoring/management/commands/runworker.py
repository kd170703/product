import time
from django.core.management.base import BaseCommand
from django.db import close_old_connections
from monitoring.services.workers import worker_tick

class Command(BaseCommand):
    help='Отдельный процесс очереди и расписания. Браузер не требуется.'
    def add_arguments(self,parser):
        parser.add_argument('--once',action='store_true');parser.add_argument('--interval',type=int,default=15)
    def handle(self,*args,**options):
        from monitoring.management.commands.cleanup_auth import cleanup_auth_records
        next_auth_cleanup = 0
        while True:
            close_old_connections()
            if time.monotonic() >= next_auth_cleanup:
                cleanup_auth_records()
                next_auth_cleanup = time.monotonic() + 3600
            result=worker_tick()
            if result['run']:self.stdout.write(str(result))
            if options['once']:return
            time.sleep(max(1,min(options['interval'],60)))
