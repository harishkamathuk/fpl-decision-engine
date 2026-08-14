"""Shared base types for the domain model."""

from pydantic import BaseModel, ConfigDict


class DomainModel(BaseModel):
    """Base model for immutable domain objects."""

    model_config = ConfigDict(frozen=True, extra="forbid")
