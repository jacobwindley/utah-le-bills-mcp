"""MCP server exposing Utah Legislature data (le.utah.gov) with context-budgeted tools."""

import functools
import json
import logging
import xml.etree.ElementTree as ET
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from . import formatting as fmt
from .client import (
    TTL_BILLLIST,
    TTL_CALENDAR,
    TTL_COMMITTEES,
    TTL_DOC,
    TTL_LEGISLATORS,
    TTL_STATUTE,
    LeClient,
    NotFound,
    TokenRequired,
    UpstreamError,
)
from .config import Settings
from .search import BillIndex, search
from .session import InvalidInput, normalize_bill, normalize_prefix
from .workspace import Workspace, decode_xml, find_snippets, render

log = logging.getLogger(__name__)

settings = Settings.from_env()
client = LeClient(token=settings.token)
workspace = Workspace(client)
index = BillIndex(client, settings.cache_dir)

INSTRUCTIONS = """\
Utah State Legislature data: bills, legislators, committees, meetings{glen}.

Sessions: '2026' or '2026GS' = general session; '2025S1' = first special session. Omit for the
current/most recent general session. Bill numbers accept 'HB 1', 'H.B. 1', 'hb0001', 'SJR3'.

Context budget: every tool returns at most `max_chars` characters (default {max_chars}). Long
results include `next_offset`; call again with `offset=next_offset` to page. Prefer
`detail="brief"` and narrow tools first. To study a bill's text: load_bill -> table of contents,
then read_bill_text(section=...) or find_in_bill(pattern) rather than reading everything.
"""

mcp = MCPServer(
    name="utah-legislature",
    instructions=INSTRUCTIONS.format(
        max_chars=settings.max_chars,
        glen=", Utah Code, Constitution and legislative rules" if settings.token else "",
    ),
)

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=True)

# ---- shared parameter types ----------------------------------------------------

Session = Annotated[
    str | None,
    Field(description="Session, e.g. '2026', '2026GS', '2025S1'. Omit for the current general session."),
]
Bill = Annotated[str, Field(description="Bill number, e.g. 'HB 1', 'SB42', 'HJR 3'.")]
DetailP = Annotated[
    Literal["brief", "standard", "full"],
    Field(description="brief = one line per item; standard adds summaries; full adds everything."),
]
MaxChars = Annotated[
    int | None,
    Field(description="Character budget for this response (500-20000). Defaults to the server setting."),
]
Offset = Annotated[int, Field(ge=0, description="Start position for paging; use next_offset from a prior call.")]
Limit = Annotated[int, Field(ge=1, le=50, description="Max items to return (1-50).")]


def _max(requested: int | None) -> int:
    return fmt.resolve_max_chars(requested, settings.max_chars)


def tool(fn):
    """Register a read-only tool and turn expected failures into clean tool errors."""

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            result = await fn(*args, **kwargs)
        except (NotFound, InvalidInput, TokenRequired, UpstreamError, LookupError) as exc:
            raise ToolError(str(exc)) from None
        # Compact JSON text (no indentation, no structured duplicate) keeps context small and
        # matches what formatting.size() budgets for.
        return json.dumps(result, ensure_ascii=False, separators=(",", ":"))

    return wrapper


def register(fn, enabled: bool = True):
    if enabled:
        mcp.add_tool(tool(fn), annotations=READ_ONLY, structured_output=False)
    return fn


async def _bill(bill: str, session: str | None) -> tuple[str, str, dict]:
    s = await client.resolve_session(session)
    b = normalize_bill(bill)
    try:
        data = await client.bill(s, b)
    except NotFound:
        raise NotFound(f"{b} not found in session {s}.") from None
    return s, b, data


# ---- bills ----------------------------------------------------------------------


async def get_bill(bill: Bill, session: Session = None, detail: DetailP = "standard", max_chars: MaxChars = None) -> dict:
    """Get a bill's metadata: title, sponsors, status, and (standard/full) its summary, subjects,
    affected code sections, versions. Does not include bill text - use load_bill for that."""
    s, _, data = await _bill(bill, session)
    return fmt.fit(fmt.summarize_bill(data, s, detail), _max(max_chars))


