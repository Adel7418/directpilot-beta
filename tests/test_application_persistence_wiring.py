from __future__ import annotations

import pytest

from app.bootstrap.dependencies import create_application_dependencies
from app.db.engine import DatabaseConfigurationError


def test_production_dependencies_fail_closed_without_postgresql(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DIRECTPILOT_APP_ENV", "production")
    monkeypatch.delenv("DIRECTPILOT_DATABASE_URL", raising=False)

    with pytest.raises(DatabaseConfigurationError, match="database configuration is required"):
        create_application_dependencies()
