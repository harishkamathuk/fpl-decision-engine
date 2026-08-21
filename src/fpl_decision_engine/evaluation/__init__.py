"""Leakage-safe evaluation of frozen gameweek decisions."""

from .contracts import (
    BaselineEvaluation,
    ComparisonSection,
    DecisionEvaluationV1,
    ForecastObservation,
    FrozenInputProvenance,
    HumanDecisionEvaluation,
    ScenarioEvaluation,
    ValidationSection,
)
from .evaluator import (
    LeakageError,
    MissingFrozenScoreError,
    MissingOutcomeError,
    evaluate_decision,
)
from .outcome import CandidateOutcome, OutcomeEvidenceV1

__all__ = [
    "BaselineEvaluation",
    "CandidateOutcome",
    "ComparisonSection",
    "DecisionEvaluationV1",
    "ForecastObservation",
    "FrozenInputProvenance",
    "HumanDecisionEvaluation",
    "LeakageError",
    "MissingFrozenScoreError",
    "MissingOutcomeError",
    "OutcomeEvidenceV1",
    "ScenarioEvaluation",
    "ValidationSection",
    "evaluate_decision",
]
