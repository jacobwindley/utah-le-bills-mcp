"""Settings loaded from the environment (and an optional .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_MAX_CHARS = 4000
HARD_MAX_CHARS = 20000
MIN_MAX_CHARS = 500


def load_dotenv(path: Path) -> None:
    """Minimal .env loader: KEY=VALUE lines, never overrides existing env vars."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key and value and key not in os.environ:
            os.environ[key] = value


def _find_dotenv() -> Path | None:
    # Prefer the working directory, then the project root next to src/.
    for candidate in (Path.cwd() / ".env", Path(__file__).resolve().parents[2] / ".env"):
        if candidate.is_file():
            return candidate
    return None


@dataclass(frozen=True)
class Settings:
    token: str | None
    max_chars: int
    cache_dir: Path | None  # None = keep the search index in memory only

    @classmethod
    def from_env(cls) -> Settings:
        if (dotenv := _find_dotenv()) is not None:
            load_dotenv(dotenv)
        try:
            max_chars = int(os.environ.get("LE_MAX_RESPONSE_CHARS", DEFAULT_MAX_CHARS))
        except ValueError:
            max_chars = DEFAULT_MAX_CHARS
        max_chars = max(MIN_MAX_CHARS, min(max_chars, HARD_MAX_CHARS))
        cache_env = os.environ.get("LE_CACHE_DIR", "").strip()
        cache_dir = Path(cache_env).expanduser() if cache_env else None
        return cls(
            token=os.environ.get("LE_UTAH_TOKEN") or None,
            max_chars=max_chars,
            cache_dir=cache_dir,
        )
