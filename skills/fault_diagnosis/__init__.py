"""Fault-diagnosis skill: equipment + symptom -> ranked, evidenced candidates."""

from __future__ import annotations

from skills.fault_diagnosis.handler import (
    SKILL_NAME,
    SKILL_VERSION,
    DiagnosisRequest,
    DiagnosisResponse,
    run,
)

__all__ = [
    "SKILL_NAME",
    "SKILL_VERSION",
    "DiagnosisRequest",
    "DiagnosisResponse",
    "run",
]
