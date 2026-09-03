"""Contracts for typed, Phase-aware Task Run Evidence."""

import pytest

from machinist.evidence import EvidenceError, TaskEvidence, checkpoint_evidence

SHA_A = "a" * 40
SHA_B = "b" * 40


def test_loaded_evidence_preserves_unknown_legacy_values():
    evidence = TaskEvidence.load({"approved_sha": "legacy", "future": {"v": 2}})

    assert evidence.as_dict() == {
        "approved_sha": "legacy",
        "future": {"v": 2},
    }
    assert evidence.approved_sha is None


def test_checkpoint_rejects_known_evidence_in_the_wrong_phase():
    with pytest.raises(EvidenceError, match="implementation_sha.*Spec"):
        checkpoint_evidence("spec", {}, {"implementation_sha": SHA_A})


def test_checkpoint_rejects_contradictory_push_evidence():
    with pytest.raises(EvidenceError, match="push.*does not match"):
        checkpoint_evidence(
            "execute",
            {"implementation_sha": SHA_A, "push_intended_sha": SHA_A},
            {"push_observed_sha": SHA_B},
        )


def test_spec_delivery_sha_requires_consistent_recovery_evidence():
    complete = TaskEvidence.load(
        {
            "spec_sha": SHA_A,
            "push_intended_sha": SHA_A,
            "push_observed_sha": SHA_A,
        }
    )
    contradictory = TaskEvidence.load(
        {
            "spec_sha": SHA_A,
            "push_intended_sha": SHA_A,
            "push_observed_sha": SHA_B,
        }
    )

    assert complete.spec_delivery_sha() == SHA_A
    assert contradictory.spec_delivery_sha() is None


def test_pr_base_rejects_unsafe_loaded_value_when_interpreted():
    evidence = TaskEvidence.load({"pr_base": "main\n--repo=attacker/other"})

    with pytest.raises(EvidenceError, match="invalid PR base"):
        evidence.pr_base()


def test_removed_keys_in_historical_records_stay_readable():
    from machinist.evidence import TaskEvidence

    legacy = {
        "ready_intended_sha": "c" * 40,
        "ready_observed_sha": "c" * 40,
        "spec_recovery": "delivery-only",
        "spec_sha": "d" * 40,
    }

    assert TaskEvidence.load(legacy).spec_sha == "d" * 40
