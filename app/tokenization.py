from __future__ import annotations

import math
import os
from pathlib import Path

import tiktoken

_O200K_CACHE_KEY = "fb374d419588a4632f3f557e76b4b70aebbca790"


def configure_bundled_tiktoken_cache() -> None:
    """Prefer the checked-in encoding asset so tokenization never needs the network."""

    if os.environ.get("TIKTOKEN_CACHE_DIR"):
        return
    candidates = (
        Path(__file__).resolve().parents[1] / "assets" / "tiktoken",
        Path("/opt/hermesgraph/tiktoken-cache"),
    )
    for candidate in candidates:
        if (candidate / _O200K_CACHE_KEY).is_file():
            os.environ["TIKTOKEN_CACHE_DIR"] = str(candidate)
            return


def load_o200k_encoding() -> tiktoken.Encoding:
    configure_bundled_tiktoken_cache()
    return tiktoken.get_encoding("o200k_base")


class TokenCounter:
    """Model-aligned token accounting with a last-resort offline fallback."""

    def __init__(self) -> None:
        try:
            self._encoding: tiktoken.Encoding | None = load_o200k_encoding()
        except Exception:
            self._encoding = None

    def count(self, text: str) -> int:
        if not text:
            return 0
        if self._encoding is not None:
            return len(self._encoding.encode(text))
        return max(1, math.ceil(len(text) / 3.2))

    def truncate(self, text: str, max_tokens: int) -> str:
        if max_tokens <= 0 or not text:
            return ""
        if self.count(text) <= max_tokens:
            return text
        if self._encoding is not None:
            tokens = self._encoding.encode(text)[:max_tokens]
            return self._encoding.decode(tokens).rstrip()
        return text[: max(1, math.floor(max_tokens * 3.2))].rstrip()
