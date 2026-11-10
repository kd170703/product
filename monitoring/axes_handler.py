"""Persist throttle counters without retaining submitted form data or fingerprinting."""
from axes.handlers.database import AxesDatabaseHandler
from django.http import QueryDict


class MinimalAxesHandler(AxesDatabaseHandler):
    def user_login_failed(self, sender, credentials, request=None, **kwargs):
        if request is None:
            return super().user_login_failed(sender, credentials, request, **kwargs)
        original_get, original_post = request.GET, request.POST
        try:
            request.GET = QueryDict()
            request.POST = QueryDict()
            request.axes_user_agent = ""
            request.axes_http_accept = ""
            return super().user_login_failed(sender, credentials, request, **kwargs)
        finally:
            request.GET, request.POST = original_get, original_post
