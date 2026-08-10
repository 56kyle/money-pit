"""Module composing the built-in generic source adapter registry."""

from money_pit.config import StrategyIntelligenceConfig
from money_pit.schemas.sources import SourceDefinition
from money_pit.secrets import CredentialResolutionError
from money_pit.secrets import OpenAICredentials
from money_pit.secrets import SourceCredentialResolver
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


def builtin_adapter_registry(
    credentials: SourceCredentialResolver | None,
    strategy: StrategyIntelligenceConfig,
) -> AdapterRegistry:
    """Return a fresh registry containing every bundled connector."""
    registry = AdapterRegistry()
    registry.register("local_text", local_text_connector)
    registry.register("local_audio", local_audio_connector)
    registry.register("local_pdf", local_pdf_connector)
    registry.register("local_email", local_email_connector)

    def imap_connector(definition: SourceDefinition) -> SourceConnector:
        if credentials is None:
            raise CredentialResolutionError("IMAP synchronization requires a credential resolver.")
        return ImapConnector(
            definition,
            credentials=credentials.imap(reason=f"Synchronize IMAP source {definition.source_id}"),
        )

    registry.register("imap", imap_connector)
    registry.register("manual_url", WebConnector)
    registry.register("web", WebConnector)
    registry.register("rss", FeedConnector)
    registry.register("atom", FeedConnector)

    def youtube_connector(definition: SourceDefinition) -> SourceConnector:
        from money_pit.evidence.media import DefaultMediaAnalyzer
        from money_pit.evidence.media import OpenAIVisionFrameReader
        from money_pit.sources.youtube import YouTubeConnector

        if credentials is None:
            raise CredentialResolutionError("YouTube synchronization requires a credential resolver.")
        resolved = credentials.youtube_media(reason=f"Synchronize YouTube source {definition.source_id}")
        return YouTubeConnector(
            definition,
            resolved.youtube_api_key,
            DefaultMediaAnalyzer(
                frame_reader=OpenAIVisionFrameReader(
                    lambda: OpenAICredentials(api_key=resolved.openai_api_key),
                    strategy.llm_model,
                )
            ),
        )

    registry.register("youtube", youtube_connector)
    registry.register("sec_filings", SecFilingsConnector)
    return registry
