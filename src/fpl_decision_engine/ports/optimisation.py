"""Port for replaceable optimisation engines."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class OptimisationEngine[RequestT, ResultT](Protocol):
    @property
    def engine_id(self) -> str: ...

    def optimise(self, request: RequestT) -> ResultT: ...
