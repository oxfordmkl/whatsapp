"""
Phase 8.2B — Marketing service layer (UNWIRED).

Home for the Phase 8.1A approved service layer: CampaignService (8.2B), and
later BroadcastService / AudienceService / TemplateService / DeliveryTracker.

Why a new package rather than app/services/campaign_service.py: that module is
the LEGACY production engine (thread + sleep, no persistence) and is explicitly
out of scope for modification. Keeping the two side by side under distinct
names lets the new engine be built and tested while the legacy path continues
to serve production untouched, with CAMPAIGN_ENGINE_V2 deciding which is live.

Nothing in this package is imported by production code.
"""
from app.marketing.campaign_service import (
    CampaignService,
    ValidationResult,
    CampaignEngineDisabled,
    CampaignValidationError,
    CampaignTransitionError,
    ALLOWED_TRANSITIONS,
)

__all__ = [
    "CampaignService",
    "ValidationResult",
    "CampaignEngineDisabled",
    "CampaignValidationError",
    "CampaignTransitionError",
    "ALLOWED_TRANSITIONS",
]
