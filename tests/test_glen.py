"""Token-gated (GLEN) tools, using recorded responses. The tools are registered on a
separate server instance because the shared one is built without a token."""

import json

import httpx
import pytest
import respx
from conftest import fixture_bytes

from le_bills_mcp import server
from le_bills_mcp.workspace import decode_xml, render

TOKEN = "TESTTOKEN"
GLEN = f"https://glen.le.utah.gov/{{}}/{TOKEN}"
ROUTES = {
    "code/53F-2-301": "glen/code_53F-2-301.xml",
    "const/A6%20S2": "glen/const_A6_S2.xml",
    "rule/JR4-2-101": "glen/rule_JR4-2-101.xml",
    "agencal": "glen/agencal.json",
}


def test_statute_render_labels_and_heading():
    text = render(decode_xml(fixture_bytes("glen/code_53F-2-301.xml")), statute=True)
    assert text.startswith("53F-2-301. Minimum basic tax rate")
    assert "Effective 7/1/2026" in text
    assert "\n(1) As used in this section:" in text
    assert '\n(a) "Basic levy increment rate"' in text


def test_constitution_latin1_and_labels():
    text = render(decode_xml(fixture_bytes("glen/const_A6_S2.xml")), statute=True)
    assert text.startswith("Article VI, Section 2.")
    assert "\n(1) Annual general sessions" in text


@pytest.fixture
def glen(monkeypatch):
    monkeypatch.setattr(server.client, "token", TOKEN)
    with respx.mock(assert_all_called=False) as router:
        for path, name in ROUTES.items():
            router.get(GLEN.format(path)).mock(return_value=httpx.Response(200, content=fixture_bytes(name)))
        server.client.cache.clear()
        yield router


@pytest.mark.parametrize(
    "fn,args",
    [
        (server.get_utah_code, {"citation": "53F-2-301"}),
        (server.get_constitution, {"citation": "A6 S2"}),
        (server.get_legislative_rule, {"citation": "JR4-2-101"}),
        (server.get_meeting_agenda, {}),
    ],
)
async def test_glen_tools_budget_and_no_token_leak(glen, fn, args):
    text = await server.tool(fn)(**args, max_chars=800)
    assert len(text) <= 800
    assert TOKEN not in text
    json.loads(text)


async def test_agenda_is_compact(glen):
    out = json.loads(await server.tool(server.get_meeting_agenda)(max_chars=4000))
    first = out["items"][0]
    assert first["meeting_id"] and first["items"][0].startswith("1. ")
