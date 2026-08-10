from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from tools import image_download_retry


@pytest.mark.asyncio
async def test_transient_timeout_and_empty_response_recover_with_attempt_count(
    monkeypatch,
):
    monkeypatch.setattr(
        image_download_retry, "IMAGE_DOWNLOAD_RETRY_DELAY_SECONDS", (0.0, 0.0)
    )
    fetcher = AsyncMock(side_effect=[TimeoutError(), None, b"image-bytes"])
    logger = MagicMock()

    payload, attempts = await image_download_retry.fetch_image_bytes_with_retry(
        fetcher,
        logger=logger,
        label="platform=test post_id=1 source_index=0",
    )

    assert payload == b"image-bytes"
    assert attempts == 3
    assert fetcher.await_count == 3
    assert logger.warning.call_count == 2
    logger.info.assert_called_once()


@pytest.mark.asyncio
async def test_retry_exhaustion_returns_final_attempt_count(monkeypatch):
    monkeypatch.setattr(
        image_download_retry, "IMAGE_DOWNLOAD_RETRY_DELAY_SECONDS", (0.0, 0.0)
    )
    fetcher = AsyncMock(return_value=None)
    logger = MagicMock()

    payload, attempts = await image_download_retry.fetch_image_bytes_with_retry(
        fetcher,
        logger=logger,
        label="platform=test post_id=2 source_index=0",
    )

    assert payload is None
    assert attempts == 3
    assert fetcher.await_count == 3
    assert logger.warning.call_count == 2
    logger.error.assert_called_once()


@pytest.mark.asyncio
async def test_empty_bytes_are_retried_instead_of_treated_as_success(monkeypatch):
    monkeypatch.setattr(
        image_download_retry, "IMAGE_DOWNLOAD_RETRY_DELAY_SECONDS", (0.0, 0.0)
    )
    fetcher = AsyncMock(side_effect=[b"", b"", b"image-bytes"])

    payload, attempts = await image_download_retry.fetch_image_bytes_with_retry(
        fetcher,
        logger=MagicMock(),
        label="platform=test post_id=empty source_index=0",
    )

    assert payload == b"image-bytes"
    assert attempts == 3
    assert fetcher.await_count == 3


@pytest.mark.asyncio
async def test_terminal_http_error_is_not_retried(monkeypatch):
    fetcher = AsyncMock(
        side_effect=image_download_retry.classified_http_image_error(404, "HTTP 404")
    )

    with pytest.raises(image_download_retry.ImageDownloadFetchError) as exc_info:
        await image_download_retry.fetch_image_bytes_with_retry(
            fetcher,
            logger=MagicMock(),
            label="platform=test post_id=missing source_index=0",
        )

    assert exc_info.value.code == "image_source_unavailable"
    assert exc_info.value.http_status == 404
    assert exc_info.value.attempts == 1
    assert fetcher.await_count == 1


@pytest.mark.asyncio
async def test_retryable_http_error_preserves_status_after_exhaustion(monkeypatch):
    monkeypatch.setattr(
        image_download_retry, "IMAGE_DOWNLOAD_RETRY_DELAY_SECONDS", (0.0, 0.0)
    )
    fetcher = AsyncMock(
        side_effect=image_download_retry.classified_http_image_error(503, "HTTP 503")
    )

    with pytest.raises(image_download_retry.ImageDownloadFetchError) as exc_info:
        await image_download_retry.fetch_image_bytes_with_retry(
            fetcher,
            logger=MagicMock(),
            label="platform=test post_id=unavailable source_index=0",
        )

    assert exc_info.value.code == "image_download_retryable"
    assert exc_info.value.http_status == 503
    assert exc_info.value.attempts == 3
    assert fetcher.await_count == 3