async def get_bill_history(
    bill: Bill,
    session: Session = None,
    full_history: Annotated[bool, Field(description="Return every action (paged) instead of recent + votes.")] = False,
    offset: Offset = 0,
    limit: Limit = 25,
    max_chars: MaxChars = None,
) -> dict:
    """Action history of a bill (committee referrals, readings, votes with tallies as
    yeas-nays-absent, governor action) plus floor debate dates. By default returns the last 10
    actions plus every vote action, oldest first."""
    s, b, data = await _bill(bill, session)
    actions = data.get("actionHistoryList") or []
    if full_history:
        chosen = actions
    else:
        recent = set(range(max(0, len(actions) - 10), len(actions)))
        chosen = [a for i, a in enumerate(actions) if i in recent or fmt.is_vote(a)]
    items = [fmt.summarize_action(a) for a in chosen]
    floor = [
        fmt.compact({"date": f.get("sessionDate"), "chamber": f.get("house"), "day": f.get("dayOfSession")})
        for f in data.get("floorDebateList") or []
    ]
    return fmt.paginate(
        items,
        offset,
        limit,
        _max(max_chars),
        bill=b,
        session=s,
        total_actions=len(actions),
        status=data.get("lastAction"),
        floor_debates=floor,
    )


async def get_bill_documents(bill: Bill, session: Session = None, max_chars: MaxChars = None) -> dict:
    """List a bill's documents (bill text versions, fiscal notes, committee reports, transmittal
    letters) with absolute URLs."""
    s, b, data = await _bill(bill, session)
    return fmt.paginate(fmt.bill_documents(data), 0, 50, _max(max_chars), bill=b, session=s)


async def load_bill(
    bill: Bill,
    session: Session = None,
    version: Annotated[
        str | None, Field(description="Text version, e.g. 'Introduced', 'Enrolled', '1st Sub'. Default: enrolled, else latest.")
    ] = None,
    refresh: Annotated[bool, Field(description="Force a re-download even if unchanged.")] = False,
) -> dict:
    """Load a bill's full text into the server's working memory and return only a table of
    contents (section ids, headings, sizes). The text stays server-side: read it piece by piece
    with read_bill_text or search it with find_in_bill. Up to 10 bills stay loaded."""
    s = await client.resolve_session(session)
    loaded = await workspace.load(s, normalize_bill(bill), version, force=refresh)
    return fmt.fit(loaded.toc(), max(settings.max_chars, 6000))


async def read_bill_text(
    bill: Bill,
    session: Session = None,
    section: Annotated[
        str | None,
        Field(description="Section id from load_bill's table of contents (e.g. '53F-2-301', 'S5', 'intro'). Omit to page through the whole bill."),
    ] = None,
    offset: Offset = 0,
    max_chars: MaxChars = None,
) -> dict:
    """Read a slice of a bill's text (loads the bill if needed). Amendments are marked
    [+inserted+] and [-deleted-]. Page with offset/next_offset."""
    s = await client.resolve_session(session)
    loaded = await workspace.get(s, normalize_bill(bill))
    if section:
        sec = loaded.find_section(section)
        if sec is None:
            ids = ", ".join(x.id for x in loaded.sections)
            raise LookupError(f"No section {section!r} in {loaded.bill}. Sections: {ids}")
        text, label = sec.text, sec.id
    else:
        text, label = loaded.full_text(), "all"
    page = fmt.slice_text(text, offset, _max(max_chars) - 300)
    return {"bill": loaded.bill, "session": s, "version": loaded.version, "section": label, **page}


