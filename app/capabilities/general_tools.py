from __future__ import annotations

import ast
import asyncio
import hashlib
import ipaddress
import math
import operator
import socket
from collections.abc import Awaitable, Callable
from datetime import datetime
from html.parser import HTMLParser
from typing import cast
from urllib.parse import urljoin, urlsplit
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from app.domain.enums import TrustLevel
from app.domain.models import (
    CalculationRequest,
    CalculationResult,
    CurrentTimeRequest,
    CurrentTimeResult,
    EvidenceRef,
    Provenance,
    RunContext,
    WebPageReadRequest,
    WebPageReadResult,
    utc_now,
)
from app.web_search.openai_web_search import normalize_public_web_url

Resolver = Callable[[str], Awaitable[list[str]]]
Number = int | float
NumericCallable = Callable[..., Number]

_BINARY_OPERATORS: dict[type[ast.operator], NumericCallable] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPERATORS: dict[type[ast.unaryop], NumericCallable] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}
_FUNCTIONS: dict[str, NumericCallable] = {
    "abs": abs,
    "ceil": math.ceil,
    "cos": math.cos,
    "exp": math.exp,
    "floor": math.floor,
    "log": math.log,
    "log10": math.log10,
    "max": max,
    "min": min,
    "round": round,
    "sin": math.sin,
    "sqrt": math.sqrt,
    "tan": math.tan,
}
_CONSTANTS = {"e": math.e, "pi": math.pi, "tau": math.tau}


class GeneralToolPolicyError(ValueError):
    pass


