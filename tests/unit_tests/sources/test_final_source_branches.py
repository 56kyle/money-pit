"""Tests pinning the final reachable source-helper branches."""

from collections.abc import Iterator
from email.message import Message
from typing import cast

from pytest import MonkeyPatch

from money_pit.sources.http import validate_public_http_url
from money_pit.sources.local import (
    _extract_email_text,  # pyright: ignore[reportPrivateUsage]  # Contract test pins the private email composer.
)


def test_validate_public_http_url_with_global_literal_ip() -> None:
    _ = validate_public_http_url("https://8.8.8.8/path")


def test__extract_email_text_with_nonbytes_plain_payload_continues(
    monkeypatch: MonkeyPatch,
) -> None:
    class Part:
        def __init__(self, payload: object) -> None:
            self._payload: object = payload

        def get_content_type(self) -> str:
            return "text/plain"

        def get_payload(self, *, decode: bool) -> object:
            assert decode
            return self._payload

        def get_content_charset(self) -> str:
            return "utf-8"

    class MultipartMessage:
        def is_multipart(self) -> bool:
            return True

        def walk(self) -> Iterator[Part]:
            return iter((Part("not decoded bytes"), Part(b"retained text")))

    message = cast("Message", cast("object", MultipartMessage()))

    def message_from_bytes(_content: bytes) -> Message:
        return message

    monkeypatch.setattr("money_pit.sources.local.email.message_from_bytes", message_from_bytes)

    assert _extract_email_text(b"message") == "retained text"
