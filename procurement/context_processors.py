from django.conf import settings

from .i18n import DEFAULT_LANG, get_strings


def tenant(request):
    lang = request.session.get('lang', DEFAULT_LANG) if hasattr(request, 'session') else DEFAULT_LANG
    return {
        'tenant_name': settings.TENANT_NAME,
        'ui': get_strings(lang),
        'current_lang': lang,
    }
