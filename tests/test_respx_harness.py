import httpx
import pytest
import respx
from respx.models import AllMockedAssertionError


def test_respx_blocks_unmocked_provider_network_paths() -> None:
    with respx.mock(assert_all_called=True, assert_all_mocked=True) as mock:
        mock.get("https://provider.invalid/contract").respond(200, json={"ok": True})

        response = httpx.get("https://provider.invalid/contract")

        with pytest.raises(AllMockedAssertionError):
            httpx.get("https://provider.invalid/unmocked")

    assert response.status_code == 200
    assert response.json() == {"ok": True}
