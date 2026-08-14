"""Ports defining contracts implemented by external providers and engines."""

from .errors import (
    ProviderAuthenticationError,
    ProviderCapabilityError,
    ProviderDataError,
    ProviderError,
    ProviderErrorCode,
    ProviderMappingError,
    ProviderRateLimitError,
    ProviderUnavailableError,
)
from .optimisation import OptimisationEngine
from .providers import (
    LeagueProvider,
    ManagerStateProvider,
    NewsEvidenceProvider,
    PlayerDataProvider,
    ProjectionProvider,
)
from .types import (
    Freshness,
    ProviderCapability,
    ProviderDescriptor,
    ProviderProvenance,
    ProviderResponse,
)

__all__ = [
    "Freshness",
    "LeagueProvider",
    "ManagerStateProvider",
    "NewsEvidenceProvider",
    "OptimisationEngine",
    "PlayerDataProvider",
    "ProjectionProvider",
    "ProviderAuthenticationError",
    "ProviderCapability",
    "ProviderCapabilityError",
    "ProviderDataError",
    "ProviderDescriptor",
    "ProviderError",
    "ProviderErrorCode",
    "ProviderMappingError",
    "ProviderProvenance",
    "ProviderRateLimitError",
    "ProviderResponse",
    "ProviderUnavailableError",
]
