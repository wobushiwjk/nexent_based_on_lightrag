"""Development-time helpers for the W7 teacher-student pipeline.

Nothing in this package is imported by the LightRAG runtime.  The modules are
pure-Python data/label utilities so they can be tested without a GPU, torch,
or a network connection.
"""

from lightrag.fault_tree.distillation.labels import (
    StudentLabelError,
    validate_student_target,
)
from lightrag.fault_tree.distillation.prompts import (
    build_system_prompt,
    build_user_prompt,
    load_prompt_profile,
)

__all__ = [
    "StudentLabelError",
    "build_system_prompt",
    "build_user_prompt",
    "load_prompt_profile",
    "validate_student_target",
]