async def find_in_bill(
    bill: Bill,
    pattern: Annotated[str, Field(description="Text to find (case-insensitive).")],
    session: Session = None,
    regex: Annotated[bool, Field(description="Treat pattern as a regular expression.")] = False,
    context_chars: Annotated[int, Field(ge=20, le=600)] = 160,
    max_hits: Annotated[int, Field(ge=1, le=50)] = 10,
    max_chars: MaxChars = None,
) -> dict:
    """Search inside a bill's text and return only matching snippets with their section ids
    (loads the bill if needed). Use read_bill_text(section=..., offset=...) to read around a hit."""
    s = await client.resolve_session(session)
    loaded = await workspace.get(s, normalize_bill(bill))
    try:
        result = find_snippets(loaded, pattern, regex, context_chars, max_hits)
    except Exception as exc:  # bad regex
        raise InvalidInput(f"Invalid pattern: {exc}") from None
    hits = result.pop("hits")
    return fmt.paginate(hits, 0, max_hits, _max(max_chars), **result)


async def list_loaded_bills() -> dict:
    """List bills currently loaded in working memory."""
    return {
        "loaded": [
            {"bill": lb.bill, "session": lb.session, "title": lb.summary.get("title"), "version": lb.version, "chars": lb.total_chars}
            for lb in workspace.loaded()
        ]
    }


async def unload_bill(bill: Bill, session: Session = None) -> dict:
    """Remove a bill from working memory."""
    s = await client.resolve_session(session)
    b = normalize_bill(bill)
    return {"bill": b, "session": s, "unloaded": workspace.unload(s, b)}


async def get_fiscal_note(bill: Bill, session: Session = None, offset: Offset = 0, max_chars: MaxChars = None) -> dict:
    """Read a bill's fiscal note (estimated revenue/expenditure impact) as plain text, paged."""
    s, b, data = await _bill(bill, session)
    notes = [d for d in fmt.bill_documents(data) if d.get("file_type") == "PubFN" and d["url"].endswith(".html")]
    if not notes:
        raise NotFound(f"No published fiscal note found for {b} ({s}).")
    note = max(notes, key=lambda d: d.get("date", ""))
    body = await client.site(note["url"], TTL_DOC)
    text = fmt.clean_html(body.decode("utf-8", errors="replace"))
    return {"bill": b, "session": s, "url": note["url"], **fmt.slice_text(text, offset, _max(max_chars) - 300)}


async def search_bills(
    query: Annotated[str, Field(description="Keywords matched against titles, subjects and summaries.")],
    session: Session = None,
    subject: Annotated[str | None, Field(description="Only bills with a subject containing this text.")] = None,
    sponsor: Annotated[str | None, Field(description="Only bills whose sponsor/floor sponsor name contains this.")] = None,
    status: Annotated[str | None, Field(description="Only bills whose last action contains this, e.g. 'Governor Signed'.")] = None,
    include_requests: Annotated[bool, Field(description="Also match unnumbered bill requests by title.")] = False,
    offset: Offset = 0,
    limit: Limit = 10,
    max_chars: MaxChars = None,
) -> dict:
    """Keyword search over bill titles, subjects and summaries for a session. With an empty
    query, lists bills matching the subject/sponsor/status filters. The first search of a
    session starts building a local index in the background (~10 min, rate-limited); until it
    finishes, results cover passed bills (titles only) plus whatever has been indexed so far."""
    s = await client.resolve_session(session)
    data = await index.ensure(s)
    results = search(data, query, subject, sponsor, status, include_requests)
    return fmt.paginate(results, offset, limit, _max(max_chars), session=s, query=query, index=index.status(s))


async def refresh_bill_index(
    session: Session = None,
    full: Annotated[bool, Field(description="Refetch every bill (slow; runs in the background).")] = False,
) -> dict:
    """Update the local search index for a session (only changed bills unless full=True), or
    check on a background build's progress."""
    s = await client.resolve_session(session)
    await index.ensure(s, force=full)
    return {"session": s, **index.status(s), **index.cached(s).get("stats", {})}


