"""Lightweight keyword search over bill titles and summaries.

The public billlist has no titles, so we keep a per-session index on disk, built
from the individual bill JSON files and refreshed incrementally using each bill's
`updatetime` from billlist. le.utah.gov rate-limits bursts, so bills are fetched
one at a time with a delay; a full build (~1,000 bills) runs in the background
and searches return partial results (seeded from passedlist titles) meanwhile.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path

from .client import TTL_BILLLIST, LeClient, NotFound, RateLimited
from .formatting import active_version, clean_html

log = logging.getLogger(__name__)

INDEX_VERSION = 1
REQUEST_INTERVAL = 0.6  # seconds between bill fetches while indexing
INLINE_LIMIT = 25  # refresh this many bills inline; more goes to the background
SAVE_EVERY = 50
MAX_BACKOFF = 300.0
_STOPWORDS = {
    "a", "an", "and", "the", "of", "to", "in", "for", "on", "or", "by", "with", "bill", "bills",
    "act", "amendments", "about", "that", "this", "is", "are", "be", "as", "at", "from",
}
# Field weights for scoring.
_WEIGHTS = {"title": 6.0, "subjects": 3.0, "general": 2.0, "highlights": 1.0, "sponsors": 2.0}


def tokenize(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", text.lower()) if len(t) > 1 and t not in _STOPWORDS]


def index_record(bill: dict, updatetime: str) -> dict:
    version = active_version(bill)
    return {
        "bill": bill.get("billNumber"),
        "title": (bill.get("shortTitle") or "").strip(),
        "sponsor": bill.get("primeSponsorName") or "",
        "floor_sponsor": bill.get("floorSponsorName") or "",
        "subjects": [s.get("description") for s in version.get("subjectList") or [] if s.get("description")],
        "general": clean_html(bill.get("generalProvisions"))[:1500],
        "highlights": clean_html(bill.get("highlightedProvisions"))[:2000],
        "status": bill.get("lastAction") or "",
        "status_date": bill.get("lastActionDate") or "",
        "updatetime": updatetime,
    }


def seed_record(passed: dict) -> dict:
    """Title-only record from passedlist.json, used until the full bill is fetched."""
    number = passed.get("number", "")
    return {
        "bill": re.sub(r"S\d+$", "", number),
        "title": (passed.get("title") or "").strip(),
        "sponsor": passed.get("sponsor") or "",
        "floor_sponsor": "",
        "subjects": [],
        "general": "",
        "highlights": "",
        "status": f"Passed; governor action {passed.get('govaction')}" if passed.get("govaction") else "Passed",
        "status_date": (passed.get("datepassed") or "")[:10],
        "updatetime": "",
        "partial": True,
    }


class BillIndex:
    def __init__(self, client: LeClient, cache_dir: Path | None, interval: float = REQUEST_INTERVAL) -> None:
        self.client = client
        # Without a cache dir the index lives in memory only and is rebuilt after a restart.
        self.dir = cache_dir / "index" if cache_dir else None
        self.interval = interval
        self._mem: dict[str, dict] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._checked: dict[str, float] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._pending: dict[str, int] = {}

    def _path(self, session: str) -> Path | None:
        return self.dir / f"{session}.json" if self.dir else None

    def _read(self, session: str) -> dict:
        if session in self._mem:
            return self._mem[session]
        try:
            if (path := self._path(session)) is None:
                raise FileNotFoundError
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("version") != INDEX_VERSION:
                raise ValueError("stale index format")
        except (OSError, ValueError):
            data = {"version": INDEX_VERSION, "session": session, "bills": {}, "requests": []}
        self._mem[session] = data
        return data

    def _write(self, session: str, data: dict) -> None:
        if (path := self._path(session)) is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)

    def cached(self, session: str) -> dict:
        """Whatever is already on disk/in memory, without network access."""
        return self._read(session)

    def status(self, session: str) -> dict:
        data = self._read(session)
        bills = data["bills"].values()
        full = sum(1 for b in bills if not b.get("partial"))
        task = self._tasks.get(session)
        building = task is not None and not task.done()
        out = {
            "indexed_bills": full,
            "title_only_bills": len(data["bills"]) - full,
            "total_bills": data.get("total", len(data["bills"])),
            "building": building,
        }
        if building:
            out["remaining"] = self._pending.get(session, 0)
            out["note"] = (
                "Index is still building in the background (~0.6s per bill); results may be "
                "incomplete. Search again later for full coverage."
            )
        out["persisted"] = self.dir is not None
        return out

    async def ensure(self, session: str, force: bool = False, wait: bool = False) -> dict:
        """Bring the index up to date. Only bills whose updatetime changed are refetched.

        Small deltas are fetched inline; large ones (first build) run in the background
        unless `wait` is set.
        """
        lock = self._locks.setdefault(session, asyncio.Lock())
        async with lock:
            task = self._tasks.get(session)
            if task is not None and not task.done():
                return self._read(session)
            loop = asyncio.get_running_loop()
            last = self._checked.get(session)
            if not force and last is not None and loop.time() - last < TTL_BILLLIST:
                return self._read(session)

            data = self._read(session)
            billlist = await self.client.billlist(session)
            current = {b["number"]: b.get("updatetime") or "" for b in billlist if b.get("number")}
            bills: dict = data["bills"]
            for n in [n for n in bills if n not in current]:
                del bills[n]
            stale = [n for n, ut in current.items() if force or bills.get(n, {}).get("updatetime") != ut]
            data["total"] = len(current)

            await self._seed(session, data)
            self._checked[session] = loop.time()
            if not stale:
                data["stats"] = {"refetched": 0, "failed": []}
                self._write(session, data)
                return data
            self._pending[session] = len(stale)
            task = asyncio.create_task(self._build(session, data, stale, current))
            self._tasks[session] = task
        if wait or len(stale) <= INLINE_LIMIT:
            await task
        return data

    async def _seed(self, session: str, data: dict) -> None:
        bills: dict = data["bills"]
        try:
            passed = await self.client.public_json(f"{session}/passedlist.json", TTL_BILLLIST)
            for p in passed.get("passedbills", []) if isinstance(passed, dict) else passed:
                rec = seed_record(p)
                if rec["bill"] and rec["bill"] not in bills:
                    bills[rec["bill"]] = rec
        except (NotFound, RateLimited):
            pass
        try:
            filelist = await self.client.public_json(f"{session}/filelist.json", TTL_BILLLIST)
            data["requests"] = [
                {"tracking_id": f.get("trackingID"), "title": (f.get("shortTitle") or "").strip(), "sponsor": f.get("sponsor") or ""}
                for f in filelist
            ]
        except (NotFound, RateLimited):
            data.setdefault("requests", [])

    async def _build(self, session: str, data: dict, stale: list[str], current: dict[str, str]) -> None:
        bills: dict = data["bills"]
        failures: list[str] = []
        backoff = 15.0
        done = 0
        try:
            for number in stale:
                while True:
                    try:
                        bill = await self.client.bill(session, number)
                        bills[number] = index_record(bill, current[number])
                        backoff = 15.0
                        break
                    except RateLimited as exc:
                        wait = min(max(exc.retry_after, backoff), MAX_BACKOFF)
                        log.warning("index: rate limited; sleeping %.0fs", wait)
                        await asyncio.sleep(wait)
                        backoff = min(backoff * 2, MAX_BACKOFF)
                    except Exception as exc:  # one bad bill shouldn't stop the build
                        log.warning("index: failed to fetch %s %s: %s", session, number, exc)
                        failures.append(number)
                        break
                done += 1
                self._pending[session] = len(stale) - done
                if done % SAVE_EVERY == 0:
                    self._write(session, data)
                await asyncio.sleep(self.interval)
        finally:
            data["stats"] = {"refetched": done - len(failures), "failed": failures}
            self._pending[session] = 0
            self._write(session, data)


def score_record(rec: dict, tokens: list[str], phrase: str) -> tuple[int, float]:
    fields = {
        "title": rec.get("title", "").lower(),
        "subjects": " ".join(rec.get("subjects") or []).lower(),
        "general": rec.get("general", "").lower(),
        "highlights": rec.get("highlights", "").lower(),
        "sponsors": f"{rec.get('sponsor', '')} {rec.get('floor_sponsor', '')}".lower(),
    }
    field_tokens = {k: set(tokenize(v)) for k, v in fields.items()}
    matched = 0
    score = 0.0
    for tok in tokens:
        hit = False
        for name, toks in field_tokens.items():
            # prefix match so "school" also hits "schools"
            if tok in toks or (len(tok) >= 4 and any(t.startswith(tok) for t in toks)):
                score += _WEIGHTS[name]
                hit = True
        matched += hit
    if phrase and len(tokens) > 1:
        if phrase in fields["title"]:
            score += 10
        elif phrase in fields["general"] or phrase in fields["highlights"]:
            score += 4
    return matched, score


def snippet(rec: dict, tokens: list[str], width: int = 180) -> str:
    text = rec.get("general") or rec.get("highlights") or ""
    low = text.lower()
    pos = min((p for t in tokens if (p := low.find(t)) >= 0), default=0)
    start = max(0, pos - width // 3)
    out = text[start : start + width].replace("\n", " ")
    return ("…" if start else "") + out + ("…" if start + width < len(text) else "")


def search(
    data: dict,
    query: str,
    subject: str | None = None,
    sponsor: str | None = None,
    status: str | None = None,
    include_requests: bool = False,
) -> list[dict]:
    tokens = tokenize(query)
    phrase = " ".join(query.lower().split())
    results = []
    for rec in data.get("bills", {}).values():
        if subject and not any(subject.lower() in s.lower() for s in rec.get("subjects") or []):
            continue
        if sponsor and sponsor.lower() not in f"{rec.get('sponsor')} {rec.get('floor_sponsor')}".lower():
            continue
        if status and status.lower() not in (rec.get("status") or "").lower():
            continue
        if tokens:
            matched, score = score_record(rec, tokens, phrase)
            if matched == 0:
                continue
        else:
            matched, score = 0, 0.0
        results.append((matched, score, rec))
    if include_requests and tokens:
        for req in data.get("requests", []):
            if sponsor and sponsor.lower() not in req["sponsor"].lower():
                continue
            title_toks = set(tokenize(req["title"]))
            matched = sum(1 for t in tokens if t in title_toks)
            if matched:
                results.append((matched, matched * _WEIGHTS["title"] * 0.8, {**req, "type": "unnumbered request"}))
    results.sort(key=lambda r: (r[0], r[1]), reverse=True)
    out = []
    for matched, score, rec in results:
        if "bill" in rec:
            item = {
                "bill": rec["bill"],
                "title": rec["title"],
                "sponsor": rec["sponsor"],
                "status": rec["status"],
                "status_date": rec["status_date"],
            }
            if tokens:
                item["score"] = round(score, 1)
                item["matched_terms"] = f"{matched}/{len(tokens)}"
                if snip := snippet(rec, tokens):
                    item["snippet"] = snip
            if rec.get("partial"):
                item["title_only"] = True
        else:
            item = {**rec, "score": round(score, 1)}
        out.append(item)
    if not tokens:
        out.sort(key=lambda r: r.get("bill") or "")
    return out
