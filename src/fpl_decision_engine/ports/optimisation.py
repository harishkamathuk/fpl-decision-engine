"""Port for replaceable optimisation engines."""

from __future__ import annotations

from typing import Protocol, TypeVar, runtime_checkable


RequestT_contra = TypeVar("RequestT_contra", contravariant=True)
ResultT_co = TypeVar("ResultT_co", covariant=True)


@runtime_checkable
class OptimisationEngine(Protocol[RequestT_contra, ResultT_co]):
    @property
    def engine_id(self) -> str: ...

    def optimise(self, request: RequestT_contra) -> ResultT_co: ...
