import copy

import httpx
import respx

from le_bills_mcp.client import LeClient
from le_bills_mcp.search import BillIndex, index_record, search


def _bill(hb1, number, title, general):
    b = copy.deepcopy(hb1)
    b.update(billNumber=number, shortTitle=title, generalProvisions=general, highlightedProvisions="")
    return b


def _data(hb1):
    bills = {
        "HB0001": index_record(hb1, "t1"),
        "HB0002": index_record(_bill(hb1, "HB0002", "Firearm Storage Amendments", "This bill modifies firearm storage."), "t1"),
        "SB0003": index_record(_bill(hb1, "SB0003", "Property Tax Changes", "This bill changes property tax rates for schools."), "t1"),
    }
    return {"bills": bills, "requests": [{"tracking_id": "X", "title": "Firearm Safety Courses", "sponsor": "ABC"}]}


def test_ranking(hb1):
    data = _data(hb1)
    assert search(data, "firearm")[0]["bill"] == "HB0002"
    assert search(data, "education budget")[0]["bill"] == "HB0001"
    assert search(data, "property tax")[0]["bill"] == "SB0003"
    assert search(data, "zebra") == []
    assert any(r.get("type") == "unnumbered request" for r in search(data, "firearm", include_requests=True))


def test_filters(hb1):
    data = _data(hb1)
    assert {r["bill"] for r in search(data, "", sponsor="whyte")} == {"HB0001", "HB0002", "SB0003"}
    assert search(data, "", status="vetoed") == []


@respx.mock
async def test_incremental_build(hb1, tmp_path):
    billlist = [{"number": "HB0001", "updatetime": "t1"}, {"number": "HB0002", "updatetime": "t1"}]
    respx.get("https://le.utah.gov/data/2026GS/billlist.json").mock(side_effect=lambda r: httpx.Response(200, json=billlist))
    respx.get("https://le.utah.gov/data/2026GS/passedlist.json").mock(return_value=httpx.Response(200, json={"passedbills": []}))
    respx.get("https://le.utah.gov/data/2026GS/filelist.json").mock(return_value=httpx.Response(200, json=[]))
    r1 = respx.get("https://le.utah.gov/data/2026GS/HB0001.json").mock(return_value=httpx.Response(200, json=hb1))
    r2 = respx.get("https://le.utah.gov/data/2026GS/HB0002.json").mock(
        return_value=httpx.Response(200, json=_bill(hb1, "HB0002", "Firearm Storage", "x"))
    )
    client = LeClient()
    idx = BillIndex(client, tmp_path, interval=0)
    data = await idx.ensure("2026GS", wait=True)
    assert set(data["bills"]) == {"HB0001", "HB0002"} and r1.call_count == 1 and r2.call_count == 1

    # Nothing changed -> no per-bill fetches; HB0002 changed -> only it is refetched.
    client.cache.clear()
    idx._checked.clear()
    await idx.ensure("2026GS", wait=True)
    assert r1.call_count == 1 and r2.call_count == 1
    billlist[1]["updatetime"] = "t2"
    client.cache.clear()
    idx._checked.clear()
    await idx.ensure("2026GS", wait=True)
    assert r1.call_count == 1 and r2.call_count == 2

    # Persisted to disk and readable by a fresh index.
    assert set(BillIndex(client, tmp_path).cached("2026GS")["bills"]) == {"HB0001", "HB0002"}


@respx.mock
async def test_rate_limit_backoff(hb1, tmp_path, monkeypatch):
    import le_bills_mcp.search as search_mod

    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr(search_mod.asyncio, "sleep", fake_sleep)
    respx.get("https://le.utah.gov/data/2026GS/billlist.json").mock(
        return_value=httpx.Response(200, json=[{"number": "HB0001", "updatetime": "t1"}])
    )
    respx.get("https://le.utah.gov/data/2026GS/passedlist.json").mock(return_value=httpx.Response(404))
    respx.get("https://le.utah.gov/data/2026GS/filelist.json").mock(return_value=httpx.Response(404))
    respx.get("https://le.utah.gov/data/2026GS/HB0001.json").mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": "3"}), httpx.Response(200, json=hb1)]
    )
    idx = BillIndex(LeClient(), tmp_path, interval=0)
    data = await idx.ensure("2026GS", wait=True)
    assert "HB0001" in data["bills"] and sleeps[0] >= 3


@respx.mock
async def test_memory_only_index_writes_nothing(hb1, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    respx.get("https://le.utah.gov/data/2026GS/billlist.json").mock(
        return_value=httpx.Response(200, json=[{"number": "HB0001", "updatetime": "t1"}])
    )
    respx.get("https://le.utah.gov/data/2026GS/passedlist.json").mock(return_value=httpx.Response(404))
    respx.get("https://le.utah.gov/data/2026GS/filelist.json").mock(return_value=httpx.Response(404))
    respx.get("https://le.utah.gov/data/2026GS/HB0001.json").mock(return_value=httpx.Response(200, json=hb1))
    idx = BillIndex(LeClient(), None, interval=0)
    data = await idx.ensure("2026GS", wait=True)
    assert "HB0001" in data["bills"]
    assert idx.status("2026GS")["persisted"] is False
    assert list(tmp_path.iterdir()) == []
