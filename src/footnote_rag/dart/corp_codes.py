"""corpCode.xml download + local JSON cache (TTL) for stock_code -> corp_code lookups."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from lxml import etree

from footnote_rag.dart.client import DartClient, DartError, read_zip_members


@dataclass(frozen=True)
class CorpInfo:
    corp_code: str
    corp_name: str
    stock_code: str


def parse_corp_code_zip(content: bytes) -> list[CorpInfo]:
    members = read_zip_members(content)
    xml_bytes = next((v for k, v in members.items() if k.lower().endswith(".xml")), None)
    if xml_bytes is None:
        raise DartError("invalid_response", "corpCode ZIP has no XML", retryable=False)
    root = etree.fromstring(xml_bytes, parser=etree.XMLParser(recover=True, huge_tree=True))
    corps: list[CorpInfo] = []
    for item in root.iter("list"):
        stock = (item.findtext("stock_code") or "").strip()
        if not stock:
            continue  # only listed companies are useful for stock_code lookups
        corps.append(
            CorpInfo(
                corp_code=(item.findtext("corp_code") or "").strip(),
                corp_name=(item.findtext("corp_name") or "").strip(),
                stock_code=stock,
            )
        )
    return corps


class CorpCodeCache:
    def __init__(self, client: DartClient, cache_path: Path, ttl_days: int = 7) -> None:
        self._client = client
        self._path = cache_path
        self._ttl_seconds = ttl_days * 86400
        self._by_stock: dict[str, CorpInfo] | None = None

    def _fresh(self) -> bool:
        return (
            self._path.exists() and (time.time() - self._path.stat().st_mtime) < self._ttl_seconds
        )

    async def _load(self) -> dict[str, CorpInfo]:
        if self._by_stock is not None:
            return self._by_stock
        if not self._fresh():
            corps = parse_corp_code_zip(await self._client.download_corp_codes())
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps([c.__dict__ for c in corps], ensure_ascii=False), encoding="utf-8"
            )
        data = json.loads(self._path.read_text(encoding="utf-8"))
        self._by_stock = {d["stock_code"]: CorpInfo(**d) for d in data}
        return self._by_stock

    async def by_stock_code(self, stock_code: str) -> CorpInfo:
        corps = await self._load()
        info = corps.get(stock_code.strip())
        if info is None:
            raise KeyError(f"stock_code {stock_code} not found in corpCode list")
        return info
