import json
import os
from pathlib import Path

# Isolate tests from a developer's real .env/token and on-disk cache. Must run before
# le_bills_mcp.server is imported (it reads settings at import time).
os.environ["LE_UTAH_TOKEN"] = ""
os.environ["LE_CACHE_DIR"] = ""  # memory-only index, the default
os.environ["LE_MAX_RESPONSE_CHARS"] = "4000"

import pytest  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def fixture_json(name: str):
    return json.loads(fixture_bytes(name).decode("utf-8-sig"))


@pytest.fixture
def hb1() -> dict:
    return fixture_json("data_2026GS_HB0001.json")
