"""Named adapter implementations for the configurable integration registry."""

from __future__ import annotations

import ipaddress
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol
from urllib.parse import urlsplit, urlunsplit

from .providers import ConfiguredAdapter, ProviderError


class ManualCalendarAdapter:
    name = "manual"
    capability = "calendar"

    def free_busy(self, windows: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
        return [dict(window) for window in windows]


class ICSCalendarAdapter(ManualCalendarAdapter):
    name = "ics"


class GoogleCalendarAdapter(ManualCalendarAdapter):
    name = "google_calendar"

    def free_busy(self, windows: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
        raise ProviderError("Google Calendar requires the credential wall and an authenticated live smoke")


class ManualTourAdapter:
    name = "manual_import"
    capability = "tour_intelligence"

    def events(self, values: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
        return [dict(value) for value in values]


class ApprovedRssTourAdapter(ManualTourAdapter):
    name = "approved_rss"


class SongkickTourAdapter(ManualTourAdapter):
    name = "songkick"

    def events(self, values: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
        raise ProviderError("Songkick requires an authenticated live smoke")


class RSSDistributionAdapter:
    name = "rss"
    capability = "distribution"

    def prepare(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return {"mode": "draft", "payload": dict(payload)}


class YouTubeMetadataAdapter(RSSDistributionAdapter):
    name = "youtube_metadata"


class ClipQueueAdapter(RSSDistributionAdapter):
    """Short-form clip preparation for TikTok, Reels, and Shorts.

    It prepares the queue entry; the upload itself stays a human action, so the
    delivery is recorded from a manual receipt like every other platform.
    """

    name = "clip_queue"


class ManualExternalReceiptAdapter(RSSDistributionAdapter):
    name = "manual_external_receipt"


class AssetAmplifierAdapter(ConfiguredAdapter):
    """Named boundary for the external Asset Amplifier estate adapter."""


# ---------------------------------------------------------------------------
# Research sources.
#
# Brave, SerpAPI, and SearXNG answer one bounded query; the allowlisted crawler
# fetches named documents.  Every adapter is transport-injected: it shapes a
# request and parses a response and never opens a socket itself.  The default
# transport refuses to execute, so an unconfigured estate produces a visible
# blocked state instead of an unbounded live fetch, and the fixture transport
# replays a recorded synthetic corpus with no network at all.
# ---------------------------------------------------------------------------

RESEARCH_CAPABILITY = "research"
MAX_RESEARCH_RESULTS = 10
MAX_RESEARCH_SEEDS = 5
MAX_SOURCE_URL_LENGTH = 512
UNPARSEABLE_SOURCE = "unparseable-source"
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")
_RESEARCH_QUERY_TEXT = re.compile(r"^[^\x00-\x1f\x7f]{3,200}$")


def normalize_source_url(value: Any) -> str:
    """Return a canonical https research URL, or raise on an unsafe one.

    The crawler fetches whatever this returns, so the checks are deliberately
    strict: https only, no embedded credentials, no explicit port, no loopback
    name, and no IP literal. Nothing here can be aimed at a private network.
    """
    text = str(value or "").strip()
    if not text or len(text) > MAX_SOURCE_URL_LENGTH or _CONTROL_CHARACTERS.search(text):
        raise ProviderError("research source URL is malformed")
    try:
        parsed = urlsplit(text)
        host = parsed.hostname
        port = parsed.port
        username = parsed.username
        password = parsed.password  # allow-secret: URL component, never a value
    except ValueError as exc:
        raise ProviderError("research source URL is malformed") from exc
    if parsed.scheme != "https" or not host:
        raise ProviderError("research sources must be https URLs with a named host")
    if username or password:
        raise ProviderError("research sources may not embed credentials")
    if port is not None:
        raise ProviderError("research sources may not name an explicit port")
    normalized_host = host.lower()
    if normalized_host == "localhost" or normalized_host.endswith(".localhost"):
        raise ProviderError("research sources may not name a loopback host")
    try:
        ipaddress.ip_address(normalized_host)
    except ValueError:
        pass
    else:
        raise ProviderError("research sources must be named hosts, never IP literals")
    return urlunsplit(("https", normalized_host, parsed.path or "/", parsed.query, ""))


def source_label(value: Any) -> str:
    """Return ``scheme://host`` for risk reporting without echoing a full URL."""
    try:
        parsed = urlsplit(str(value or ""))
        host = parsed.hostname
    except ValueError:
        return UNPARSEABLE_SOURCE
    if not parsed.scheme or not host:
        return UNPARSEABLE_SOURCE
    return f"{parsed.scheme}://{host.lower()}"


def source_allowed(value: Any, allowed_sources: Iterable[Any]) -> bool:
    """True when the URL is a safe https URL under an allowlisted prefix."""
    try:
        candidate = urlsplit(normalize_source_url(value))
    except ProviderError:
        return False
    for source in allowed_sources:
        try:
            allowed = urlsplit(normalize_source_url(source))
        except ProviderError:
            continue
        if allowed.netloc == candidate.netloc and candidate.path.startswith(allowed.path or "/"):
            return True
    return False


@dataclass(frozen=True)
class ResearchQuery:
    """One bounded research request: text, a result ceiling, and named seeds."""

    text: str
    limit: int = 5
    seeds: tuple[str, ...] = ()
    allowed_sources: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not _RESEARCH_QUERY_TEXT.fullmatch(str(self.text)):
            raise ProviderError("research query text must be 3-200 printable characters")
        if (
            isinstance(self.limit, bool)
            or not isinstance(self.limit, int)
            or not 1 <= self.limit <= MAX_RESEARCH_RESULTS
        ):
            raise ProviderError(f"research result limit must be between 1 and {MAX_RESEARCH_RESULTS}")
        if len(self.seeds) > MAX_RESEARCH_SEEDS:
            raise ProviderError(f"a research job may name at most {MAX_RESEARCH_SEEDS} seed documents")


@dataclass(frozen=True)
class ResearchFinding:
    """One attributable retrieval result, always carrying its own provider."""

    provider: str
    title: str
    url: str
    snippet: str
    rank: int
    published_at: str | None = None

    def as_json(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet,
            "rank": self.rank,
            "published_at": self.published_at,
        }


class ResearchTransport(Protocol):
    def fetch(self, *, provider: str, endpoint: str, params: Mapping[str, Any]) -> Mapping[str, Any]: ...


class UnconfiguredResearchTransport:
    """The default transport: a provider call is a visible blocked state."""

    def fetch(self, *, provider: str, endpoint: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        del endpoint, params
        raise ProviderError(
            f"{provider} has no configured research transport; live retrieval requires "
            "the credential wall and an authenticated smoke receipt",
            503,
        )


class FixtureResearchTransport:
    """Replay a recorded synthetic provider corpus from a fixture directory."""

    def __init__(self, root: str | Path):
        self.root = Path(root)

    @staticmethod
    def slug(value: Any) -> str:
        normalized = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
        return normalized or "query"

    def fetch(self, *, provider: str, endpoint: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        del endpoint
        key = params.get("q") or params.get("url") or ""
        path = self.root / self.slug(provider) / f"{self.slug(key)}.json"
        if not path.is_file():
            raise ProviderError(f"{provider} has no recorded fixture for this bounded query", 404)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ProviderError(f"{provider} fixture is unreadable") from exc
        if not isinstance(payload, Mapping):
            raise ProviderError(f"{provider} fixture must be a JSON object")
        return payload


class ResearchSearchAdapter:
    """Shared contract every named research source implements."""

    capability = RESEARCH_CAPABILITY
    name = "research"
    endpoint = ""
    default_cost_minor = 0

    def __init__(
        self,
        transport: ResearchTransport | None = None,
        *,
        settings: Mapping[str, Any] | None = None,
    ):
        self.transport: ResearchTransport = transport or UnconfiguredResearchTransport()
        self.settings = dict(settings or {})

    @property
    def cost_minor(self) -> int:
        value = self.settings.get("cost_per_query_minor", self.default_cost_minor)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ProviderError(f"{self.name} declares an invalid per-query cost")
        return value

    def cost_for(self, query: ResearchQuery) -> int:
        """Projected cost of running ``query``, charged before the fetch."""
        del query
        return self.cost_minor

    def verify(self) -> dict[str, Any]:
        return {"status": "ready", "provider": self.name, "capability": self.capability}

    def search(self, query: ResearchQuery) -> list[ResearchFinding]:
        payload = self.transport.fetch(
            provider=self.name,
            endpoint=self.resolved_endpoint(),
            params=self.request_params(query),
        )
        if not isinstance(payload, Mapping):
            raise ProviderError(f"{self.name} returned a response that is not a JSON object")
        return self.parse_response(payload)[: query.limit]

    def resolved_endpoint(self) -> str:
        return str(self.settings.get("endpoint", self.endpoint))

    def request_params(self, query: ResearchQuery) -> dict[str, Any]:
        raise NotImplementedError

    def parse_response(self, payload: Mapping[str, Any]) -> list[ResearchFinding]:
        raise NotImplementedError

    @staticmethod
    def clean_text(value: Any, limit: int) -> str:
        return _CONTROL_CHARACTERS.sub(" ", str(value or "")).strip()[:limit]

    @classmethod
    def build_finding(
        cls,
        provider: str,
        rank: int,
        *,
        title: Any,
        url: Any,
        snippet: Any,
        published_at: Any = None,
    ) -> "ResearchFinding | None":
        """Return a finding, keeping an unsafe URL verbatim for risk reporting.

        A result whose URL cannot be normalized is not dropped here: source
        policy is the agent's decision, and a silently vanished result looks
        exactly like a source the provider never returned.
        """
        clean_title = cls.clean_text(title, 200)
        clean_url = cls.clean_text(url, MAX_SOURCE_URL_LENGTH)
        if not clean_title or not clean_url:
            return None
        try:
            clean_url = normalize_source_url(clean_url)
        except ProviderError:
            pass
        return ResearchFinding(
            provider=provider,
            title=clean_title,
            url=clean_url,
            snippet=cls.clean_text(snippet, 400),
            rank=rank,
            published_at=cls.clean_text(published_at, 40) or None,
        )

    @classmethod
    def collect(cls, provider: str, rows: Any, fields: tuple[str, str, str, str]) -> list[ResearchFinding]:
        title_key, url_key, snippet_key, date_key = fields
        findings: list[ResearchFinding] = []
        if not isinstance(rows, list):
            return findings
        for index, item in enumerate(rows, 1):
            if not isinstance(item, Mapping):
                continue
            finding = cls.build_finding(
                provider,
                index,
                title=item.get(title_key),
                url=item.get(url_key),
                snippet=item.get(snippet_key),
                published_at=item.get(date_key),
            )
            if finding is not None:
                findings.append(finding)
        return findings


class ManualCitationsResearchAdapter(ResearchSearchAdapter):
    """Operator-supplied citations: HOSPES performs no retrieval at all."""

    name = "manual_citations"

    def search(self, query: ResearchQuery) -> list[ResearchFinding]:
        del query
        raise ProviderError("manual_citations performs no retrieval; the operator supplies citations directly")


class BraveResearchAdapter(ResearchSearchAdapter):
    name = "brave"
    endpoint = "https://api.search.brave.com/res/v1/web/search"
    default_cost_minor = 5

    def request_params(self, query: ResearchQuery) -> dict[str, Any]:
        return {"q": query.text, "count": query.limit, "result_filter": "web"}

    def parse_response(self, payload: Mapping[str, Any]) -> list[ResearchFinding]:
        web = payload.get("web")
        rows = web.get("results") if isinstance(web, Mapping) else None
        return self.collect(self.name, rows, ("title", "url", "description", "page_age"))


class SerpApiResearchAdapter(ResearchSearchAdapter):
    name = "serpapi"
    endpoint = "https://serpapi.com/search.json"
    default_cost_minor = 25

    def request_params(self, query: ResearchQuery) -> dict[str, Any]:
        return {"q": query.text, "num": query.limit, "engine": "google"}

    def parse_response(self, payload: Mapping[str, Any]) -> list[ResearchFinding]:
        return self.collect(self.name, payload.get("organic_results"), ("title", "link", "snippet", "date"))


class SearxngResearchAdapter(ResearchSearchAdapter):
    name = "searxng"
    endpoint = "https://searxng.internal/search"
    default_cost_minor = 1

    def request_params(self, query: ResearchQuery) -> dict[str, Any]:
        return {"q": query.text, "format": "json", "safesearch": 1}

    def parse_response(self, payload: Mapping[str, Any]) -> list[ResearchFinding]:
        return self.collect(self.name, payload.get("results"), ("title", "url", "content", "publishedDate"))


class AllowlistedCrawlerResearchAdapter(ResearchSearchAdapter):
    """Fetch named documents, and only from the configured source allowlist."""

    name = "allowlisted_crawler"
    endpoint = "https://crawler.internal/document"
    default_cost_minor = 2

    def cost_for(self, query: ResearchQuery) -> int:
        return self.cost_minor * max(len(query.seeds), 1)

    def request_params(self, query: ResearchQuery) -> dict[str, Any]:
        return {"q": query.text}

    def parse_response(self, payload: Mapping[str, Any]) -> list[ResearchFinding]:
        del payload
        raise ProviderError("the allowlisted crawler parses one document at a time")

    def search(self, query: ResearchQuery) -> list[ResearchFinding]:
        if not query.seeds:
            raise ProviderError("the allowlisted crawler requires at least one seed document")
        findings: list[ResearchFinding] = []
        for rank, seed in enumerate(query.seeds, 1):
            target = normalize_source_url(seed)
            if not source_allowed(target, query.allowed_sources):
                raise ProviderError("crawler seed is outside the configured research source allowlist", 403)
            payload = self.transport.fetch(
                provider=self.name,
                endpoint=self.resolved_endpoint(),
                params={"url": target},
            )
            if not isinstance(payload, Mapping):
                raise ProviderError("crawler response must be a JSON object")
            landed = normalize_source_url(payload.get("url", target))
            if landed != target:
                raise ProviderError("crawler response left its requested allowlisted document", 403)
            finding = self.build_finding(
                self.name,
                rank,
                title=payload.get("title"),
                url=landed,
                snippet=payload.get("excerpt", payload.get("text")),
                published_at=payload.get("published_at"),
            )
            if finding is None:
                raise ProviderError("crawler response carries no attributable title")
            findings.append(finding)
        return findings[: query.limit]


RESEARCH_ADAPTERS: dict[str, type[ResearchSearchAdapter]] = {
    ManualCitationsResearchAdapter.name: ManualCitationsResearchAdapter,
    BraveResearchAdapter.name: BraveResearchAdapter,
    SerpApiResearchAdapter.name: SerpApiResearchAdapter,
    SearxngResearchAdapter.name: SearxngResearchAdapter,
    AllowlistedCrawlerResearchAdapter.name: AllowlistedCrawlerResearchAdapter,
}


def research_adapter(
    name: Any,
    *,
    transport: ResearchTransport | None = None,
    settings: Mapping[str, Any] | None = None,
) -> ResearchSearchAdapter:
    """Resolve one registered research source by its configured name."""
    factory = RESEARCH_ADAPTERS.get(str(name))
    if factory is None:
        raise ProviderError(f"{name!r} is not a registered research source")
    return factory(transport, settings=settings)


__all__ = [
    "AllowlistedCrawlerResearchAdapter",
    "ApprovedRssTourAdapter",
    "AssetAmplifierAdapter",
    "BraveResearchAdapter",
    "ClipQueueAdapter",
    "FixtureResearchTransport",
    "GoogleCalendarAdapter",
    "ICSCalendarAdapter",
    "MAX_RESEARCH_RESULTS",
    "MAX_RESEARCH_SEEDS",
    "ManualCalendarAdapter",
    "ManualCitationsResearchAdapter",
    "ManualExternalReceiptAdapter",
    "ManualTourAdapter",
    "RESEARCH_ADAPTERS",
    "RESEARCH_CAPABILITY",
    "RSSDistributionAdapter",
    "ResearchFinding",
    "ResearchQuery",
    "ResearchSearchAdapter",
    "ResearchTransport",
    "SearxngResearchAdapter",
    "SerpApiResearchAdapter",
    "SongkickTourAdapter",
    "UNPARSEABLE_SOURCE",
    "UnconfiguredResearchTransport",
    "YouTubeMetadataAdapter",
    "normalize_source_url",
    "research_adapter",
    "source_allowed",
    "source_label",
]
