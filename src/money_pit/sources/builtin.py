"""Module composing the built-in generic source adapter registry."""

from money_pit.sources.feeds import FeedConnector
from money_pit.sources.http import WebConnector
from money_pit.sources.imap import ImapConnector
from money_pit.sources.local import local_audio_connector
from money_pit.sources.local import local_email_connector
from money_pit.sources.local import local_pdf_connector
from money_pit.sources.local import local_text_connector
from money_pit.sources.registry import AdapterRegistry
from money_pit.sources.sec import SecFilingsConnector
from money_pit.sources.youtube import YouTubeConnector


def builtin_adapter_registry() -> AdapterRegistry:
    """Return a fresh registry containing every bundled connector."""
    registry = AdapterRegistry()
    registry.register("local_text", local_text_connector)
    registry.register("local_audio", local_audio_connector)
    registry.register("local_pdf", local_pdf_connector)
    registry.register("local_email", local_email_connector)
    registry.register("imap", ImapConnector)
    registry.register("manual_url", WebConnector)
    registry.register("web", WebConnector)
    registry.register("rss", FeedConnector)
    registry.register("atom", FeedConnector)
    registry.register("youtube", YouTubeConnector)
    registry.register("sec_filings", SecFilingsConnector)
    return registry