async def list_bills(
    session: Session = None,
    prefix: Annotated[str | None, Field(description="Bill type: HB, SB, HJR, SJR, HCR, SCR, HR, SR.")] = None,
    updated_since: Annotated[str | None, Field(description="ISO date/time, e.g. '2026-02-15'. Only bills updated on/after.")] = None,
    sort: Annotated[Literal["number", "recent"], Field(description="'recent' = most recently updated first.")] = "number",
    offset: Offset = 0,
    limit: Limit = 25,
    max_chars: MaxChars = None,
) -> dict:
    """List numbered bills in a session with last-update times (titles included when the search
    index has been built). Use get_bill or search_bills for details."""
    s = await client.resolve_session(session)
    rows = await client.billlist(s)
    if prefix:
        p = normalize_prefix(prefix)
        rows = [r for r in rows if r["number"].startswith(p) and r["number"][len(p) :].isdigit()]
    if updated_since:
        rows = [r for r in rows if (r.get("updatetime") or "") >= updated_since]
    if sort == "recent":
        rows = sorted(rows, key=lambda r: r.get("updatetime") or "", reverse=True)
    titles = index.cached(s).get("bills", {})
    items = [
        fmt.compact({"bill": r["number"], "title": titles.get(r["number"], {}).get("title"), "updated": (r.get("updatetime") or "")[:16]})
        for r in rows
    ]
    return fmt.paginate(items, offset, limit, _max(max_chars), session=s)


async def list_passed_bills(
    session: Session = None,
    governor_action: Annotated[str | None, Field(description="Filter by governor action code, e.g. 'GSIGN', 'GOVLVETO'.")] = None,
    offset: Offset = 0,
    limit: Limit = 25,
    max_chars: MaxChars = None,
) -> dict:
    """Bills that passed the Legislature in a session (2016+), with governor action."""
    s = await client.resolve_session(session)
    data = await client.public_json(f"{s}/passedlist.json", TTL_BILLLIST)
    rows = data.get("passedbills", []) if isinstance(data, dict) else data
    if governor_action:
        rows = [r for r in rows if (r.get("govaction") or "").upper() == governor_action.upper()]
    items = [
        fmt.compact(
            {
                "bill": r.get("number"),
                "title": r.get("title"),
                "sponsor": r.get("sponsor"),
                "passed": (r.get("datepassed") or "")[:10],
                "governor_action": r.get("govaction"),
                "governor_date": (r.get("govactiondate") or "")[:10],
                "chapter": r.get("chapter"),
            }
        )
        for r in rows
    ]
    return fmt.paginate(items, offset, limit, _max(max_chars), session=s)


async def list_bill_requests(
    session: Session = None,
    query: Annotated[str | None, Field(description="Only requests whose title contains this text.")] = None,
    sponsor: Annotated[str | None, Field(description="Only requests whose sponsor id contains this.")] = None,
    offset: Offset = 0,
    limit: Limit = 25,
    max_chars: MaxChars = None,
) -> dict:
    """Unnumbered bill requests (drafts being prepared) for a session, with titles and sponsor ids."""
    s = await client.resolve_session(session)
    rows = await client.public_json(f"{s}/filelist.json", TTL_BILLLIST)
    if query:
        rows = [r for r in rows if query.lower() in (r.get("shortTitle") or "").lower()]
    if sponsor:
        rows = [r for r in rows if sponsor.lower() in (r.get("sponsor") or "").lower()]
    items = [{"tracking_id": r.get("trackingID"), "title": (r.get("shortTitle") or "").strip(), "sponsor": r.get("sponsor")} for r in rows]
    return fmt.paginate(items, offset, limit, _max(max_chars), session=s)


# ---- legislators / committees / calendar ---------------------------------------------


async def _legislators() -> list[dict]:
    data = await client.public_json("legislators.json", TTL_LEGISLATORS)
    return data.get("legislators", [])


