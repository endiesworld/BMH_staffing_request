from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.shortcuts import redirect, render

from accounts.decorators import role_required
from accounts.models import User

from . import services
from .forms import ServiceRequestForm
from .models import ServiceRequest

PAGE_SIZE = 20


@login_required
@role_required(User.Role.CLIENT, "Only clients can use the client pages.")
def submit_request(request):
    """The client's entry point: raise a request, get told it is under review.

    Handles both halves of the round trip -- GET renders an empty form, POST
    validates and saves. `services.submit_request` (module-qualified, not this
    function) is what actually writes the row.
    """
    if request.method == "POST":
        # A *bound* form: it has the posted data, so it can validate.
        form = ServiceRequestForm(data=request.POST)

        if form.is_valid():
            try:
                # cleaned_data holds converted Python objects -- request_type
                # is a RequestType instance here, not the posted "5", and
                # expected_duration is a timedelta, not "120".
                #
                # Splatted because the form's field names deliberately mirror
                # submit_request()'s keyword arguments. If the two ever drift
                # apart this raises TypeError immediately, which is the failure
                # you want: loud, and at the call site.
                services.submit_request(client=request.user, **form.cleaned_data)
            except ValueError as exc:
                # Practically unreachable from this form: ModelChoiceField
                # re-runs its queryset (filtered to is_active) when cleaning,
                # so a retired type is rejected during validation. Kept because
                # the service -- not the form -- is the authority on that rule.
                form.add_error("request_type", str(exc))
            else:
                # ADR-011 D1: the acknowledgement is the response itself.
                # Notifications are for outcomes the client cannot see now.
                messages.success(
                    request,
                    "Request submitted and currently under review. "
                    "We will let you know the outcome.",
                )
                # Redirect rather than render, so a refresh cannot resubmit
                # (post/redirect/get).
                return redirect("servicing:my_requests")
    else:
        # An *unbound* form: fields but no data, purely to render as HTML.
        form = ServiceRequestForm()

    # Reached on GET, and on a POST whose form was invalid -- in which case
    # `form` is bound and carries .errors, which the template displays.
    return render(request, "servicing/submit_request.html", {"form": form})


@login_required
@role_required(User.Role.CLIENT, "Only clients can use the client pages.")
def my_requests(request):
    """Where the client watches the workflow happen to their requests."""
    # Ownership is enforced by what enters the queryset, not by a permission:
    # Django permissions are per-model, so "may view their own requests" is not
    # expressible as one. Other clients' rows never load, so there is nothing
    # to forbid.
    service_requests = (
        ServiceRequest.objects
        .filter(client=request.user)
        .select_related("request_type")
    )

    # get_page() is the forgiving variant: a missing, non-numeric or
    # out-of-range ?page= gives a valid page instead of raising.
    paginator = Paginator(service_requests, PAGE_SIZE)
    page_obj = paginator.get_page(request.GET.get("page"))

    context = {
        # Iterating a page yields only that page's rows.
        "service_requests": page_obj,
        "page_obj": page_obj,
        "is_paginated": page_obj.has_other_pages(),
    }
    return render(request, "servicing/my_requests.html", context)
