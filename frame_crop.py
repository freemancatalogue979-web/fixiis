"""
frame_crop.py — crop CDP screencast frames down to the page-content area.

WHY THIS EXISTS
---------------
Chrome windows have a MINIMUM WINDOW WIDTH (~500 CSS px on headed/Xvfb
Linux; phone compositor surfaces are the full physical screen). When the
emulated page viewport is narrower than the surface — typical for mobile
sessions at 390 CSS px — the page content renders at the TOP-LEFT of the
surface and Chrome paints the remainder as blank white "browser" area.

CDP screencast captures the SURFACE, not the page. That produced:

  1. the white strip on the right of mobile streams, and
  2. wrong coordinate mapping: the admin/PCM canvas scales clicks by the
     frame width (surface) while the server resolves them against the page
     viewport (content), so taps drifted rightward and clicks landed off
     target. PCM mobile was the visible victim.

This module crops every frame to the top-left content rectangle so the
frame IS the page — stream pixels == page CSS pixels — which fixes both
the display and the click mapping at the source, regardless of why the
surface ended up wider (min window width, phone screen, whatever).

Decoder: cv2 (opencv-python-headless, hard dependency) + numpy. If cv2 is
unavailable frames pass through uncropped (degrades to previous behavior,
never crashes the stream).
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)

_cv2 = None
_np = None
_cv_checked = False


def _cv():
    global _cv2, _np, _cv_checked
    if not _cv_checked:
        _cv_checked = True
        try:
            import cv2
            import numpy as np
            _cv2, _np = cv2, np
        except Exception as e:  # pragma: no cover - environment dependent
            logger.warning(f"[frame-crop] cv2/numpy unavailable, crops disabled: {e}")
            _cv2 = False
    return _cv2 or None


def crop_frame_to_content(frame_bytes: bytes,
                          content_w: int,
                          content_h: int,
                          metadata: Optional[dict] = None,
                          quality: int = 100) -> bytes:
    """
    Crop a CDP screencast frame (PNG or JPEG) to the page-content rectangle.

    Args:
        frame_bytes: raw frame bytes (from Page.screencastFrame "data").
            The format is auto-detected from magic bytes and the re-encode
            matches it: PNG stays lossless PNG; JPEG re-encodes at `quality`.
        content_w / content_h: emulated page viewport in CSS px (e.g. 390x844).
        metadata: the screencastFrame "metadata" dict. deviceWidth/deviceHeight
            describe the captured SURFACE in device px and are required to map
            content CSS px -> image px; missing -> frame passes through.
        quality: JPEG quality when the frame is JPEG (ignored for PNG).

    Returns:
        Cropped frame bytes in the SAME format, or the ORIGINAL bytes when
        cropping is impossible or unnecessary (surface already matches content
        within 2 px — the common desktop case — at which point this is a
        zero-cost pass-through).
    """
    if content_w <= 0 or content_h <= 0 or not frame_bytes:
        return frame_bytes
    md = metadata or {}
    try:
        dev_w = float(md.get('deviceWidth') or 0)
        dev_h = float(md.get('deviceHeight') or 0)
    except (TypeError, ValueError):
        return frame_bytes
    if dev_w <= 0 or dev_h <= 0:
        return frame_bytes

    # Fast path: when surface matches content within 2px on desktop widths (>= 500px),
    # cropping is guaranteed unnecessary. Skip the expensive cv2 decode entirely.
    if content_w >= 500 and abs(dev_w - content_w) <= 2 and abs(dev_h - content_h) <= 2:
        return frame_bytes

    cv2 = _cv()
    if cv2 is None:
        return frame_bytes

    try:
        # PNG magic: 89 50 4E 47 | JPEG magic: FF D8
        is_png = frame_bytes[:4] == b'\x89PNG'
        arr = _np.frombuffer(frame_bytes, dtype=_np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            return frame_bytes
        ih, iw = img.shape[:2]
        if iw <= 0 or ih <= 0:
            return frame_bytes

        # Scale image px / content CSS px, per axis.
        #
        # Two metadata regimes exist in the wild:
        #   (a) deviceWidth reports the real SURFACE size (e.g. 500 window
        #       for a 390 page) -> scale = iw/dev_w maps content px exactly.
        #   (b) deviceWidth reports the EMULATED size (390) while the image
        #       still contains the wider surface -> the naive scale sees
        #       "no overshoot" and the white strip survives. In that case
        #       dev_w <= content_w but iw > content_w, and since our
        #       screencast caps never downscale the surfaces we care about
        #       (image == surface 1:1), the only sane mapping is unit
        #       scale: content occupies exactly content_w columns.
        if dev_w > content_w:
            sx = iw / dev_w
        else:
            sx = 1.0
            if iw > content_w + 2:
                _log_once(
                    f"meta-fallback dev_w={dev_w} img_w={iw} "
                    f"content_w={content_w} -> unit-scale crop")
        if dev_h > content_h:
            sy = ih / dev_h
        else:
            sy = 1.0
        cw = min(iw, int(round(content_w * sx)))
        ch = min(ih, int(round(content_h * sy)))
        if cw <= 0 or ch <= 0:
            return frame_bytes
        # No overshoot on either axis -> surface already == content.
        if iw - cw <= 2 and ih - ch <= 2:
            return frame_bytes

        _log_once(
            f"crop img={iw}x{ih} dev={dev_w}x{dev_h} "
            f"content={content_w}x{content_h} -> {cw}x{ch}")

        img = img[:ch, :cw]
        if is_png:
            ok, out = cv2.imencode('.png', img)
        else:
            ok, out = cv2.imencode('.jpg', img,
                                   [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
        if not ok:
            return frame_bytes
        return out.tobytes()
    except Exception as e:
        logger.debug(f"[frame-crop] pass-through ({e})")
        return frame_bytes


_logged_geoms = set()


def _log_once(msg: str):
    """Log each distinct crop geometry once per process (not per frame)."""
    try:
        if msg in _logged_geoms:
            return
        _logged_geoms.add(msg)
        logger.info(f"[frame-crop] {msg}")
    except Exception:
        pass


# Backward-compatible alias for callers still importing the old name.
crop_jpeg_to_content = crop_frame_to_content
