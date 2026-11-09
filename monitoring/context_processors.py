def navigation(request):
    user = getattr(request, 'user', None)
    return {'can_edit': bool(user and user.is_authenticated and (user.is_superuser or user.has_perm('monitoring.add_observation'))), 'can_administer': bool(user and user.is_authenticated and (user.is_superuser or user.has_perm('monitoring.change_source')))}
