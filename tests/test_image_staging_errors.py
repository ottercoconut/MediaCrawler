from __future__ import annotations

from io import BytesIO

from PIL import Image
import pytest

from tools import image_manifest


def png_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGB", (8, 6), color="blue").save(output, format="PNG")
    return output.getvalue()


def test_byte_limit_uses_formal_terminal_error_code(monkeypatch) -> None:
    monkeypatch.setattr(image_manifest, "MAX_IMAGE_BYTES", 3)

    with pytest.raises(image_manifest.ImageStagingError) as exc_info:
        image_manifest.inspect_image_bytes(b"four")

    assert exc_info.value.code == "image_too_large"


def test_non_image_response_uses_formal_terminal_error_code() -> None:
    with pytest.raises(image_manifest.ImageStagingError) as exc_info:
        image_manifest.inspect_image_bytes(b"<html>not an image</html>")

    assert exc_info.value.code == "image_non_raster_response"


def test_truncated_recognized_raster_uses_decode_error_code() -> None:
    content = png_bytes()

    with pytest.raises(image_manifest.ImageStagingError) as exc_info:
        image_manifest.inspect_image_bytes(content[: len(content) // 2])

    assert exc_info.value.code == "image_decode_failed"
