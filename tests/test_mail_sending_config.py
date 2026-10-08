"""Sending policy is finite and remains inert without an explicit drain."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from signalnest.config import ConfigurationError, MailSendingSettings, load_config

ROOT = Path(__file__).resolve().parents[1]


def test_default_sending_policy():
    policy = MailSendingSettings()
    assert policy.max_attempts == 6
    assert policy.max_messages == 5
    assert policy.run_seconds == 300.0
    assert policy.retry_delays_seconds == [300, 900, 3600, 21600, 86400]
    assert policy.uncertain_delay_seconds == 1800


def test_reduced_attempt_limit_does_not_require_rewriting_default_backoff():
    policy = MailSendingSettings(max_attempts=2)
    assert policy.max_attempts == 2 and len(policy.retry_delays_seconds) == 5
    once = MailSendingSettings(max_attempts=1, retry_delays_seconds=[])
    assert once.retry_delays_seconds == []


@pytest.mark.parametrize(
    "changes",
    [
        {"max_attempts": 0},
        {"max_attempts": 7},
        {"max_attempts": True},
        {"max_attempts": "6"},
        {"max_messages": 0},
        {"max_messages": 21},
        {"max_messages": 5.0},
        {"max_messages": "5"},
        {"run_seconds": 0.0},
        {"run_seconds": 3600.1},
        {"run_seconds": float("nan")},
        {"run_seconds": float("inf")},
        {"run_seconds": "300.0"},
        {"run_seconds": True},
        {"uncertain_delay_seconds": 1799},
        {"uncertain_delay_seconds": 86401},
        {"uncertain_delay_seconds": "1800"},
        {"retry_delays_seconds": []},
        {"retry_delays_seconds": [1, 2, 3, 4]},
        {"retry_delays_seconds": [1, 2, 3, 4, 0]},
        {"retry_delays_seconds": [1, 2, 3, 4, -1]},
        {"retry_delays_seconds": [1, 2, 3, 4, 31536001]},
        {"retry_delays_seconds": [1, 2, 3, 4, True]},
        {"retry_delays_seconds": [1, 2, 3, 4, "5"]},
        {"retry_delays_seconds": [5, 4, 3, 2, 1]},
        {"retry_delays_seconds": (1, 2, 3, 4, 5)},
        {"retry_forever": True},
    ],
)
def test_invalid_sending_policy(changes):
    with pytest.raises(ValidationError):
        MailSendingSettings.model_validate(changes)


def test_old_config_has_default_policy_and_no_smtp(tmp_path):
    example = (ROOT / "config.example.toml").read_text()
    before, remainder = example.split("[mail_sending]", 1)
    _, after = remainder.split("[runtime.full]", 1)
    config = tmp_path / "signalnest.toml"
    config.write_text(before + "[runtime.full]" + after)
    settings = load_config(config)
    assert settings.mail_sending == MailSendingSettings() and settings.smtp is None
    assert list(tmp_path.iterdir()) == [config]


def test_bad_sending_policy_error_does_not_echo_input(tmp_path):
    config = tmp_path / "signalnest.toml"
    config.write_text(
        (ROOT / "config.example.toml")
        .read_text()
        .replace("max_attempts = 6", 'max_attempts = "private-invalid-limit"')
    )
    with pytest.raises(ConfigurationError, match="mail_sending.max_attempts") as error:
        load_config(config)
    assert "private-invalid-limit" not in str(error.value)
    assert list(tmp_path.iterdir()) == [config]
