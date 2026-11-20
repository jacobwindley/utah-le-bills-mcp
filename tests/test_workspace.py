from conftest import fixture_bytes

from le_bills_mcp.workspace import LoadedBill, choose_version, decode_xml, find_snippets, parse_sections, text_versions


def _loaded() -> LoadedBill:
    sections = parse_sections(decode_xml(fixture_bytes("HB0001.xml")))
    return LoadedBill("2026GS", "HB0001", "Enrolled", "u", (), {"title": "t"}, sections, [])


def test_sections_parsed():
    lb = _loaded()
    ids = [s.id for s in lb.sections]
    assert ids[:2] == ["intro", "53F-2-301"]
    assert {"S5", "S6", "S7", "S8"} <= set(ids)
    assert all(s.text for s in lb.sections)
    assert "Appropriations" in lb.find_section("S5").heading


def test_amendment_markers():
    text = _loaded().find_section("53F-2-301").text
    assert "[-2025-][+2026+]" in text
    assert "Effective 07/01/26." in text


def test_find_section_aliases():
    lb = _loaded()
    assert lb.find_section("5").id == "S5"
    assert lb.find_section("section 1").id == "53F-2-301"
    assert lb.find_section("53f-2-301") is not None
    assert lb.find_section("nope") is None


def test_find_snippets_limits():
    res = find_snippets(_loaded(), "weighted pupil unit", False, 50, 2)
    assert res["total_hits"] > 2 and len(res["hits"]) == 2
    assert all(len(h["snippet"]) < 200 for h in res["hits"])


def test_toc_has_no_text():
    toc = _loaded().toc()
    assert all(set(s) <= {"id", "heading", "action", "chars"} for s in toc["sections"])


def test_choose_version(hb1):
    versions = text_versions(hb1)
    assert choose_version(versions, None)["file_type"] == "Enrolled"
    assert choose_version(versions, "introduced")["file_type"] == "Introduced"
    assert choose_version(versions, "3rd sub") is None


def test_decode_utf16_bom():
    raw = '<?xml version="1.0" encoding="UTF-16"?><leg><bdy/></leg>'.encode("utf-16")
    assert decode_xml(raw).tag == "leg"
