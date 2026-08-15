"""Port for replaceable optimisation engines."""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol, runtime_checkable

from fpl_decision_engine.domain import OptimisationDiagnostic


class OptimisationErrorCode(StrEnum):
    """Stable failure categories exposed independently of any solver library."""

    INVALID_INPUT = "invalid_input"
    INFEASIBLE = "infeasible"
    SOLVER_FAILURE = "solver_failure"


class OptimisationError(RuntimeError):
    """Project-owned optimisation failure with structured diagnostic context."""

    def __init__(
        self,
        message: str,
        *,
        code: OptimisationErrorCode,
        diagnostics: tuple[OptimisationDiagnostic, ...] = (),
        solver_status: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.diagnostics = diagnostics
        self.solver_status = solver_status


@runtime_checkable
class OptimisationEngine[RequestT, ResultT](Protocol):
    @property
    def engine_id(self) -> str: ...

    def optimise(self, request: RequestT) -> ResultT: ...
