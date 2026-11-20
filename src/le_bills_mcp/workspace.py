"""In-memory workspace of loaded bills, with bill XML parsed into sections."""

from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from collections import OrderedDict
from dataclasses import dataclass, field

from .client import LeClient
from .formatting import absolute_url, summarize_bill

MAX_LOADED = 10

# Elements that start a new line when rendered.
_BLOCK_TAGS = {
    "secline", "catline", "subsection", "summary", "licat", "lineitem", "appsrc", "program",
    "hl", "hp", "gd", "gdhead", "hphead", "appintent", "st", "sessionhead", "sponsorhead",
    "otherSponsorhead", "lthead", "moni", "oc", "ua", "sa", "sect", "enact", "p", "para",
    "authorizations", "sources", "programs", "agency", "ltamt", "ltsrc", "ltcat", "catchline",
    "article", "chapter", "part", "section", "title", "rule", "histories", "history",
}
# Inline elements that need surrounding spaces so words don't run together.
_SPACED_TAGS = {"display", "amt", "pamt", "lidesc", "number", "name", "parens"}
_SKIP_TAGS = {"info", "sinfo", "foot", "amendoutstart", "amendoutend", "nextbuid"}


def decode_xml(raw: bytes) -> ET.Element:
    """Parse legislature XML. Files often claim UTF-16 while actually being UTF-8."""
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = raw.decode("utf-16")
    elif len(raw) > 1 and raw[1] == 0:
        text = raw.decode("utf-16-le")
    else:
        raw = raw.removeprefix(b"\xef\xbb\xbf")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            # e.g. Constitution XML, which declares ISO-8859-1
            text = raw.decode("latin-1")
    text = re.sub(r"^\s*<\?xml[^>]*\?>", "", text)
    return ET.fromstring(text)


def render(elem: ET.Element, statute: bool = False) -> str:
    """Render legislative XML to readable text. Insertions: [+ +], deletions: [- -].

    `statute=True` is for Utah Code / Constitution / rules XML, where subsection labels
    such as (1)(a) live in a `number` attribute rather than in the text.
    """
    parts: list[str] = []
    _render_into(elem, parts, statute)
    text = "".join(parts)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" ([.,;:])(?=\s|$)", r"\1", text)
    text = re.sub(r" *\n[ \n]*", "\n", text)
    return text.strip()


def _render_into(elem: ET.Element, parts: list[str], statute: bool = False) -> None:
    tag = elem.tag
    if tag in _SKIP_TAGS or elem.get("show") == "false":
        if elem.tail:
            parts.append(elem.tail)
        return
    if statute and tag == "section" and elem.get("number"):
        _render_statute_section(elem, parts)
        return
    block = tag in _BLOCK_TAGS
    if block:
        parts.append("\n")
    elif tag in _SPACED_TAGS:
        parts.append(" ")
    labelled = False
    if statute and tag == "subsection" and (label := re.search(r"(\([^()]+\))$", elem.get("number") or "")):
        parts.append(label.group(1) + " ")
        labelled = True
    marker = None
    if tag == "amend":
        marker = {"erase": ("[-", "-]"), "amend": ("[+", "+]")}.get(elem.get("ea", ""))
    if marker:
        parts.append(marker[0])
    if elem.text:
        parts.append(elem.text.lstrip() if labelled else elem.text)
    for child in elem:
        _render_into(child, parts, statute)
    if marker:
        parts.append(marker[1])
    if block:
        parts.append("\n")
    elif tag in _SPACED_TAGS:
        parts.append(" ")
    if elem.tail:
        parts.append(elem.tail)


def _render_statute_section(elem: ET.Element, parts: list[str]) -> None:
    """Heading line ("53F-2-301. Catchline"), effective date and history, then the body."""
    catchline = elem.find("catchline")
    title = "".join(catchline.itertext()).strip() if catchline is not None else ""
    parts.append(f"\n{elem.get('number')}. {title}\n")
    meta = []
    if (eff := elem.findtext("effdate")) and eff.strip():
        meta.append(f"Effective {eff.strip()}")
    history = [" ".join("".join(h.itertext()).split()) for h in elem.iter("history")]
    if history:
        meta.append("History: " + "; ".join(history))
    if meta:
        parts.append(" | ".join(meta) + "\n")
    if elem.text and elem.text.strip():
        parts.append(elem.text)
    for child in elem:
        if child.tag not in ("catchline", "effdate", "histories", "modyear"):
            _render_into(child, parts, True)
        elif child.tail:
            parts.append(child.tail)
    parts.append("\n")
    if elem.tail:
        parts.append(elem.tail)


@dataclass
class Section:
    id: str
    number: str  # bill section number ("1", "2", ...) or "intro"
    heading: str
    action: str
    text: str


@dataclass
class LoadedBill:
    session: str
    bill: str
    version: str
    doc_url: str
    fingerprint: tuple
    summary: dict
    sections: list[Section]
    available_versions: list[dict]
    loaded_at: float = field(default_factory=time.time)

    @property
    def total_chars(self) -> int:
        return sum(len(s.text) for s in self.sections)

    def find_section(self, ref: str) -> Section | None:
        key = ref.strip().upper().removeprefix("SECTION").strip()
        for s in self.sections:
            if key in (s.id.upper(), s.number.upper(), f"S{s.number}".upper()):
                return s
        return None

    def full_text(self) -> str:
        return "\n\n".join(f"=== [{s.id}] {s.heading} ===\n{s.text}" for s in self.sections)

    def toc(self) -> dict:
        return {
            "bill": self.bill,
            "session": self.session,
            "title": self.summary.get("title"),
            "status": self.summary.get("status"),
            "version": self.version,
            "text_url": self.doc_url,
            "total_chars": self.total_chars,
            "sections": [
                {"id": s.id, "heading": s.heading, "action": s.action, "chars": len(s.text)}
                for s in self.sections
            ],
            "available_versions": self.available_versions,
            "hint": "Use read_bill_text(section=...) to read a section, or find_in_bill to search.",
        }


