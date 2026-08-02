from functools import wraps

from django.core.exceptions import PermissionDenied


def role_required(role, message="You do not have access to this page."):
    """Restrict a view to users holding a given User.Role.

    A role check, not a permission check: "is this person a client?" asks who
    they are, not what they may touch. Inventing can_submit_request-style
    permissions would add rows to auth_permission that nothing in Django
    would ever consult.

    Always stack UNDER @login_required:

        @login_required                      <- runs first, redirects anonymous
        @role_required(User.Role.PERSONNEL)  <- so .role is safe to read

    Decorators apply bottom-up, so the one written last wraps innermost and
    runs last. The other order would read .role on an AnonymousUser and raise
    AttributeError before the login redirect ever happened.
    """

    def decorator(view_func):
        @wraps(view_func)
        def wrapper(request, *args, **kwargs):
            if request.user.role != role:
                raise PermissionDenied(message)
            return view_func(request, *args, **kwargs)

        return wrapper

    return decorator
