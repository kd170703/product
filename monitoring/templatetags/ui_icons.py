"""Decorative, consistent interface icons from the local SVG sprite."""
from django import template
from django.templatetags.static import static
from django.utils.html import format_html

register = template.Library()
ICONS = {
    'dashboard', 'users', 'check', 'trend', 'target', 'check-square',
    'upload', 'report', 'sparkles', 'settings', 'menu', 'arrow-right',
    'arrow-left', 'external', 'download', 'plus', 'info', 'refresh',
    'filter', 'folder', 'lock', 'alert', 'history', 'database', 'logout',
}


@register.simple_tag
def icon(name, class_name=''):
    """Render a decorative SVG; the adjacent label names the action."""
    if name not in ICONS:
        name = 'info'
    url = static('monitoring/icons.svg') + '?v=20261110-a11y#' + name
    classes = 'icon' + (' ' + str(class_name) if class_name else '')
    return format_html(
        '<svg class="{}" viewBox="0 0 24 24" aria-hidden="true" '
        'focusable="false" fill="none" stroke="currentColor" stroke-width="1.8" '
        'stroke-linecap="round" stroke-linejoin="round"><use href="{}"></use></svg>',
        classes,
        url,
    )
