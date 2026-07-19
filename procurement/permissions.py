from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied


def role_required(*roles):
    """Gate a view to specific User.Role values. Superusers (the seeded
    admin account) always pass — this is a small single-institution
    deployment where admin needs to exercise every screen for setup and
    support. Role checks alone don't guarantee separation of duties (a
    short-staffed unit could assign one person two roles) — the service
    layer's SeparationOfDutiesError is the actual enforcement for that;
    this decorator only keeps people out of screens their role has no
    business seeing at all."""

    def decorator(view_func):
        @wraps(view_func)
        @login_required
        def wrapped(request, *args, **kwargs):
            if request.user.is_superuser or request.user.role in roles:
                return view_func(request, *args, **kwargs)
            raise PermissionDenied('Your role does not have access to this action.')
        return wrapped
    return decorator
