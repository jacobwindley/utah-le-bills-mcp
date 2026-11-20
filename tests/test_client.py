import httpx
import pytest
import respx

from le_bills_mcp.client import LeClient, NotFound, RateLimited, TokenRequired, UpstreamError


@respx.mock
async def test_cache_and_bom():
    route = respx.get("https://le.utah.gov/data/legislators.json").mock(
        return_value=httpx.Response(200, content=b'\xef\xbb\xbf{"legislators": []}')
    )
    c = LeClient()
    assert await c.public_json("legislators.json", 60) == {"legislators": []}
    await c.public_json("legislators.json", 60)
    assert route.call_count == 1


@respx.mock
async def test_errors():
    respx.get("https://le.utah.gov/data/2026GS/HB9999.json").mock(return_value=httpx.Response(404))
    respx.get("https://le.utah.gov/data/2026GS/HB0001.json").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "7"})
    )
    c = LeClient()
    with pytest.raises(NotFound):
        await c.bill("2026GS", "HB9999")
    with pytest.raises(RateLimited) as exc:
        await c.bill("2026GS", "HB0001")
    assert exc.value.retry_after == 7


@respx.mock
async def test_glen_token_redacted():
    respx.get(url__startswith="https://glen.le.utah.gov/").mock(return_value=httpx.Response(500))
    c = LeClient(token="SECRET123")
    with pytest.raises(UpstreamError) as exc:
        await c.glen("code/53F", 60)
    assert "SECRET123" not in str(exc.value) and "***" in str(exc.value)
    with pytest.raises(TokenRequired):
        await LeClient().glen("code/53F", 60)


@respx.mock
async def test_default_session_falls_back(monkeypatch):
    import le_bills_mcp.client as client_mod

    monkeypatch.setattr(client_mod, "candidate_default_sessions", lambda: ["2027GS", "2026GS"])
    respx.get("https://le.utah.gov/data/2027GS/billlist.json").mock(return_value=httpx.Response(404))
    respx.get("https://le.utah.gov/data/2026GS/billlist.json").mock(return_value=httpx.Response(200, json=[]))
    assert await LeClient().resolve_session(None) == "2026GS"
