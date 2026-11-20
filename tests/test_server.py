"""End-to-end tool calls against fixture data, checking the context budget is honoured."""

import json

import httpx
import pytest
import respx
from conftest import fixture_bytes
from mcp.server.mcpserver.exceptions import ToolError

from le_bills_mcp import server

DATA = "https://le.utah.gov/data/"
ROUTES = {
    DATA + "2026GS/billlist.json": "data_2026GS_billlist.json",
    DATA + "2026GS/HB0001.json": "data_2026GS_HB0001.json",
    DATA + "2026GS/passedlist.json": "data_2026GS_passedlist.json",
    DATA + "2026GS/filelist.json": "data_2027GS_filelist.json",
    DATA + "legislators.json": "data_legislators.json",
    DATA + "committees.json": "data_committees.json",
    DATA + "legcal.json": "data_legcal.json",
    "https://le.utah.gov/Session/2026/bills/enrolled/HB0001.xml": "HB0001.xml",
    "https://le.utah.gov/Session/2026/bills/introduced/HB0001.xml": "HB0001.xml",
    "https://pf.utleg.gov/public-web/sessions/2026GS/fiscal-notes/HB0001.fn.html": "HB0001.fn.html",
}

BUDGET = 800
S = {"session": "2026"}
CALLS = [
    ("get_bill", {"bill": "HB 1", "detail": "full", **S}),
    ("get_bill_history", {"bill": "HB1", "full_history": True, **S}),
    ("get_bill_documents", {"bill": "HB1", **S}),
    ("read_bill_text", {"bill": "HB1", **S}),
    ("read_bill_text", {"bill": "HB1", "section": "S6", "offset": 5000, **S}),
    ("find_in_bill", {"bill": "HB1", "pattern": "fund", "max_hits": 50, **S}),
    ("get_fiscal_note", {"bill": "HB1", **S}),
    ("list_bills", {"limit": 50, **S}),
    ("list_passed_bills", {"limit": 50, **S}),
    ("list_bill_requests", {"limit": 50, **S}),
    ("list_legislators", {"detail": "full", "limit": 50}),
    ("get_legislator", {"who": "PETERT", "detail": "full"}),
    ("list_committees", {"limit": 50}),
    ("get_committee", {"committee": "INTBUS"}),
    ("get_meeting_calendar", {"limit": 50}),
]


@pytest.fixture
def mocked():
    with respx.mock(assert_all_called=False) as router:
        for url, name in ROUTES.items():
            router.get(url).mock(return_value=httpx.Response(200, content=fixture_bytes(name)))
        server.client.cache.clear()
        yield router


async def _call(name: str, args: dict) -> str:
    result = await server.mcp.call_tool(name, args)
    return result.content[0].text


@pytest.mark.parametrize("name,args", CALLS, ids=[f"{n}-{i}" for i, (n, _) in enumerate(CALLS)])
async def test_tool_respects_budget(mocked, name, args):
    text = await _call(name, {**args, "max_chars": BUDGET})
    assert len(text) <= BUDGET, f"{name} returned {len(text)} chars"
    json.loads(text)


async def test_load_bill_returns_toc_only(mocked):
    toc = json.loads(await _call("load_bill", {"bill": "HB1", **S}))
    assert toc["version"] == "Enrolled"
    assert toc["total_chars"] > 50000
    assert len(json.dumps(toc)) < 5000
    loaded = json.loads(await _call("list_loaded_bills", {}))
    assert loaded["loaded"][0]["bill"] == "HB0001"


async def test_paging_reads_whole_section(mocked):
    offset, chunks = 0, []
    while True:
        page = json.loads(await _call("read_bill_text", {"bill": "HB1", "section": "53F-2-301", "offset": offset, "max_chars": 1000, **S}))
        chunks.append(page["text"])
        if "next_offset" not in page:
            break
        offset = page["next_offset"]
    assert len(chunks) > 1
    assert "".join(chunks).startswith("Section 1.")


async def test_errors_are_clean(mocked):
    mocked.get(DATA + "2026GS/HB0999.json").mock(return_value=httpx.Response(404))
    # The protocol layer turns ToolError into an is_error result for the client.
    with pytest.raises(ToolError, match="not found"):
        await server.mcp.call_tool("get_bill", {"bill": "HB 999", **S})
    with pytest.raises(ToolError, match="Sections:"):
        await server.mcp.call_tool("read_bill_text", {"bill": "HB1", "section": "zzz", **S})


async def test_glen_tools_hidden_without_token():
    names = {t.name for t in await server.mcp.list_tools()}
    assert "get_bill" in names and "get_utah_code" not in names
