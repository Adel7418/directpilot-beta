from app.config import Settings, mask_secret


def test_settings_default_to_mock_mode():
    settings = Settings(_env_file=None)

    assert settings.directpilot_mode == "mock"
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