def _legislator_view(leg: dict, detail: str) -> dict:
    out = {
        "id": leg.get("id"),
        "name": leg.get("formatName") or leg.get("fullName"),
        "chamber": {"H": "House", "S": "Senate"}.get(leg.get("house"), leg.get("house")),
        "party": leg.get("party"),
        "district": leg.get("district"),
        "counties": leg.get("counties"),
    }
    if detail in ("standard", "full"):
        out.update(
            {
                "email": leg.get("email"),
                "phone": leg.get("cell") or leg.get("workPhone"),
                "profession": leg.get("profession"),
                "service_start": leg.get("serviceStart"),
                "committees": [c.get("name") for c in leg.get("committees") or []],
            }
        )
    if detail == "full":
        out.update(
            {
                "bio": leg.get("bio"),
                "education": leg.get("education"),
                "address": leg.get("address"),
                "image": leg.get("image"),
                "legislation_url": leg.get("legislation"),
                "demographics_url": leg.get("demographic"),
                "finance_reports": [f.get("url") for f in leg.get("FinanceReport") or []],
                "social": {k: leg.get(k) for k in ("twitter", "facebook", "instagram", "linkedin")},
            }
        )
    return fmt.compact(out)


async def list_legislators(
    chamber: Annotated[Literal["H", "S"] | None, Field(description="'H' House or 'S' Senate.")] = None,
    party: Annotated[str | None, Field(description="'R' or 'D'.")] = None,
    district: Annotated[str | None, Field(description="District number.")] = None,
    county: Annotated[str | None, Field(description="Only legislators whose district includes this county.")] = None,
    detail: DetailP = "brief",
    offset: Offset = 0,
    limit: Limit = 25,
    max_chars: MaxChars = None,
) -> dict:
    """List current Utah legislators, filterable by chamber, party, district or county."""
    rows = await _legislators()
    if chamber:
        rows = [r for r in rows if r.get("house") == chamber]
    if party:
        rows = [r for r in rows if (r.get("party") or "").upper() == party.upper()[:1]]
    if district:
        rows = [r for r in rows if r.get("district") == str(int(district))]
    if county:
        rows = [r for r in rows if county.lower() in (r.get("counties") or "").lower()]
    items = [_legislator_view(r, detail) for r in rows]
    return fmt.paginate(items, offset, limit, _max(max_chars))


async def get_legislator(
    who: Annotated[str, Field(description="Legislator id (e.g. 'PETERT') or part of a name.")],
    detail: DetailP = "standard",
    max_chars: MaxChars = None,
) -> dict:
    """Get one legislator by id or name. Returns candidates if the name is ambiguous."""
    rows = await _legislators()
    exact = [r for r in rows if (r.get("id") or "").upper() == who.strip().upper()]
    matches = exact or [
        r for r in rows if who.lower() in f"{r.get('fullName')} {r.get('formatName')}".lower()
    ]
    if not matches:
        raise NotFound(f"No legislator matching {who!r}.")
    if len(matches) > 1:
        return {"ambiguous": True, "candidates": [_legislator_view(r, "brief") for r in matches[:15]]}
    return fmt.fit(_legislator_view(matches[0], detail), _max(max_chars))


async def _committees() -> list[dict]:
    data = await client.public_json("committees.json", TTL_COMMITTEES)
    return data.get("committees", [])


def _next_meeting(c: dict) -> str | None:
    times = sorted(m.get("mtgTime") or "" for m in c.get("meetings") or [])
    return times[0] if times else None


async def list_committees(
    query: Annotated[str | None, Field(description="Only committees whose name or id contains this.")] = None,
    offset: Offset = 0,
    limit: Limit = 25,
    max_chars: MaxChars = None,
) -> dict:
    """List legislative committees with chair, member count and next meeting (UTC)."""
    rows = await _committees()
    if query:
        q = query.lower()
        rows = [r for r in rows if q in (r.get("description") or "").lower() or q in (r.get("id") or "").lower()]
    items = [
        fmt.compact(
            {
                "id": r.get("id"),
                "name": r.get("description"),
                "chair": [m.get("name") for m in r.get("members") or [] if "chair" in (m.get("position") or "")],
                "members": len(r.get("members") or []),
                "next_meeting_utc": _next_meeting(r),
            }
        )
        for r in rows
    ]
    return fmt.paginate(items, offset, limit, _max(max_chars))


