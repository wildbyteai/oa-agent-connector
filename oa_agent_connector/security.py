from __future__ import annotations

import re
import urllib.parse
from typing import Any


_SENSITIVE_ERROR_MARKER = re.compile(
    r"(?i)(?:password|passwd|j_password|cookie|set-cookie|jsessionid|authorization)"
)


def sanitize_error_message(message: Any, limit: int = 200) -> str:
    text = str(message or "")
    try:
        decoded = urllib.parse.unquote_plus(text)
    except Exception:
        decoded = text
    if _SENSITIVE_ERROR_MARKER.search(text) or _SENSITIVE_ERROR_MARKER.search(decoded):
        return "请求失败，敏感内容已隐藏"
    text = re.sub(r"<script\b.*?</script>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<style\b.*?</style>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()[: max(1, int(limit))]
