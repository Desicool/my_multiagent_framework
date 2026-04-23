"""WebSearchTool, WebFetchTool — search and fetch via httpx."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx

from beidou.tools.base import BaseTool

if TYPE_CHECKING:
    from beidou.context import AgentContext

_TIMEOUT = 30.0
_HEADERS = {"User-Agent": "Beidou/0.1 (+https://github.com/beidou-agent)"}


class WebSearchTool(BaseTool):
    """Search the web using DuckDuckGo instant-answer API (no auth required)."""

    @property
    def name(self) -> str:
        return "web_search"

    def schema(self) -> dict:
        return {
            "name": "web_search",
            "description": "Search the web and return a list of results with titles and snippets.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The search query."},
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of results to return (default 5).",
                        "default": 5,
                    },
                },
                "required": ["query"],
            },
        }

    async def execute(self, ctx: "AgentContext", query: str, max_results: int = 5, **_: Any) -> dict:
        async with httpx.AsyncClient(headers=_HEADERS, timeout=_TIMEOUT, follow_redirects=True) as client:
            resp = await client.get(
                "https://api.duckduckgo.com/",
                params={"q": query, "format": "json", "no_html": "1", "skip_disambig": "1"},
            )
        data = resp.json()
        results: list[dict] = []

        if data.get("AbstractText"):
            results.append({"title": data.get("Heading", ""), "snippet": data["AbstractText"], "url": data.get("AbstractURL", "")})

        for r in data.get("RelatedTopics", [])[:max_results]:
            if "Text" in r:
                results.append({"title": r.get("Text", "")[:80], "snippet": r.get("Text", ""), "url": r.get("FirstURL", "")})

        return {"query": query, "results": results[:max_results]}


class WebFetchTool(BaseTool):
    """Fetch a URL and return the response body (text)."""

    @property
    def name(self) -> str:
        return "web_fetch"

    def schema(self) -> dict:
        return {
            "name": "web_fetch",
            "description": "Fetch the content of a URL and return the response text.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The URL to fetch."},
                    "max_chars": {
                        "type": "integer",
                        "description": "Truncate response to this many characters (default 8000).",
                        "default": 8000,
                    },
                },
                "required": ["url"],
            },
        }

    async def execute(self, ctx: "AgentContext", url: str, max_chars: int = 8000, **_: Any) -> dict:
        async with httpx.AsyncClient(headers=_HEADERS, timeout=_TIMEOUT, follow_redirects=True) as client:
            resp = await client.get(url)
        text = resp.text[:max_chars]
        return {
            "url": str(resp.url),
            "status_code": resp.status_code,
            "content": text,
            "truncated": len(resp.text) > max_chars,
        }
