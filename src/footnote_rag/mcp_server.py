"""MCP 서버(stdio): Claude Code 같은 MCP 클라이언트에 검색·질문 도구를 노출한다.

역할: DB를 직접 만지지 않고 사용자 JWT로 HTTP API를 호출해 권한 검사와 조직 격리를 그대로 유지한다.
주요 요소: 도구 ``search_documents``(→ ``POST /search``), ``ask``(→ ``POST /questions``), ``_post``.
호출 관계: MCP 클라이언트가 도구를 호출하고, 이 모듈은 ``answering/router.py``의 엔드포인트를 부른다.
학습 문서: docs/study/10-eval-and-mcp.md

MCP server (stdio) exposing footnote-rag to MCP clients such as Claude Code.

It does not touch the database: every tool call goes through the HTTP API with the
caller's access token, so the same authentication, role checks and organization isolation
apply.

Environment:
    FOOTNOTE_API_URL    default http://localhost:8000
    FOOTNOTE_API_TOKEN  JWT from POST /auth/token (required)

Run:  python -m footnote_rag.mcp_server
"""

from __future__ import annotations

import os
from typing import Any

import httpx

# mcp >= 2 renamed FastMCP to MCPServer (same decorator API); support both.
try:
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server

mcp = _Server("footnote-rag")


class ApiError(RuntimeError):
    pass


def _api_client(transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    base_url = os.environ.get("FOOTNOTE_API_URL", "http://localhost:8000").rstrip("/")
    token = os.environ.get("FOOTNOTE_API_TOKEN", "")
    if not token:
        raise ApiError("FOOTNOTE_API_TOKEN is not set (get one from POST /auth/token)")
    return httpx.AsyncClient(
        base_url=base_url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=180.0,
        transport=transport,
    )


async def _post(
    path: str, body: dict[str, Any], transport: httpx.AsyncBaseTransport | None = None
) -> Any:
    async with _api_client(transport) as client:
        resp = await client.post(path, json=body)
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail")
        except ValueError:
            detail = resp.text[:200]
        raise ApiError(f"API {resp.status_code}: {detail}")
    return resp.json()


@mcp.tool()
async def search_documents(query: str, k: int = 8) -> list[dict[str, Any]]:
    """Semantic search over your organization's imported DART filings and uploads.

    Returns the top-k chunks with document title, company, table-of-contents path
    (section_path), similarity score, text and a DART viewer link.
    """
    return await _post("/search", {"query": query, "k": k})


@mcp.tool()
async def ask(question: str) -> dict[str, Any]:
    """Ask a question; the answer cites its evidence with numbered footnotes [n].

    Each footnote carries the document title, company, section_path, the quoted text and a
    DART link. Low-confidence answers come back with status "pending_review".
    """
    return await _post("/questions", {"question": question})


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
