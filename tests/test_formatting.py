from le_bills_mcp import formatting as fmt


def test_clean_html_bullets():
    assert fmt.clean_html("This bill:<hr><ltbullet>amends x;<hr><ltbullet>repeals y.") == "This bill:\n- amends x;\n- repeals y."
    assert fmt.clean_html("a&nbsp;b &amp; c") == "a b & c"


def test_slice_text_pages_cover_everything():
    text = " ".join(f"word{i}" for i in range(2000))
    pages, offset = [], 0
    while True:
        page = fmt.slice_text(text, offset, 1000)
        assert len(page["text"]) <= 1000
        pages.append(page["text"])
        if "next_offset" not in page:
            break
        offset = page["next_offset"]
    assert "".join(pages) == text


def test_paginate_respects_budget():
    items = [{"bill": f"HB{i:04d}", "title": "x" * 200} for i in range(100)]
    out = fmt.paginate(items, 0, 50, 2000)
    assert fmt.size(out) <= 2000
    assert out["total"] == 100 and out["next_offset"] == out["returned"]
    assert "truncated" in out


def test_fit_shrinks_to_budget(hb1):
    full = fmt.summarize_bill(hb1, "2026GS", "full")
    assert fmt.size(full) > 2000
    fitted = fmt.fit(full, 2000)
    assert fmt.size(fitted) <= 2000
    assert fitted["bill"] == "HB0001" and "_truncated" in fitted


def test_summarize_bill_levels(hb1):
    brief = fmt.summarize_bill(hb1, "2026GS", "brief")
    assert brief["title"] == "Public Education Base Budget Amendments"
    assert brief["status"] == "Governor Signed"
    assert "general_description" not in brief
    standard = fmt.summarize_bill(hb1, "2026GS", "standard")
    assert "Education" in standard["subjects"]
    assert "53F-2-301" in standard["code_sections_affected"]
    assert "<hr>" not in standard["highlighted_provisions"]
    full = fmt.summarize_bill(hb1, "2026GS", "full")
    assert any(d["url"].startswith("https://le.utah.gov/Session/2026/") for d in full["documents"])


def test_summarize_action_vote(hb1):
    vote = next(a for a in hb1["actionHistoryList"] if a["voteStr"])
    out = fmt.summarize_action(vote)
    assert out["vote"]["tally"] == vote["voteStr"]
