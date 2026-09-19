import pytest
from pydantic import ValidationError

from machinist.config import MachinistConfig


def test_background_is_opt_in_and_bounded():
    policy = MachinistConfig().background
    assert not policy.enabled
    assert policy.timeout_minutes == 30
    assert policy.max_open_prs == 2
    assert policy.required_checks == []


@pytest.mark.parametrize(
    "patch",
    [
        {"enabled": True},
        {"image": "--privileged"},
        {"network": "host"},
        {"timeout_minutes": 0},
        {"max_open_prs": 0},
        {"required_checks": ["CI gate", "CI gate"]},
        {"required_checks": [""]},
        {"queue_label": ""},
    ],
)
def test_background_rejects_invalid_policy(patch):
    with pytest.raises(ValidationError):
        MachinistConfig.model_validate({"background": patch})


def test_background_enabled_requires_image_and_ci_contract():
    config = MachinistConfig.model_validate(
        {
            "background": {
                "enabled": True,
                "image": "machinist-worker:test",
                "required_checks": ["CI gate"],
            }
        }
    )
    assert config.background.queue_label == "machinist:queue"
    assert config.background.network == "bridge"