async def get_committee(
    committee: Annotated[str, Field(description="Committee id (e.g. 'INTBUS') or part of its name.")],
    max_chars: MaxChars = None,
) -> dict:
    """Get a committee's members (with positions) and scheduled meetings."""
    rows = await _committees()
    exact = [r for r in rows if (r.get("id") or "").upper() == committee.strip().upper()]
    matches = exact or [r for r in rows if committee.lower() in (r.get("description") or "").lower()]
    if not matches:
        raise NotFound(f"No committee matching {committee!r}.")
    if len(matches) > 1:
        return {"ambiguous": True, "candidates": [{"id": r.get("id"), "name": r.get("description")} for r in matches[:20]]}
    c = matches[0]
    return fmt.fit(
        {
            "id": c.get("id"),
            "name": c.get("description"),
            "url": c.get("link"),
            "members": [{"id": m.get("id"), "name": m.get("name"), "position": m.get("position")} for m in c.get("members") or []],
            "meetings_utc": [{"time": m.get("mtgTime"), "place": m.get("mtgPlace"), "status": m.get("status")} for m in c.get("meetings") or []],
        },
        _max(max_chars),
    )


async def get_meeting_calendar(
    committee: Annotated[str | None, Field(description="Only meetings whose committee name contains this.")] = None,
    start_date: Annotated[str | None, Field(description="YYYY-MM-DD; meetings on/after this date.")] = None,
    end_date: Annotated[str | None, Field(description="YYYY-MM-DD; meetings on/before this date.")] = None,
    offset: Offset = 0,
    limit: Limit = 25,
    max_chars: MaxChars = None,
) -> dict:
    """Upcoming legislative meetings (times in UTC; Utah is UTC-7/-6)."""
    data = await client.public_json("legcal.json", TTL_CALENDAR)
    rows = data.get("items", []) if isinstance(data, dict) else data
    if committee:
        rows = [r for r in rows if committee.lower() in (r.get("committee") or "").lower()]
    if start_date:
        rows = [r for r in rows if (r.get("mtgTime") or "")[:10] >= start_date]
    if end_date:
        rows = [r for r in rows if (r.get("mtgTime") or "")[:10] <= end_date]
    rows = sorted(rows, key=lambda r: r.get("mtgTime") or "")
    rows = [r for r in rows if r.get("committee") or r.get("mtgID")]
    items = [
        fmt.compact({"meeting_id": r.get("mtgID"), "committee": r.get("committee"), "time_utc": r.get("mtgTime"), "place": r.get("mtgPlace"), "url": r.get("link")})
        for r in rows
    ]
    return fmt.paginate(items, offset, limit, _max(max_chars))


# ---- token-gated (GLEN) tools ------------------------------------------------------


def _glen_payload(body: bytes, offset: int, max_chars: int) -> dict:
    text = body.decode("utf-8-sig", errors="replace").strip()
    if text.startswith(("{", "[")):
        try:
            text = json.dumps(fmt.compact(json.loads(text)), ensure_ascii=False, indent=1)
        except ValueError:
            pass
    elif text.startswith("<"):
        try:
            text = render(decode_xml(body), statute=True)
        except ET.ParseError:  # not well-formed XML (e.g. an HTML error page)
            text = fmt.clean_html(text)
    return fmt.slice_text(text, offset, max_chars - 300)


async def get_utah_code(
    citation: Annotated[str, Field(description="Title, chapter or section, e.g. '53F', '53F-2', '53F-2-301'.")],
    offset: Offset = 0,
    max_chars: MaxChars = None,
) -> dict:
    """Read current Utah Code text by title/chapter/section (requires developer token). Paged."""
    body = await client.glen(f"code/{citation.strip()}", TTL_STATUTE)
    return {"citation": citation, **_glen_payload(body, offset, _max(max_chars))}


