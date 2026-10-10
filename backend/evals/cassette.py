"""按完整 user 消息哈希存取 LLM 响应。gather 的完成顺序不影响查找。"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

# ponytail: propose 的 user 文本含每次新生成的 item UUID。哈希前抹掉，否则回放永远 miss。
_UUID = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)

CASSETTE_DIR = Path(__file__).resolve().parent / "cassettes"


def user_hash(text: str) -> str:
    return hashlib.sha256(_UUID.sub("{id}", text).encode("utf-8")).hexdigest()


def last_user_text(payload: dict[str, Any]) -> str:
    users = [
        str(message.get("content") or "")
        for message in payload.get("messages") or []
        if message.get("role") == "user"
    ]
    return users[-1] if users else ""


def load_cassettes(directory: Path = CASSETTE_DIR) -> tuple[dict[str, dict[str, Any]], list[str]]:
    entries: dict[str, dict[str, Any]] = {}
    versions: list[str] = []
    for path in sorted(directory.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        versions.append(str(data.get("prompt_version") or ""))
        entries.update(data.get("entries") or {})
    return entries, versions


def dump_cassette(path: Path, prompt_version: str, entries: dict[str, dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"prompt_version": prompt_version, "entries": entries}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
