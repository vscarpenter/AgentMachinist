"""Typed interpretation and Phase-aware validation for Task Run Evidence.

Persistence deliberately remains an open JSON mapping so existing Task Run files
stay readable. This module owns the vocabulary and relationships of fields the
controller itself understands; unknown historical fields pass through unchanged.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

type EvidenceValue = (
    str | int | float | bool | None | list[EvidenceValue] | dict[str, EvidenceValue]
)
type Evidence = dict[str, EvidenceValue]

_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
_PHASES = frozenset({"spec", "execute", "review"})
_PHASE_FIELDS = {
    "spec_sha": frozenset({"spec"}),
    "spec_recovery": frozenset({"spec"}),
    "approved_sha": frozenset({"execute"}),
    "implementation_sha": frozenset({"execute"}),
    "harness_completed": frozenset({"execute"}),
    "feedback_supplied": frozenset({"execute"}),
    "feedback_characters": frozenset({"execute"}),
    "change_summary": frozenset({"execute"}),
    "verification_report": frozenset({"execute"}),
    "verification_log_dir": frozenset({"execute"}),
    "review_required_sha": frozenset({"execute"}),
    "ready_intended_sha": frozenset({"execute"}),
    "reviewed_sha": frozenset({"review"}),
    "review_report": frozenset({"review"}),
    "review_comment_id": frozenset({"review"}),
}
_SHA_FIELDS = frozenset(
    {
        "spec_sha",
        "approved_sha",
        "implementation_sha",
        "push_intended_sha",
        "push_observed_sha",
        "review_required_sha",
        "reviewed_sha",
        "ready_intended_sha",
        "ready_observed_sha",
    }
)
_POSITIVE_INTEGER_FIELDS = frozenset(
    {"pr_number", "review_comment_id", "completion_comment_id"}
)
_NONNEGATIVE_INTEGER_FIELDS = frozenset({"feedback_characters"})
_BOOLEAN_FIELDS = frozenset(
    {"harness_completed", "feedback_supplied", "cleanup_succeeded"}
)
_MAPPING_FIELDS = frozenset(
    {"harness", "usage", "change_summary", "verification_report", "review_report"}
)


class EvidenceError(ValueError):
    """Known Task Run Evidence is malformed or internally inconsistent."""


@dataclass(frozen=True)
class TaskEvidence:
    """Read-only typed view over one persisted Evidence mapping."""

    _values: Evidence

    @classmethod
    def load(cls, value: object) -> TaskEvidence:
        """Read JSON-safe historical Evidence without closing its schema."""
        return cls(validate_evidence(value))

    def as_dict(self) -> Evidence:
        return validate_evidence(self._values)

    @property
    def spec_sha(self) -> str | None:
        return self._sha("spec_sha")

    @property
    def approved_sha(self) -> str | None:
        return self._sha("approved_sha")

    @property
    def implementation_sha(self) -> str | None:
        return self._sha("implementation_sha")

    @property
    def pushed_sha(self) -> str | None:
        return self._sha("push_observed_sha")

    @property
    def intended_push_sha(self) -> str | None:
        return self._sha("push_intended_sha")

    @property
    def current_stage(self) -> str | None:
        return self._string("current_stage")

    @property
    def workspace_path(self) -> str | None:
        return self._string("workspace_path")

    @property
    def harness_report_excerpt(self) -> str | None:
        return self._string("harness_report_excerpt")

    @property
    def harness_completed(self) -> bool:
        return self._values.get("harness_completed") is True

    @property
    def pr_number(self) -> int | None:
        return self._positive_integer("pr_number")

    @property
    def review_comment_id(self) -> int | None:
        return self._positive_integer("review_comment_id")

    @property
    def completion_comment_id(self) -> int | None:
        return self._positive_integer("completion_comment_id")

    @property
    def feedback_supplied(self) -> bool:
        return self._values.get("feedback_supplied") is True

    @property
    def feedback_characters(self) -> int:
        value = self._values.get("feedback_characters")
        return value if type(value) is int and value >= 0 else 0

    @property
    def prior_workspace_paths(self) -> list[str]:
        value = self._values.get("prior_workspace_paths")
        if not isinstance(value, list):
            return []
        return [item for item in value if isinstance(item, str)]

    @property
    def resume_forbidden_reason(self) -> str | None:
        return self._string("resume_forbidden_reason")

    @property
    def verification_report(self) -> dict[str, EvidenceValue] | None:
        return self._mapping("verification_report")

    @property
    def harness(self) -> dict[str, EvidenceValue] | None:
        return self._mapping("harness")

    @property
    def usage(self) -> dict[str, EvidenceValue] | None:
        return self._mapping("usage")

    @property
    def change_summary(self) -> dict[str, EvidenceValue] | None:
        return self._mapping("change_summary")

    @property
    def git_custody(self) -> dict[str, EvidenceValue] | None:
        return self._mapping("git_custody")

    def pr_base(self) -> str | None:
        value = self._values.get("pr_base")
        if value is None:
            return None
        if not isinstance(value, str) or not _safe_ref(value):
            raise EvidenceError("Task Run Evidence contains an invalid PR base")
        return value

    def spec_delivery_sha(self) -> str | None:
        spec_sha = self.spec_sha
        intended = self.intended_push_sha
        observed = self.pushed_sha
        if spec_sha is None or intended is None or spec_sha != intended:
            return None
        if observed is not None and observed != intended:
            return None
        return intended

    def _sha(self, key: str) -> str | None:
        value = self._values.get(key)
        return value if isinstance(value, str) and _FULL_SHA.fullmatch(value) else None

    def _string(self, key: str) -> str | None:
        value = self._values.get(key)
        return value if isinstance(value, str) else None

    def _positive_integer(self, key: str) -> int | None:
        value = self._values.get(key)
        return value if type(value) is int and value > 0 else None

    def _mapping(self, key: str) -> dict[str, EvidenceValue] | None:
        value = self._values.get(key)
        return validate_evidence(value) if isinstance(value, dict) else None


def checkpoint_evidence(
    phase: str,
    current: Mapping[str, object],
    updates: Mapping[str, object],
) -> Evidence:
    """Validate and merge one controller-owned checkpoint for ``phase``."""
    if phase not in _PHASES:
        raise EvidenceError(f"unknown Task Run Phase {phase!r}")
    existing = validate_evidence(dict(current))
    additions = validate_evidence(dict(updates))
    for key, value in additions.items():
        _validate_phase_field(phase, key)
        _validate_known_value(key, value)
    merged = {**existing, **additions}
    _validate_relationships(phase, merged, frozenset(additions))
    return merged


def validate_evidence(value: object) -> Evidence:
    """Clone one value into the strict JSON-safe Evidence type."""
    if not isinstance(value, dict):
        raise EvidenceError("Task Run evidence must be an object")
    result: Evidence = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise EvidenceError("Task Run evidence keys must be strings")
        result[key] = _evidence_value(item, path=key)
    return result


def _evidence_value(value: object, *, path: str) -> EvidenceValue:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        raise EvidenceError(f"Task Run evidence '{path}' must be finite")
    if isinstance(value, list):
        return [
            _evidence_value(item, path=f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    if isinstance(value, dict):
        result: dict[str, EvidenceValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise EvidenceError(f"Task Run evidence '{path}' keys must be strings")
            result[key] = _evidence_value(item, path=f"{path}.{key}")
        return result
    raise EvidenceError(
        f"Task Run evidence '{path}' has unsupported type {type(value).__name__}"
    )


def _validate_phase_field(phase: str, key: str) -> None:
    owners = _PHASE_FIELDS.get(key)
    if owners is None or phase in owners:
        return
    display = {"spec": "Spec", "execute": "Execute", "review": "Review"}
    expected = " or ".join(display[item] for item in sorted(owners))
    raise EvidenceError(
        f"Task Run Evidence '{key}' belongs to {expected}, not {display[phase]}"
    )


def _validate_known_value(key: str, value: EvidenceValue) -> None:
    if value is None:
        return
    if key in _SHA_FIELDS and not (
        isinstance(value, str) and _FULL_SHA.fullmatch(value)
    ):
        raise EvidenceError(f"Task Run Evidence '{key}' must be a full Git SHA")
    if key in _POSITIVE_INTEGER_FIELDS and not (type(value) is int and value > 0):
        raise EvidenceError(f"Task Run Evidence '{key}' must be a positive integer")
    if key in _NONNEGATIVE_INTEGER_FIELDS and not (type(value) is int and value >= 0):
        raise EvidenceError(f"Task Run Evidence '{key}' must be a non-negative integer")
    if key in _BOOLEAN_FIELDS and not isinstance(value, bool):
        raise EvidenceError(f"Task Run Evidence '{key}' must be a boolean")
    if key in _MAPPING_FIELDS and not isinstance(value, dict):
        raise EvidenceError(f"Task Run Evidence '{key}' must be an object")
    if key == "pr_base" and not (isinstance(value, str) and _safe_ref(value)):
        raise EvidenceError("Task Run Evidence contains an invalid PR base")


def _validate_relationships(
    phase: str, evidence: Evidence, updated: frozenset[str]
) -> None:
    view = TaskEvidence(cast(Evidence, evidence))
    relationships = [("push_intended_sha", "push_observed_sha")]
    if phase == "spec":
        relationships.append(("spec_sha", "push_intended_sha"))
    if phase == "execute":
        relationships.extend(
            [
                ("implementation_sha", "push_intended_sha"),
                ("ready_intended_sha", "ready_observed_sha"),
            ]
        )
    for left_key, right_key in relationships:
        if not updated.intersection({left_key, right_key}):
            continue
        left = view._sha(left_key)
        right = view._sha(right_key)
        if left is not None and right is not None and left != right:
            raise EvidenceError(
                f"Task Run Evidence {right_key} does not match {left_key}"
            )


def _safe_ref(value: str) -> bool:
    return (
        bool(value)
        and value == value.strip()
        and not any(character in value for character in ("\0", "\n", "\r"))
    )