async def get_constitution(
    citation: Annotated[str, Field(description="Article or section, e.g. 'A6' or 'A6 S2'; 'list' for all articles.")],
    offset: Offset = 0,
    max_chars: MaxChars = None,
) -> dict:
    """Read the Utah Constitution by article/section (requires developer token). Paged."""
    body = await client.glen(f"const/{citation.strip()}", TTL_STATUTE)
    return {"citation": citation, **_glen_payload(body, offset, _max(max_chars))}


async def get_legislative_rule(
    citation: Annotated[str, Field(description="Rule title/chapter/section, e.g. 'JR4', 'JR4-2', 'JR4-2-101'; 'list' for all.")],
    offset: Offset = 0,
    max_chars: MaxChars = None,
) -> dict:
    """Read Utah legislative rules (joint, House, Senate) (requires developer token). Paged."""
    body = await client.glen(f"rule/{citation.strip()}", TTL_STATUTE)
    return {"citation": citation, **_glen_payload(body, offset, _max(max_chars))}


def _agenda_view(m: dict) -> dict:
    return fmt.compact(
        {
            "meeting_id": m.get("id"),
            "committee": m.get("name"),
            "committee_id": m.get("comcode"),
            "time_utc": m.get("time"),
            "place": m.get("location"),
            "items": [f"{i.get('item')}. {i.get('description')}" for i in m.get("agendaitems") or []],
        }
    )


async def get_meeting_agenda(
    meeting_id: Annotated[str | None, Field(description="Meeting id from get_meeting_calendar. Omit for today's agendas.")] = None,
    offset: Offset = 0,
    limit: Limit = 10,
    max_chars: MaxChars = None,
) -> dict:
    """Get a meeting agenda by meeting id, or all of today's agendas (requires developer token)."""
    if meeting_id:
        body = await client.glen(f"agencal/{meeting_id.strip()}", TTL_CALENDAR)
        return fmt.fit(_agenda_view(json.loads(body.decode("utf-8-sig"))), _max(max_chars))
    body = await client.glen("agencal", TTL_CALENDAR)
    data = json.loads(body.decode("utf-8-sig"))
    meetings = [_agenda_view(m) for m in (data.get("mtgs") if isinstance(data, dict) else data) or []]
    return fmt.paginate(meetings, offset, limit, _max(max_chars), date="today")


# ---- registration -----------------------------------------------------------------

for _fn in (
    get_bill, get_bill_history, get_bill_documents, load_bill, read_bill_text, find_in_bill,
    list_loaded_bills, unload_bill, get_fiscal_note, search_bills, refresh_bill_index, list_bills,
    list_passed_bills, list_bill_requests, list_legislators, get_legislator, list_committees,
    get_committee, get_meeting_calendar,
):
    register(_fn)

for _fn in (get_utah_code, get_constitution, get_legislative_rule, get_meeting_agenda):
    register(_fn, enabled=bool(settings.token))


# ---- resources --------------------------------------------------------------------


@mcp.resource("bill://{session}/{bill}", mime_type="application/json", description="Bill summary (standard detail).")
async def bill_resource(session: str, bill: str) -> str:
    s, _, data = await _bill(bill, session)
    return json.dumps(fmt.summarize_bill(data, s, "standard"), ensure_ascii=False)


@mcp.resource("bill://{session}/{bill}/section/{section}", mime_type="text/plain", description="One section of a bill's text.")
async def bill_section_resource(session: str, bill: str, section: str) -> str:
    s = await client.resolve_session(session)
    loaded = await workspace.get(s, normalize_bill(bill))
    sec = loaded.find_section(section)
    if sec is None:
        raise NotFound(f"No section {section!r} in {loaded.bill}.")
    return sec.text


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    mcp.run()


# httpx logs every request at INFO, which would leak the GLEN token (it is part of the URL).
logging.getLogger("httpx").setLevel(logging.WARNING)


if __name__ == "__main__":
    main()