class _ReadableHTMLParser(HTMLParser):
    _ignored = {"script", "style", "noscript", "svg", "template"}
    _blocks = {
        "article",
        "blockquote",
        "br",
        "div",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "li",
        "main",
        "nav",
        "p",
        "section",
        "table",
        "td",
        "th",
        "tr",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._ignored_depth = 0
        self._in_title = False
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        tag = tag.lower()
        if tag in self._ignored:
            self._ignored_depth += 1
        elif tag == "title":
            self._in_title = True
        elif tag in self._blocks and not self._ignored_depth:
            self.text_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self._ignored and self._ignored_depth:
            self._ignored_depth -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in self._blocks and not self._ignored_depth:
            self.text_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        if self._in_title:
            self.title_parts.append(data)
        self.text_parts.append(data)


class GeneralToolService:
    """Bounded web-reading and deterministic utility tools."""

    revision = "general-tools-v1"

    def __init__(
        self,
        *,
        timeout_seconds: float = 15.0,
        max_download_bytes: int = 1_000_000,
        allowed_domains: list[str] | None = None,
        client: httpx.AsyncClient | None = None,
        resolver: Resolver | None = None,
    ) -> None:
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=False,
            headers={"User-Agent": "HermesGraph/1.0 (+public-web-reader)"},
        )
        self._owns_client = client is None
        self._max_download_bytes = max_download_bytes
        self._allowed_domains = allowed_domains or []
        self._resolver = resolver or _resolve_host

    async def read_web_page(
        self,
        request: WebPageReadRequest,
        context: RunContext,
    ) -> WebPageReadResult:
        current_url = await self._validate_target(request.url)
        redirect_count = 0
        while True:
            async with self._client.stream("GET", current_url) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location or redirect_count >= 3:
                        raise GeneralToolPolicyError("web page redirect limit exceeded")
                    current_url = await self._validate_target(urljoin(current_url, location))
                    redirect_count += 1
                    continue
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").split(";", 1)[0]
                if content_type not in {"text/html", "text/plain", "application/xhtml+xml"}:
                    raise GeneralToolPolicyError("web page content type is not readable text")
                body, byte_truncated = await self._bounded_body(response)
                encoding = response.encoding or "utf-8"
                decoded = body.decode(encoding, errors="replace")
                break

        if content_type in {"text/html", "application/xhtml+xml"}:
            parser = _ReadableHTMLParser()
            parser.feed(decoded)
            title = _collapse_text(" ".join(parser.title_parts)) or (
                urlsplit(current_url).hostname or "Web page"
            )
            text = _collapse_lines("".join(parser.text_parts))
        else:
            title = urlsplit(current_url).hostname or "Web page"
            text = _collapse_lines(decoded)
        char_truncated = len(text) > request.max_chars
        text = text[: request.max_chars].rstrip()
        if not text:
            raise GeneralToolPolicyError("web page did not contain readable text")
        observed_at = utc_now()
        evidence_key = f"{self.revision}:{current_url}:{hashlib.sha256(text.encode()).hexdigest()}"
        evidence = EvidenceRef(
            evidence_id=uuid5(NAMESPACE_URL, evidence_key),
            text=text,
            title=title[:500],
            score=1.0,
            provenance=Provenance(
                source_type="web_page",
                source_id=current_url,
                run_id=context.run_id,
                content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                locator={"uri": current_url},
                trust=TrustLevel.UNTRUSTED,
                observed_at=observed_at,
            ),
            metadata={
                "uri": current_url,
                "web_page": {"reader_revision": self.revision},
            },
        )
        return WebPageReadResult(
            url=current_url,
            title=title[:500],
            text=text,
            content_type=content_type,
            truncated=byte_truncated or char_truncated,
            evidence=[evidence],
            trace={
                "reader_revision": self.revision,
                "redirect_count": redirect_count,
                "downloaded_bytes": len(body),
            },
        )

    async def calculate(
        self,
        request: CalculationRequest,
        context: RunContext,
    ) -> CalculationResult:
        del context
        expression = " ".join(request.expression.split())
        try:
            parsed = ast.parse(expression, mode="eval")
            value = _evaluate(parsed.body, depth=0)
        except (SyntaxError, TypeError, ValueError, ZeroDivisionError, OverflowError) as exc:
            raise GeneralToolPolicyError(f"invalid calculation: {type(exc).__name__}") from exc
        if isinstance(value, float) and not math.isfinite(value):
            raise GeneralToolPolicyError("calculation result must be finite")
        return CalculationResult(expression=expression, result=str(value))

    async def current_time(
        self,
        request: CurrentTimeRequest,
        context: RunContext,
    ) -> CurrentTimeResult:
        del context
        try:
            zone = ZoneInfo(request.timezone)
        except ZoneInfoNotFoundError as exc:
            raise GeneralToolPolicyError("unknown IANA timezone") from exc
        now = datetime.now(zone)
        return CurrentTimeResult(
            timezone=request.timezone,
            iso8601=now.isoformat(timespec="seconds"),
            date=now.date().isoformat(),
            time=now.time().isoformat(timespec="seconds"),
            utc_offset=now.strftime("%z")[:3] + ":" + now.strftime("%z")[3:],
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _validate_target(self, value: str) -> str:
        normalized = normalize_public_web_url(value, self._allowed_domains)
        if normalized is None:
            raise GeneralToolPolicyError("URL is not an allowed public HTTP(S) target")
        host = urlsplit(normalized).hostname
        if host is None:
            raise GeneralToolPolicyError("URL host is missing")
        addresses = await self._resolver(host)
        if not addresses or any(not ipaddress.ip_address(item).is_global for item in addresses):
            raise GeneralToolPolicyError("URL resolves to a non-public network address")
        return normalized

    async def _bounded_body(self, response: httpx.Response) -> tuple[bytes, bool]:
        declared = response.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > self._max_download_bytes:
            raise GeneralToolPolicyError("web page exceeds the download size limit")
        chunks: list[bytes] = []
        size = 0
        truncated = False
        async for chunk in response.aiter_bytes():
            remaining = self._max_download_bytes - size
            if len(chunk) > remaining:
                chunks.append(chunk[:remaining])
                truncated = True
                break
            chunks.append(chunk)
            size += len(chunk)
        return b"".join(chunks), truncated


async def _resolve_host(host: str) -> list[str]:
    def resolve() -> list[str]:
        return list({cast(str, item[4][0]) for item in socket.getaddrinfo(host, None)})

    try:
        return await asyncio.to_thread(resolve)
    except socket.gaierror as exc:
        raise GeneralToolPolicyError("URL host could not be resolved") from exc


def _evaluate(node: ast.AST, *, depth: int) -> Number:
    if depth > 24:
        raise ValueError("expression is too deeply nested")
    if isinstance(node, ast.Constant) and type(node.value) in {int, float}:
        value: Number = cast(Number, node.value)
    elif isinstance(node, ast.Name) and node.id in _CONSTANTS:
        value = _CONSTANTS[node.id]
    elif isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPERATORS:
        value = _UNARY_OPERATORS[type(node.op)](_evaluate(node.operand, depth=depth + 1))
    elif isinstance(node, ast.BinOp) and type(node.op) in _BINARY_OPERATORS:
        left = _evaluate(node.left, depth=depth + 1)
        right = _evaluate(node.right, depth=depth + 1)
        if isinstance(node.op, ast.Pow) and abs(right) > 100:
            raise ValueError("exponent is too large")
        value = _BINARY_OPERATORS[type(node.op)](left, right)
    elif (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in _FUNCTIONS
        and not node.keywords
        and 1 <= len(node.args) <= 10
    ):
        value = _FUNCTIONS[node.func.id](*[_evaluate(item, depth=depth + 1) for item in node.args])
    else:
        raise ValueError("expression contains an unsupported operation")
    if type(value) not in {int, float} or abs(value) > 1e100:
        raise ValueError("calculation result is outside the allowed range")
    return value


def _collapse_text(value: str) -> str:
    return " ".join(value.split())


def _collapse_lines(value: str) -> str:
    lines = [_collapse_text(line) for line in value.splitlines()]
    return "\n".join(line for line in lines if line)
