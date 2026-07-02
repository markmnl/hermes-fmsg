"""TokenManager: exchange, caching, refresh window, auth failure."""

from datetime import datetime, timedelta, timezone

import pytest

from plugin.fmsg_client import FmsgAuthError, TokenManager
from tests.conftest import API_KEY, BOT_ADDRESS


async def test_exchanges_key_and_exposes_address(token_manager, fake_api):
    token = await token_manager.get_token()
    assert token == fake_api.issued_tokens[-1]
    assert token_manager.address == BOT_ADDRESS
    assert fake_api.token_exchanges == 1


async def test_caches_token_until_refresh_window(token_manager, fake_api):
    t1 = await token_manager.get_token()
    t2 = await token_manager.get_token()
    assert t1 == t2
    assert fake_api.token_exchanges == 1


async def test_refreshes_within_five_minutes_of_expiry(token_manager, fake_api):
    await token_manager.get_token()
    token_manager._expires_at = datetime.now(timezone.utc) + timedelta(minutes=4)
    await token_manager.get_token()
    assert fake_api.token_exchanges == 2


async def test_force_refresh(token_manager, fake_api):
    await token_manager.get_token()
    await token_manager.get_token(force=True)
    assert fake_api.token_exchanges == 2


async def test_bad_key_raises_auth_error(fake_api, http_client):
    tm = TokenManager("http://fmsg.test", "fmsgk_wrong_key", http=http_client)
    with pytest.raises(FmsgAuthError):
        await tm.get_token()
