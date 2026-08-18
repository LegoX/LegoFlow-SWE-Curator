"""Public API for LegoFlow Curator.

This module defines the stable, user-facing imports for programmatic use.
"""

from legoflow_curator.analyze import (
    Classification,
    Subtype,
    TaskVerdict,
    TrialClassification,
    classify_trial,
    compute_task_verdict,
)

__all__ = [
    "Classification",
    "Subtype",
    "TaskVerdict",
    "TrialClassification",
    "classify_trial",
    "compute_task_verdict",
]
