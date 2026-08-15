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
from .persistence import (
    CanonicalDatasetName,
    CanonicalRepository,
    CanonicalSnapshot,
    DatasetArtifact,
    DecisionRunRepository,
    ImmutableRegistrationConflict,
    PersistenceError,
    SnapshotCatalog,
    SnapshotRegistration,
    SourceObjectHash,
    UnsupportedSchemaVersion,
)
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
    "CanonicalDatasetName",
    "CanonicalRepository",
    "CanonicalSnapshot",
    "DatasetArtifact",
    "DecisionRunRepository",
    "ImmutableRegistrationConflict",
    "PersistenceError",
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
    "SnapshotCatalog",
    "SnapshotRegistration",
    "SourceObjectHash",
    "UnsupportedSchemaVersion",
]
