"""Who can fulfil a request (ADR-008).

Rule-based and synchronous for this phase -- it is a millisecond database
filter, not slow external I/O, so forcing it through Celery would add a broker
round trip and worse UX for nothing. Written behind an interface (Strategy
pattern) so an MLRecommender can replace it without touching call sites, and
as a pure function of its arguments so it can become a Celery task unchanged
if it ever grows heavy.
"""

from abc import ABC, abstractmethod

from accounts.models import PersonnelProfile

from .models import Assignment


class RecommendationEngine(ABC):
    """The seam. Call sites depend on this, never on a concrete recommender."""

    @abstractmethod
    def recommend(self, service_request):
        """Return a queryset of PersonnelProfile eligible for this request."""


class RuleBasedRecommender(RecommendationEngine):
    """Three rules, all expressible as a query (brief section 3: eligibility is
    a query, not a state).

    1. Sector must match what the request type needs. This is why
       RequestType.required_sector reuses PersonnelProfile.SectorCategory --
       if the two vocabularies drifted, this filter would silently match
       nothing.
    2. They must have opted in to being available. New registrations are
       UNAVAILABLE (ADR-009 D3), so registering does not make you assignable.
    3. They must not already have declined this request, or the reassign loop
       would hand it straight back to someone who said no.
    """

    def recommend(self, service_request):
        already_declined = Assignment.objects.filter(
            service_request=service_request,
            status=Assignment.Status.DECLINED,
        ).values("personnel_id")

        return (
            PersonnelProfile.objects.filter(
                sector=service_request.request_type.required_sector,
                availability_status=PersonnelProfile.AvailabilityStatus.AVAILABLE,
            )
            .exclude(user_id__in=already_declined)
            .select_related("user")
            .order_by("user__email")
        )


def get_recommender():
    """Single place to swap the strategy (settings-driven later, if wanted)."""
    return RuleBasedRecommender()


def recommend_personnel(service_request):
    """Convenience wrapper -- what callers actually use."""
    return get_recommender().recommend(service_request)
