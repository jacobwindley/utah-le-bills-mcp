"""Compact, context-budgeted views of legislature data."""

from __future__ import annotations

import html
import json
import re
from typing import Any, Literal

from .config import HARD_MAX_CHARS, MIN_MAX_CHARS

Detail = Literal["brief", "standard", "full"]
SITE = "https://le.utah.gov"


# ---- text cleanup -------------------------------------------------------------


def clean_html(text: str | None) -> str:
    """Strip the markup used in bill fields/fiscal notes down to readable plain text."""
    if not text:
        return ""
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", text)
    text = re.sub(r"(?i)<ltbullet\s*/?>", "- ", text)
    text = re.sub(r"(?i)<(hr|br|p|div|tr|li|h\d)\b[^>]*>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text).replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n[ \n]*", "\n", text)
    return text.strip()


def compact(obj: Any) -> Any:
    """Recursively drop empty values so they don't eat context."""
    if isinstance(obj, dict):
        out = {k: compact(v) for k, v in obj.items()}
        return {k: v for k, v in out.items() if v not in (None, "", [], {})}
    if isinstance(obj, list):
        return [c for c in (compact(v) for v in obj) if c not in (None, "", [], {})]
    return obj


def size(obj: Any) -> int:
    return len(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))


def resolve_max_chars(requested: int | None, default: int) -> int:
    value = default if requested is None else requested
    return max(MIN_MAX_CHARS, min(int(value), HARD_MAX_CHARS))


def absolute_url(url: str) -> str:
    return url if url.startswith("http") else SITE + ("" if url.startswith("/") else "/") + url


# ---- budgeting ----------------------------------------------------------------


def slice_text(text: str, offset: int, max_chars: int) -> dict:
    """Return one page of `text`, cut at a whitespace boundary, with paging info."""
    total = len(text)
    offset = max(0, min(offset, total))
    end = min(total, offset + max_chars)
    if end < total:
        cut = max(text.rfind("\n", offset, end), text.rfind(" ", offset, end))
        if cut > offset + max_chars // 2:
            end = cut + 1
    page: dict[str, Any] = {
        "text": text[offset:end],
        "offset": offset,
        "total_chars": total,
    }
    if end < total:
        page["truncated"] = True
        page["next_offset"] = end
    return page


def paginate(items: list, offset: int, limit: int, max_chars: int, **extra: Any) -> dict:
    """Page a list by count *and* by character budget."""
    total = len(items)
    offset = max(0, offset)
    out: dict[str, Any] = {"total": total, "offset": offset, **extra, "items": []}
    # Leave room for the paging fields added below.
    budget = max_chars - size(out) - 140
    used = 0
    for item in items[offset : offset + limit]:
        cost = size(item) + 1
        if used + cost > budget:
            if out["items"]:
                break
            # A single item bigger than the whole budget: shrink it rather than skip it.
            item = fit(item, max(budget, 200)) if isinstance(item, dict) else item
            cost = size(item) + 1
        out["items"].append(item)
        used += cost
    out["returned"] = len(out["items"])
    nxt = offset + out["returned"]
    if nxt < total:
        out["next_offset"] = nxt
    if out["returned"] < min(limit, total - offset):
        out["truncated"] = "character budget reached; page with next_offset"
    return out


TRUNCATION_NOTICE = "Fields shortened to fit max_chars; raise max_chars or use a narrower tool."


