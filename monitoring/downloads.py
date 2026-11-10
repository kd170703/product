from django.contrib.auth.decorators import login_required
from django.http import FileResponse,Http404,JsonResponse
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404
from monitoring.models import Publication,ImportBatch,ReportSnapshot
from monitoring.services.presentation import clean_public_data

@login_required
def download(request,kind,pk,fmt):
    if not (request.user.is_superuser or request.user.has_perm('monitoring.view_observation')):raise PermissionDenied('Необходима роль просмотра.')
    if kind=='report':
        obj=get_object_or_404(ReportSnapshot,pk=pk)
        field=obj.docx_file if fmt=='docx' else obj.xlsx_file if fmt=='xlsx' else None
    elif kind=='publication':field=get_object_or_404(Publication,pk=pk).file
    elif kind=='batch':
        batch=get_object_or_404(ImportBatch,pk=pk)
        if fmt=='preview':
            response=JsonResponse(clean_public_data(batch.preview),json_dumps_params={'ensure_ascii':False,'indent':2})
            response['Content-Disposition']=f'attachment; filename="import-preview-{batch.pk}.json"'
            return response
        field=batch.file
    else:raise Http404
    if not field:raise Http404
    return FileResponse(field.open('rb'),as_attachment=True,filename=field.name.rsplit('/',1)[-1])
