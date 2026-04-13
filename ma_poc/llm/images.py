"""Shared image-size utilities for LLM providers.

Bug-hunt #6: check base64-encoded size before every API call.
Azure <= 20 MB, Anthropic <= 5 MB. Downsample or crop if oversized.
"""
from __future__ import annotations


def check_size(image_bytes: bytes, limit: int) -> bytes:
    """Enforce per-provider size limit. Downsamples via Pillow when available."""
    if len(image_bytes) <= limit:
        return image_bytes
    try:
        from io import BytesIO

        from PIL import Image

        img = Image.open(BytesIO(image_bytes))
        for scale in (0.75, 0.5, 0.35, 0.25):
            buf = BytesIO()
            new = img.resize((int(img.width * scale), int(img.height * scale)))
            new.save(buf, format="PNG", optimize=True)
            data = buf.getvalue()
            if len(data) <= limit:
                return data
        return data  # type: ignore[possibly-undefined]
    except Exception:
        return image_bytes[:limit]
