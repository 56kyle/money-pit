"""Module composing the built-in generic source adapter registry."""

from money_pit.config import Config
from money_pit.config import CredentialResolutionError
from money_pit.schemas.sources import SourceDefinition
from money_pit.sources.feeds import FeedConnector
from money_pit.sources.http import WebConnector
from money_pit.sources.imap import ImapConnector
from money_pit.sources.local import local_audio_connector
from money_pit.sources.local import local_email_connector
from money_pit.sources.local import local_pdf_connector
from money_pit.sources.local import local_text_connector
from money_pit.sources.protocol import SourceConnector
from money_pit.sources.registry import AdapterRegistry
from money_pit.sources.sec import SecFilingsConnector


def builtin_adapter_registry(config: Config) -> AdapterRegistry:
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

    def youtube_connector(definition: SourceDefinition) -> SourceConnector:
        from money_pit.evidence.media import DefaultMediaAnalyzer
        from money_pit.evidence.media import OpenAIVisionFrameReader
        from money_pit.sources.youtube import YouTubeConnector

        credential = config.youtube_api_key
        if credential is None or not credential.get_secret_value().strip():
            raise CredentialResolutionError("MONEY_PIT__YOUTUBE_API_KEY is required for YouTube synchronization.")
        return YouTubeConnector(
            definition,
            credential,
            DefaultMediaAnalyzer(frame_reader=OpenAIVisionFrameReader(config)),
        )

    registry.register("youtube", youtube_connector)
    registry.register("sec_filings", SecFilingsConnector)
    return registry
