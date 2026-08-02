from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect

from accounts.models import User

# Where each role belongs after logging in, or when they hit the site root.
# Lives in config/ rather than in an app: routing *between* apps by role is a
# project concern, and putting it in accounts would have accounts reversing
# servicing's URLs (ADR-007 keeps that dependency pointing the other way).
ROLE_HOME = {
    User.Role.CLIENT: "servicing:my_requests",
    User.Role.PERSONNEL: "servicing:my_assignments",
    User.Role.COORDINATOR: "admin:servicing_servicerequest_changelist",
}

DEFAULT_HOME = "servicing:my_requests"


@login_required
def home(request):
    """Send each role to its own front door.

    A single static LOGIN_REDIRECT_URL cannot do this: it pointed everyone at
    the client request list, so personnel logged in and were met with a 403
    from the client-only role check.

    Anonymous visitors are sent to the login page by @login_required, which is
    what makes this safe to use as the site root.
    """
    return redirect(ROLE_HOME.get(request.user.role, DEFAULT_HOME))