def parse_sections(root: ET.Element) -> list[Section]:
    sections: list[Section] = []
    intro_parts = [render(e) for tag in ("tbox", "lt") if (e := root.find(tag)) is not None]
    if intro_parts:
        sections.append(Section("intro", "intro", "Title, sponsors and long title", "", "\n".join(intro_parts)))
    body = root.find("bdy")
    bsecs = body.findall("bsec") if body is not None else []
    for i, bsec in enumerate(bsecs, 1):
        sn = bsec.get("sn") or str(i)
        num = bsec.get("num")
        text = render(bsec)
        lines = [ln for ln in text.splitlines() if ln.strip()]
        # First line is "Section N. Section X is amended to read:"; second the catchline.
        heading = " / ".join(lines[:2]) if lines else ""
        heading = re.sub(r"\[[+-]|[+-]\]", "", heading)
        sections.append(
            Section(
                id=num if (num and bsec.get("src") == "code") else f"S{sn}",
                number=sn,
                heading=heading[:200],
                action=bsec.get("type") or "",
                text=text,
            )
        )
    return sections


def bill_fingerprint(bill: dict) -> tuple:
    return (
        bill.get("lastAction"),
        bill.get("lastActionDate"),
        bill.get("currentSubVersion"),
        len(bill.get("actionHistoryList") or []),
    )


def text_versions(bill: dict) -> list[dict]:
    out = []
    for v in bill.get("billVersionList") or []:
        for d in v.get("billDocs") or []:
            url = d.get("url") or ""
            if url.lower().endswith(".xml"):
                out.append(
                    {
                        "version": d.get("shortDesc") or d.get("fileType"),
                        "file_type": d.get("fileType") or "",
                        "date": (d.get("fileDate") or "")[:16],
                        "url": absolute_url(url),
                    }
                )
    return out


def choose_version(versions: list[dict], wanted: str | None) -> dict | None:
    if not versions:
        return None
    if wanted:
        w = wanted.lower()
        for v in versions:
            if w in (v["version"] or "").lower() or w in v["file_type"].lower() or w in v["url"].lower():
                return v
        return None
    for v in versions:
        if v["file_type"].lower() == "enrolled":
            return v
    return max(versions, key=lambda v: v["date"])


class Workspace:
    def __init__(self, client: LeClient, max_loaded: int = MAX_LOADED) -> None:
        self.client = client
        self.max_loaded = max_loaded
        self._bills: OrderedDict[str, LoadedBill] = OrderedDict()

    async def load(self, session: str, bill: str, version: str | None = None, force: bool = False) -> LoadedBill:
        key = f"{session}/{bill}"
        data = await self.client.bill(session, bill)
        fp = bill_fingerprint(data)
        existing = self._bills.get(key)
        if (
            existing
            and not force
            and existing.fingerprint == fp
            and (version is None or version.lower() in existing.version.lower())
        ):
            self._bills.move_to_end(key)
            return existing

        versions = text_versions(data)
        chosen = choose_version(versions, version)
        if chosen is None:
            if version:
                names = ", ".join(v["version"] for v in versions) or "none"
                raise LookupError(f"No text version matching {version!r} for {bill}. Available: {names}.")
            raise LookupError(f"{bill} ({session}) has no published bill text yet.")
        root = decode_xml(await self.client.site(chosen["url"]))
        loaded = LoadedBill(
            session=session,
            bill=bill,
            version=chosen["version"],
            doc_url=chosen["url"],
            fingerprint=fp,
            summary=summarize_bill(data, session, "brief"),
            sections=parse_sections(root),
            available_versions=[{"version": v["version"], "date": v["date"]} for v in versions],
        )
        self._bills[key] = loaded
        self._bills.move_to_end(key)
        while len(self._bills) > self.max_loaded:
            self._bills.popitem(last=False)
        return loaded

    async def get(self, session: str, bill: str) -> LoadedBill:
        """Return a loaded bill, loading it on first use."""
        existing = self._bills.get(f"{session}/{bill}")
        if existing:
            self._bills.move_to_end(f"{session}/{bill}")
            return existing
        return await self.load(session, bill)

    def unload(self, session: str, bill: str) -> bool:
        return self._bills.pop(f"{session}/{bill}", None) is not None

    def loaded(self) -> list[LoadedBill]:
        return list(self._bills.values())


def find_snippets(loaded: LoadedBill, pattern: str, regex: bool, context_chars: int, max_hits: int) -> dict:
    flags = re.IGNORECASE
    rx = re.compile(pattern if regex else re.escape(pattern), flags)
    hits = []
    total = 0
    for s in loaded.sections:
        for m in rx.finditer(s.text):
            total += 1
            if len(hits) >= max_hits:
                continue
            start = max(0, m.start() - context_chars)
            end = min(len(s.text), m.end() + context_chars)
            snippet = s.text[start:end].replace("\n", " ")
            hits.append(
                {
                    "section": s.id,
                    "offset": m.start(),
                    "snippet": ("…" if start else "") + snippet + ("…" if end < len(s.text) else ""),
                }
            )
    return {"bill": loaded.bill, "version": loaded.version, "pattern": pattern, "total_hits": total, "hits": hits}