def fit(obj: dict, max_chars: int) -> dict:
    """Shrink a record until it fits the budget: halve the biggest list or cut the longest
    string, whichever is larger, repeatedly."""
    obj = compact(obj)
    if size(obj) <= max_chars:
        return obj
    obj = json.loads(json.dumps(obj))  # deep copy
    note = "…"
    target = max_chars - len(TRUNCATION_NOTICE) - 20
    for _ in range(300):
        over = size(obj) - target
        if over <= 0:
            break
        spath, svalue = _longest_string(obj)
        lpath, lst = _longest_list(obj)
        list_cost = size(lst) if lpath is not None and len(lst) > 1 else 0
        if list_cost > len(svalue) or (len(svalue) <= 40 and list_cost):
            keep = max(1, len(lst) // 2)
            _set(obj, lpath, lst[:keep] + [f"…{len(lst) - keep} more"])
        elif spath is not None and len(svalue) > 40:
            keep = max(40, len(svalue) - over - 5)
            _set(obj, spath, svalue[:keep] + note)
        else:
            break
    # Last resort: drop the largest top-level fields.
    while size(obj) > target and len(obj) > 1:
        del obj[max(obj, key=lambda k: size(obj[k]))]
    obj["_truncated"] = TRUNCATION_NOTICE
    return obj


def _walk(obj: Any, path: tuple = ()):
    yield path, obj
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk(v, path + (k,))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk(v, path + (i,))


def _longest_string(obj: Any):
    best = (None, "")
    for path, v in _walk(obj):
        if isinstance(v, str) and len(v) > len(best[1]):
            best = (path, v)
    return best


def _longest_list(obj: Any):
    best = (None, [])
    for path, v in _walk(obj):
        if isinstance(v, list) and path and size(v) > size(best[1]):
            best = (path, v)
    return best


def _set(obj: Any, path: tuple, value: Any) -> None:
    for key in path[:-1]:
        obj = obj[key]
    obj[path[-1]] = value


# ---- bills --------------------------------------------------------------------


def bill_page_url(session: str, bill: str) -> str:
    return f"{SITE}/~{session[:4]}/bills/static/{bill}.html" if session.endswith("GS") else (
        f"{SITE}/bills/billSearch.jsp?sess={session}"
    )


def active_version(bill: dict) -> dict:
    versions = bill.get("billVersionList") or []
    for v in versions:
        if v.get("activeVersion"):
            return v
    return versions[-1] if versions else {}


def summarize_action(a: dict) -> dict:
    out = {
        "date": (a.get("actionDate") or "")[:16],
        "action": a.get("description"),
        "owner": a.get("owner"),
    }
    if a.get("voteStr") or a.get("voteID"):
        vote: dict[str, Any] = {"tally": a.get("voteStr"), "chamber": a.get("voteHouse")}
        if a.get("voiceVote") == "1":
            vote["voice_vote"] = True
        out["vote"] = vote
    return compact(out)


def is_vote(a: dict) -> bool:
    return bool(a.get("voteStr") or a.get("voteID"))


def summarize_bill(bill: dict, session: str, detail: Detail = "brief") -> dict:
    number = bill.get("billNumber", "")
    out: dict[str, Any] = {
        "bill": number,
        "short": bill.get("billNumberShort"),
        "session": session,
        "title": (bill.get("shortTitle") or "").strip(),
        "sponsor": bill.get("primeSponsorName"),
        "floor_sponsor": bill.get("floorSponsorName"),
        "status": bill.get("lastAction"),
        "status_date": bill.get("lastActionDate"),
        "url": bill_page_url(session, number),
    }
    if detail == "brief":
        return compact(out)

    version = active_version(bill)
    out.update(
        {
            "status_owner": bill.get("lastActionOwner"),
            "general_description": clean_html(bill.get("generalProvisions")),
            "highlighted_provisions": clean_html(bill.get("highlightedProvisions")),
            "monies_appropriated": clean_html(bill.get("moniesAppropriated")),
            "subjects": [s.get("description") for s in version.get("subjectList") or []],
            "cosponsors": [
                c.get("name") or c.get("legislatorName") or c.get("id") or str(c)
                for c in version.get("coSponsorList") or []
            ],
            "code_sections_affected": [s.get("secNo") for s in version.get("sectionAffectedList") or []],
            "versions": [
                {
                    "sub_version": v.get("subVersion"),
                    "sponsor": v.get("versionSponsorName"),
                    "active": v.get("activeVersion") or None,
                }
                for v in bill.get("billVersionList") or []
            ],
            "action_count": len(bill.get("actionHistoryList") or []),
        }
    )
    if detail == "full":
        actions = bill.get("actionHistoryList") or []
        out.update(
            {
                "drafting_attorney": bill.get("draftingAttorney"),
                "fiscal_analyst": bill.get("fiscalAnalyst"),
                "file_number": bill.get("fileNumber"),
                "tracking_id": bill.get("trackingID"),
                "recent_actions": [summarize_action(a) for a in actions[-10:]],
                "floor_debates": [
                    {"date": f.get("sessionDate"), "chamber": f.get("house"), "day": f.get("dayOfSession")}
                    for f in bill.get("floorDebateList") or []
                ],
                "documents": bill_documents(bill),
            }
        )
    return compact(out)


def bill_documents(bill: dict) -> list[dict]:
    docs = []
    seen = set()
    for v in bill.get("billVersionList") or []:
        for d in v.get("billDocs") or []:
            url = d.get("url")
            if not url or url in seen:
                continue
            seen.add(url)
            docs.append(
                compact(
                    {
                        "type": d.get("shortDesc") or d.get("fileType"),
                        "file_type": d.get("fileType"),
                        "date": (d.get("fileDate") or "")[:16],
                        "url": absolute_url(url),
                        "sub_version": v.get("subVersion"),
                    }
                )
            )
    return docs
