from app.config import Settings, mask_secret


def test_settings_default_to_live_readonly_mode():
    # Default mode is live-first: the product path is live_readonly with
    # active testing against real Yandex Direct data, and writes only
    # possible through the gated live_write mode. mock/sandbox are kept
    # in the codebase as a legacy/dev fallback and must be selected
    # explicitly via the DIRECTPILOT_MODE env var.
    settings = Settings(
        _env_file=None,
        yandex_client_id=None,
        yandex_client_secret=None,
        yandex_oauth_token=None,
    )

    assert settings.directpilot_mode == "live_readonly"
    assert settings.is_yandex_configured is False


def test_settings_detect_yandex_credentials():
    settings = Settings(
        yandex_client_id="client-id",
        yandex_client_secret="client-secret",
        yandex_oauth_token="token",
    )

    assert settings.is_yandex_configured is True


def test_mask_secret_never_exposes_full_value():
    masked = mask_secret("abcdefghijklmnopqrstuvwxyz")

    assert masked == "abcd…wxyz(len=26)"
    assert "abcdefghijkl" not in masked
