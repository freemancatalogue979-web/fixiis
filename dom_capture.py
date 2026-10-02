"""DOM capture engine for remote browser sessions.

Design
------
Two-tier live-mirror pipeline (see MIGRATION_LIVE_MIRROR.md):

1. **Fast capture** (default, ``DOM_CAPTURE_MODE=fast``): a single
   CSP-immune ``page.evaluate`` serializes the live DOM in-page
   (live form state materialized, open shadow roots emitted as
   declarative shadow DOM, same-origin canvases as data-URL images).
   External asset URLs are rewritten server-side to the
   content-addressed ``/assets/<hash>`` cache (fetch-once, immutable)
   so recaptures render from the browser HTTP cache.  Interaction
   recaptures skip the legacy load/networkidle settle entirely.

2. **Legacy SingleFile** (``DOM_CAPTURE_MODE=singlefile``): the
   bundled MV3 SingleFile extension inlines every asset into one
   self-contained document.  Kept for archive flows (PCM page
   manager / LPV store) where self-containment is the point, and as
   the automatic fallback whenever the fast serializer comes back
   degenerate.

Between full captures an in-page MutationObserver (``LIVE_DELTA=1``)
streams compact DOM ops (``dom_patch`` frames) which the client
applies in place instead of rebuilding the snapshot iframe.  This
patch-first path is used for both live-preferred and snapshot-preferred
hosts; full documents remain the navigation, overflow, and recovery
floor.  Node identity is carried by ``data-mid`` stamps assigned during
serialization (WeakMap + counter page-side).

Captures coalesce (``SEND_COALESCE=1``): at most one capture in
flight per session; overlapping triggers produce exactly one
follow-up send with the latest reason.  Unchanged-DOM checksums
(``DOM_CAPTURE_SKIP_UNCHANGED=1``) suppress no-op recaptures before
paying for them.

The caller drives capture timing: explicitly call ``send_page``
when you want a fresh snapshot (initial, on navigation, on
demand).  Event handlers (handle_click / handle_navigation /
handle_keypress / handle_submit_form) just dispatch to
Playwright; they do not auto-capture.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlsplit

# Shared JSON helper for CDP evaluation payloads.
_json = json

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration (env-overridable)
# ---------------------------------------------------------------------------

PRE_CAPTURE_WAIT: float = float(os.environ.get("DOM_CAPTURE_PRE_CAPTURE_WAIT", "0.05"))
SINGLEFILE_TIMEOUT_S: int = int(os.environ.get("DOM_CAPTURE_SINGLEFILE_TIMEOUT_S", "60"))

# Minimum interval between interaction-triggered recaptures.  This is
# purely a flood-control knob; it does NOT decide whether a click is
# "real" -- that decision is made in the page by _INTERACTION_TRIGGER_JS.
# The delta observer carries the first visual update; this only throttles
# the slower full-document safety capture.
INTERACTION_CAPTURE_MIN_INTERVAL_S: float = float(
    os.environ.get("DOM_CAPTURE_INTERACTION_MIN_INTERVAL_S", "0.03")
)

# Snapshot-mode interaction cadence when the delta channel is unavailable.
# Healthy snapshot-preferred pages patch in place and do not use this full
# capture throttle for ordinary interactions.
SNAPSHOT_CAPTURE_MIN_INTERVAL_S: float = float(
    os.environ.get("DOM_SNAPSHOT_MIN_INTERVAL_S", "0.15")
)

# MutationObserver -> websocket batching. 8 ms matches 120 Hz displays
# so mutations stream to the client instantly without skipped frames.
DELTA_FLUSH_MS: int = max(0, int(os.environ.get("DOM_DELTA_FLUSH_MS", "8")))

# URL polling is local (page.url is cached on the adapters), so 100 ms catches
# a navigation much sooner than the old 500 ms loop without a CDP round-trip.
URL_WATCH_INTERVAL_S: float = max(
    0.05, float(os.environ.get("DOM_URL_WATCH_INTERVAL_S", "0.1"))
)

# Hosts that prefer a full-document consistency cadence.  Every host can
# still use the live delta (DOM-mutation) pipeline when LIVE_DELTA is enabled;
# snapshot preference now means "keep full captures as the fidelity floor",
# not "disable in-place patches".  Heavy sites remain on the more aggressive
# live cadence (google/netflix/comcast/...).
_LIVE_DOM_HOSTS_RAW = os.environ.get(
    "DOM_LIVE_HOSTS", "google.com,netflix.com,comcast.com,youtube.com,gstatic.com"
)
_LIVE_DOM_HOSTS = tuple(
    h.strip().lower() for h in _LIVE_DOM_HOSTS_RAW.split(",") if h.strip()
)


def prefer_snapshot_for_url(url: str) -> bool:
    """True when the URL should keep full captures as its fidelity floor.

    The return value no longer disables delta patches; it only selects the
    more conservative full-capture cadence.  All hosts still get safe
    in-place updates when LIVE_DELTA is enabled.
    """
    if not url:
        return True
    try:
        from urllib.parse import urlparse
        host = (urlparse(url if "://" in url else "https://" + url).hostname or "").lower()
    except Exception:
        host = ""
    if not host:
        return True
    for h in _LIVE_DOM_HOSTS:
        if host == h or host.endswith("." + h):
            return False
    return True



# ---------------------------------------------------------------------------
# Fast-capture pipeline (see MIGRATION_LIVE_MIRROR.md)
# ---------------------------------------------------------------------------
#
# Capture backend for the LIVE victim mirror:
#   fast        — in-page serializer + asset-cache rewrite (default)
#   singlefile  — legacy SingleFile extension round-trip (pre-overhaul)
# Legacy Playwright/direct archive flows (PCM page manager, LPV store) keep
# SingleFile regardless; the SeleniumBase path does not load the extension:
# self-containment is the point there, not latency.
DOM_CAPTURE_MODE: str = os.environ.get("DOM_CAPTURE_MODE", "fast").strip().lower()

# Coalesce interaction captures: at most one capture in flight per session;
# triggers that arrive mid-capture set a dirty bit and produce exactly one
# follow-up send carrying the latest reason.
SEND_COALESCE: bool = os.environ.get("SEND_COALESCE", "1").strip().lower() not in ("0", "false", "no", "off")

# Skip the capture entirely when the cheap DOM checksum hasn't moved since
# the last send (hover/poll re-triggers become no-ops).
SKIP_UNCHANGED: bool = os.environ.get("DOM_CAPTURE_SKIP_UNCHANGED", "1").strip().lower() not in ("0", "false", "no", "off")

# Live-delta layer: in-page MutationObserver streams compact DOM ops between
# full captures; client applies them in place instead of rebuilding the iframe.
LIVE_DELTA: bool = os.environ.get("LIVE_DELTA", "1").strip().lower() not in ("0", "false", "no", "off")

# Debug/rollback: force the legacy "load + networkidle + readyState" settle
# on EVERY capture, including interaction recaptures.
FULL_SETTLE: bool = os.environ.get("DOM_CAPTURE_FULL_SETTLE", "").strip().lower() in ("1", "true", "yes", "on")

# Asset-cache fetcher bounds (server-side fetch-once rewrite).
# Raised from 48 -> 256: the cap silently dropped assets from heavy pages
# (yahoo: half the stylesheets just never got fetched — the mirror lost
# "some of the CSS").  256 covers real-world pages; env to tune down.
ASSET_MAX_PER_PAGE: int = int(os.environ.get("DOM_CAPTURE_ASSET_MAX_PER_PAGE", "256"))
ASSET_FETCH_TIMEOUT_S: float = float(os.environ.get("DOM_CAPTURE_ASSET_TIMEOUT_S", "6"))
ASSET_MAX_BYTES: int = int(os.environ.get("DOM_CAPTURE_ASSET_MAX_BYTES", str(12 * 1024 * 1024)))
CACHE_MAX_ITEMS: int = int(os.environ.get("DOM_CAPTURE_CACHE_MAX_ITEMS", "4000"))
CACHE_MAX_BYTES: int = int(os.environ.get("DOM_CAPTURE_CACHE_MAX_BYTES", str(192 * 1024 * 1024)))

# Images this size or smaller are EMBEDDED as data: URIs in the captured
# HTML (logos, icons, SVGs, GIFs) instead of pointing at /assets/<hash> —
# the mirror renders them even when the client can't reach the /assets
# route (reverse proxies that only forward /ws, offline viewers, copied
# HTML artifacts).  Larger images keep the cache path so recaptures don't
# re-ship megabytes.  0 disables embedding entirely.
EMBED_MAX_BYTES: int = int(os.environ.get("DOM_CAPTURE_EMBED_MAX_BYTES", str(1024 * 1024)))

# Stylesheets this size or smaller are INLINED as <style> blocks in the
# captured HTML (after their inner url()/@import refs are rewritten) instead
# of staying <link href="/assets/<hash>"> — mirrored pages keep their CSS
# even when the client can't reach the /assets route (yahoo-class pages
# render naked without it).  Larger stylesheets keep the cache path.
# 0 disables CSS inlining (back to pure cache refs).
# 3 MB covers virtually every real stylesheet (app bundles included) — mirrors
# must get ALL the CSS, since the client may not reach the /assets route.
CSS_EMBED_MAX_BYTES: int = int(os.environ.get("DOM_CAPTURE_CSS_EMBED_MAX_BYTES", str(3 * 1024 * 1024)))


_CSS_STYLE_CLOSE_RE = re.compile(r"</style", re.IGNORECASE)
_ASSET_PATH_RE = re.compile(r"/assets/([0-9a-f]{64})(?:\.[a-z0-9]+)?", re.IGNORECASE)


def _inline_css_safe(css_text: str) -> str:
    """Make CSS safe to drop inside a <style> element: the HTML parser must
    never see a literal '</style' from CSS strings/comments."""
    return _CSS_STYLE_CLOSE_RE.sub("<\\/style", css_text)


# ---------------------------------------------------------------------------
# SingleFile source loading
# ---------------------------------------------------------------------------

_SINGLEFILE_SOURCES: Optional[Dict[str, str]] = None
_SINGLEFILE_SOURCES_LOCK = asyncio.Lock()


def _find_singlefile_lib_dir() -> Optional[Path]:
    env_override = os.environ.get("SINGLEFILE_LIB_DIR")
    if env_override:
        p = Path(env_override)
        if (p / "lib" / "single-file-script.js").is_file():
            return p
        logger.warning("SINGLEFILE_LIB_DIR=%r set but %s missing", env_override, p / "lib" / "single-file-script.js")

    candidates: List[Path] = [
        Path("/usr/lib/node_modules/single-file-cli"),
        Path("/usr/local/lib/node_modules/single-file-cli"),
        Path("/opt/homebrew/lib/node_modules/single-file-cli"),
        Path.home() / ".npm-global" / "lib" / "node_modules" / "single-file-cli",
        Path.home() / "n" / "lib" / "node_modules" / "single-file-cli",
        Path.home() / ".linuxbrew" / "lib" / "node_modules" / "single-file-cli",
    ]
    nvm_root = Path.home() / ".nvm" / "versions" / "node"
    if nvm_root.is_dir():
        try:
            for v in nvm_root.iterdir():
                if v.is_dir():
                    candidates.append(v / "lib" / "node_modules" / "single-file-cli")
        except OSError:
            pass
    if os.name == "nt":
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        candidates.append(Path(pf) / "nodejs" / "node_modules" / "single-file-cli")
        appdata = os.environ.get("APPDATA")
        if appdata:
            candidates.append(Path(appdata) / "npm" / "node_modules" / "single-file-cli")

    for c in candidates:
        try:
            if (c / "lib" / "single-file-script.js").is_file():
                return c
        except (OSError, PermissionError):
            continue

    npm_path = shutil.which("npm")
    if npm_path:
        try:
            out = subprocess.run([npm_path, "root", "-g"], capture_output=True, text=True, timeout=5)
            if out.returncode == 0 and out.stdout.strip():
                root = Path(out.stdout.strip()) / "single-file-cli"
                if (root / "lib" / "single-file-script.js").is_file():
                    return root
        except Exception:
            pass

    logger.error(
        "single-file-cli not found. Install with `npm install -g single-file-cli` "
        "or set SINGLEFILE_LIB_DIR."
    )
    return None


async def _load_singlefile_sources_uncached() -> Optional[Dict[str, str]]:
    global _SINGLEFILE_SOURCES
    if _SINGLEFILE_SOURCES is not None:
        return _SINGLEFILE_SOURCES

    lib_dir = _find_singlefile_lib_dir()
    if lib_dir is None:
        # Fallback: use the shipped MV3 extension's bundled SingleFile
        # sources directly so library-injection still works even when
        # `single-file-cli` is not installed (e.g. in minimal Docker).
        # This keeps Yahoo / strict-CSP fallback from being "hrabbinyme"
        # when only the extension is present.
        try:
            candidates = [
                Path(__file__).resolve().parent / "single",
                Path.cwd() / "single",
                Path.home() / "shifixsxs" / "single",
                Path("/home/user/shifixsxs/single"),
            ]
            env_dir = os.environ.get("SINGLEFILE_EXT_DIR", "").strip()
            if env_dir:
                candidates.insert(0, Path(env_dir))
            ext_lib = None
            for cand in candidates:
                cand_lib = Path(os.path.abspath(cand)) / "lib"
                if (cand_lib / "single-file.js").is_file() and (cand_lib / "single-file-hooks-frames.js").is_file():
                    ext_lib = cand_lib
                    break
            if ext_lib is not None:
                hook_src = (ext_lib / "single-file-hooks-frames.js").read_text(encoding="utf-8", errors="ignore")
                main_src = (ext_lib / "single-file.js").read_text(encoding="utf-8", errors="ignore")
                zip_candidates = [ext_lib / "single-file-zip.js", ext_lib / "single-file-zip.min.js"]
                zip_src = ""
                for zc in zip_candidates:
                    if zc.is_file():
                        zip_src = zc.read_text(encoding="utf-8", errors="ignore")
                        break
                if hook_src and main_src:
                    _SINGLEFILE_SOURCES = {"hook": hook_src, "main": main_src, "zip": zip_src}
                    logger.debug("SingleFile lib: using extension bundled sources (no CLI) from %s", ext_lib)
                    return _SINGLEFILE_SOURCES
        except Exception as _e:
            logger.debug("SingleFile lib: extension fallback failed: %s", _e)
        return None

    script_path = lib_dir / "lib" / "single-file-script.js"
    script_uri = script_path.resolve().as_uri()
    extractor = (
        "import * as sf from %r;"
        "const o = {"
        "  hook: await sf.getHookScriptSource(),"
        "  main: await sf.getScriptSource({}),"
        "  zip: await sf.getZipScriptSource()"
        "};"
        "process.stdout.write('###SPLIT###' + JSON.stringify(o) + '###END###');"
    ) % script_uri

    def _run() -> subprocess.CompletedProcess:
        return subprocess.run(
            ["node", "--input-type=module", "-e", extractor],
            capture_output=True, text=True, timeout=30,
        )

    try:
        result = await asyncio.to_thread(_run)
    except Exception as exc:
        logger.error("Failed to extract single-file sources: %s", exc)
        return None

    out = result.stdout or ""
    if "###SPLIT###" not in out or "###END###" not in out:
        logger.error("single-file source extraction malformed (rc=%s)", result.returncode)
        return None

    payload = out.split("###SPLIT###", 1)[1].rsplit("###END###", 1)[0]
    try:
        parsed = json.loads(payload)
    except Exception as exc:
        logger.error("Failed to parse single-file sources: %s", exc)
        return None

    if not (isinstance(parsed, dict) and parsed.get("hook") and parsed.get("main") and parsed.get("zip")):
        logger.error("single-file sources payload missing required fields")
        return None

    _SINGLEFILE_SOURCES = parsed
    return _SINGLEFILE_SOURCES


async def _load_singlefile_sources() -> Optional[Dict[str, str]]:
    """Initialize the process-wide SingleFile sources exactly once."""
    async with _SINGLEFILE_SOURCES_LOCK:
        if _SINGLEFILE_SOURCES is not None:
            return _SINGLEFILE_SOURCES
        return await _load_singlefile_sources_uncached()


# ---------------------------------------------------------------------------
# Page helpers
# ---------------------------------------------------------------------------

async def _ensure_page_stable(page: Any) -> None:
    """Best-effort wait for the page to settle.  Each step has a tight
    timeout so a hung network request doesn't block the capture pipeline."""
    try:
        await page.wait_for_load_state("load", timeout=2500)
    except Exception:
        pass
    try:
        await page.wait_for_load_state("networkidle", timeout=2500)
    except Exception:
        pass
    try:
        await page.wait_for_function("document.readyState === 'complete'", timeout=1000)
    except Exception:
        pass
    if PRE_CAPTURE_WAIT > 0:
        await asyncio.sleep(PRE_CAPTURE_WAIT)


# <base href="..."> injector.  SingleFile output usually has a <base> already;
# we ensure the captured page's <head> has one so relative URLs in the
# captured HTML resolve against the original page URL.
_BASE_HREF_RE = re.compile(r"<base\b[^>]*\bhref=[\"'][^\"']*[\"'][^>]*>", re.IGNORECASE)

# ---------------------------------------------------------------------------
# Dead-subframe stripping
# ---------------------------------------------------------------------------
# When an iframe refuses to load (proxy block, X-Frame-Options, refused
# connection — e.g. gpt.mail.yahoo.net), Chrome paints an error page inside
# the frame ("<div id=\"sub-frame-error\"> ... refused to connect").  That
# error markup must never reach the mirror: it renders as a broken page
# island and the real frame content isn't there anyway.  Fast captures drop
# such iframes in the serializer; this net covers SingleFile/delta-replayed
# HTML too.  Same idea: whole chrome-error:// iframes and documents.

_IFRAME_BLOCK_RE = re.compile(
    r"<iframe\b(?P<attrs>[^>]*)(?:>(?P<body>.*?</iframe\s*>)|/?>)",
    re.IGNORECASE | re.DOTALL,
)
_DEAD_ERROR_OPEN_RE = re.compile(
    r"<div\b[^>]*\bid\s*=\s*[\"'](?:sub-frame-error|main-frame-error)[\"'][^>]*>",
    re.IGNORECASE,
)
_DIV_TOKEN_RE = re.compile(r"</?div\b[^>]*>", re.IGNORECASE)
_KNOWN_DEAD_FRAME_HOSTS = {"gpt.mail.yahoo.net"}
_PROTECTED_HTML_RE = re.compile(
    r"<!--.*?-->|<(?P<tag>script|style)\b[^>]*>.*?</(?P=tag)\s*>",
    re.IGNORECASE | re.DOTALL,
)


def _rewrite_html_outside_protected(html: str, transform: Any) -> str:
    """Apply a markup transform without touching script/style text or comments."""
    pieces: List[str] = []
    cursor = 0
    for protected in _PROTECTED_HTML_RE.finditer(html):
        pieces.append(transform(html[cursor:protected.start()]))
        pieces.append(protected.group(0))
        cursor = protected.end()
    pieces.append(transform(html[cursor:]))
    return "".join(pieces)


def _remove_chrome_error_blocks_plain(html: str) -> str:
    """Remove Chrome error divs with balanced nested divs.

    A non-greedy regex is not sufficient here: Chrome's error template has
    changed its nesting over time, and stopping after two closing tags can
    leave a visible ``</div>`` or the error details behind.
    """
    if not html:
        return html
    pieces: List[str] = []
    cursor = 0
    while True:
        opening = _DEAD_ERROR_OPEN_RE.search(html, cursor)
        if not opening:
            pieces.append(html[cursor:])
            break
        pieces.append(html[cursor:opening.start()])
        depth = 1
        scan = opening.end()
        end = len(html)
        while depth and scan < len(html):
            token = _DIV_TOKEN_RE.search(html, scan)
            if not token:
                end = len(html)
                depth = 0
                break
            if token.group(0).startswith("</"):
                depth -= 1
            else:
                depth += 1
            scan = token.end()
            if depth == 0:
                end = scan
        cursor = end
    return "".join(pieces)


def _remove_chrome_error_blocks(html: str) -> str:
    return _rewrite_html_outside_protected(html, _remove_chrome_error_blocks_plain)


def _remove_known_dead_frame_tags(html: str) -> str:
    """Drop known failed embeds even when a caller has no page URL."""
    def _replace(match: re.Match[str]) -> str:
        attrs = match.group("attrs") or ""
        src_match = re.search(r"\bsrc\s*=\s*([\"'])(.*?)\1", attrs,
                              re.IGNORECASE | re.DOTALL)
        if not src_match:
            return match.group(0)
        try:
            host = (urlsplit(src_match.group(2).strip()).hostname or "").lower()
            return "" if host in _KNOWN_DEAD_FRAME_HOSTS else match.group(0)
        except Exception:
            return match.group(0)

    return _rewrite_html_outside_protected(
        html, lambda part: _IFRAME_BLOCK_RE.sub(_replace, part)
    )


def _strip_dead_subframes(html: str, base_url: Optional[str] = None) -> str:
    """Remove browser error documents and iframe shells that cannot mirror.

    The fast serializer already omits cross-origin frames.  SingleFile and
    fallback captures can still contain the opening iframe tag, however, and
    the client may render Chrome's ``sub-frame-error`` page inside it.  Apply
    the same policy to every full-capture tier, while retaining same-origin,
    ``about:``, ``data:`` and ``srcdoc`` frames.
    """
    if not html:
        return html
    out = html
    # Whole chrome-error iframes.  Keep this separate from the generic iframe
    # pass so it also works when no page URL is available.
    out = _rewrite_html_outside_protected(
        out,
        lambda part: re.sub(
            r"<iframe\b[^>]*\bsrc=[\"\']chrome-error://[^\"\']*[\"\'][^>]*>(?:.*?</iframe>)?",
            "", part, flags=re.IGNORECASE | re.DOTALL,
        ),
    )
    # Chrome's error template is nested and has varied across Chrome builds;
    # remove the balanced block rather than guessing how many div closers it
    # contains.
    out = _remove_chrome_error_blocks(out)
    # Keep the Yahoo embed known to fail out of legacy callers that only pass
    # HTML. With a page URL, the generic origin check below removes all such
    # cross-origin frames, not only this host.
    out = _remove_known_dead_frame_tags(out)

    if not base_url:
        return out
    try:
        page = urlsplit(base_url)
        page_origin = (
            page.scheme.lower(), page.hostname.lower() if page.hostname else "",
            page.port or (443 if page.scheme.lower() == "https" else 80),
        )
    except Exception:
        return out

    def _frame_replacement(match: re.Match[str]) -> str:
        attrs = match.group("attrs") or ""
        src_match = re.search(r"\bsrc\s*=\s*([\"'])(.*?)\1", attrs,
                              re.IGNORECASE | re.DOTALL)
        if not src_match:
            return match.group(0)  # srcdoc/about:blank/blob/data frames may be valid
        src = src_match.group(2).strip()
        if not src or src.lower().startswith(("about:", "data:", "blob:", "javascript:")):
            return match.group(0)
        try:
            target = urlsplit(urljoin(base_url, src))
            if target.scheme.lower() not in ("http", "https"):
                return match.group(0)
            target_origin = (
                target.scheme.lower(), target.hostname.lower() if target.hostname else "",
                target.port or (443 if target.scheme.lower() == "https" else 80),
            )
            if target_origin != page_origin:
                return ""
        except Exception:
            return match.group(0)
        return match.group(0)

    return _rewrite_html_outside_protected(
        out, lambda part: _IFRAME_BLOCK_RE.sub(_frame_replacement, part)
    )


def _inject_base_href(html: str, url: str) -> str:
    if not html or not url:
        return html
    safe_url = url.replace('"', "&quot;")
    base_tag = f'<base href="{safe_url}">'
    if _BASE_HREF_RE.search(html):
        return _BASE_HREF_RE.sub(base_tag, html, count=1)
    m = re.search(r"<head\b[^>]*>", html, re.IGNORECASE)
    if m:
        return html[: m.end()] + base_tag + html[m.end() :]
    m = re.search(r"<html\b[^>]*>", html, re.IGNORECASE)
    if m:
        return html[: m.end()] + "<head>" + base_tag + "</head>" + html[m.end() :]
    return base_tag + html


# ---------------------------------------------------------------------------
# Mirror-font handling: preserve original site typography
# ---------------------------------------------------------------------------
# Pages retain the target website's authentic fonts (e.g. Google Sans,
# Amazon Ember, Segoe UI, Roboto, custom webfonts) instead of forcing
# Montserrat. Any legacy or stale shfm-font style blocks are stripped so
# the site's typography renders naturally, accurately, and without external font-fetching delays.

def _strip_mirror_font(html: str) -> str:
    """Ensure no legacy forced font overrides (shfm-font) clobber the target site's natural fonts."""
    if not html or "shfm-font" not in html:
        return html
    return re.sub(r'<style\b[^>]*>/\*\s*shfm-font\s*\*/.*?</style>', '', html, flags=re.DOTALL | re.IGNORECASE)


# ---------------------------------------------------------------------------
# Content-addressed asset cache (server fetch-once ↔ /assets/<hash> endpoint)
# ---------------------------------------------------------------------------

class AssetEntry:
    """One cached asset.  Fields match what api.py's /assets handler reads."""

    __slots__ = ("data", "content_type", "size")

    def __init__(self, data: bytes, content_type: str) -> None:
        self.data = data
        self.content_type = content_type or "application/octet-stream"
        self.size = len(data)


class AssetManager:
    """Content-addressed cache with session leases.

    A shared digest may be reused by many sessions, but eviction and explicit
    clears never remove an asset while a live session still references it.
    """

    def __init__(self, max_items: int = CACHE_MAX_ITEMS, max_bytes: int = CACHE_MAX_BYTES) -> None:
        self._items: "OrderedDict[str, AssetEntry]" = OrderedDict()
        self._by_url: "OrderedDict[str, str]" = OrderedDict()
        self._refs: Dict[str, set] = {}
        # CSS can point at other cached assets.  Keep those dependency leases
        # alive whenever the stylesheet itself is leased by a live session.
        self._deps: Dict[str, set] = {}
        self._lock = threading.RLock()
        self._max_items = max_items
        self._max_bytes = max_bytes
        self._bytes = 0
        self._hits = 0
        self._misses = 0
        self._evictions = 0
        self._registered = 0

    def register(self, data: bytes, content_type: str, url: Optional[str] = None,
                 owner_id: Optional[str] = None) -> str:
        digest = hashlib.sha256(data).hexdigest()
        with self._lock:
            existing = self._items.get(digest)
            if existing is not None:
                self._items.move_to_end(digest)
            else:
                entry = AssetEntry(data, content_type)
                self._items[digest] = entry
                self._bytes += entry.size
                self._registered += 1
            if (content_type or "").split(";", 1)[0].strip().lower() == "text/css":
                deps = {m.group(1).lower() for m in _ASSET_PATH_RE.finditer(data.decode("utf-8", errors="ignore"))}
                self._deps[digest] = deps
            if owner_id:
                self._retain_locked(digest, str(owner_id))
            if url:
                self._by_url[url] = digest
                self._by_url.move_to_end(url)
            self._evict_unreferenced_locked()
            return digest

    def _retain_locked(self, digest: str, owner: str, seen: Optional[set] = None) -> None:
        if digest not in self._items:
            return
        seen = seen or set()
        if digest in seen:
            return
        seen.add(digest)
        self._refs.setdefault(digest, set()).add(owner)
        for dependency in self._deps.get(digest, set()):
            self._retain_locked(dependency, owner, seen)

    def retain(self, digest: str, owner_id: Optional[str]) -> bool:
        if not digest or not owner_id:
            return False
        with self._lock:
            if digest not in self._items:
                return False
            self._retain_locked(digest, str(owner_id))
            return True

    def _evict_unreferenced_locked(self) -> None:
        while self._items and (len(self._items) > self._max_items or self._bytes > self._max_bytes):
            candidate = next(
                ((digest, entry) for digest, entry in self._items.items()
                 if not self._refs.get(digest)),
                None,
            )
            if candidate is None:
                # Every excess entry is still visible to a live session. Keep
                # it rather than invalidating another session's mirror.
                break
            evict_digest, ev = candidate
            self._items.pop(evict_digest, None)
            self._bytes -= ev.size
            self._refs.pop(evict_digest, None)
            self._deps.pop(evict_digest, None)
            self._evictions += 1
            for url, digest in list(self._by_url.items()):
                if digest == evict_digest:
                    self._by_url.pop(url, None)
        while len(self._by_url) > self._max_items:
            self._by_url.popitem(last=False)

    def get(self, digest: str) -> Optional[AssetEntry]:
        with self._lock:
            entry = self._items.get(digest)
            if entry is None:
                self._misses += 1
                return None
            self._hits += 1
            self._items.move_to_end(digest)
            return entry

    def get_by_url(self, url: str, owner_id: Optional[str] = None) -> Optional[AssetEntry]:
        with self._lock:
            digest = self._by_url.get(url)
            if digest and owner_id:
                self._retain_locked(digest, str(owner_id))
        return self.get(digest) if digest else None

    def digest_for(self, data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    async def clear(self, owner_id: Optional[str] = None) -> None:
        """Release one session, or prune only unreferenced global entries."""
        with self._lock:
            if owner_id is not None:
                owner = str(owner_id)
                for refs in self._refs.values():
                    refs.discard(owner)
            else:
                # A global admin clear must not invalidate live sessions.
                pass
            self._evict_unreferenced_locked()

    async def release_session(self, owner_id: str) -> None:
        await self.clear(owner_id=owner_id)

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            referenced = sum(1 for refs in self._refs.values() if refs)
            return {
                "items": len(self._items),
                "url_entries": len(self._by_url),
                "bytes": self._bytes,
                "max_items": self._max_items,
                "max_bytes": self._max_bytes,
                "hits": self._hits,
                "misses": self._misses,
                "evictions": self._evictions,
                "registered": self._registered,
                "referenced_items": referenced,
            }


_GLOBAL_ASSET_MANAGER: Optional[AssetManager] = None
_GLOBAL_ASSET_MANAGER_LOCK = threading.Lock()


def get_global_asset_manager() -> AssetManager:
    """Process-wide singleton used by the capture pipeline and /assets."""
    global _GLOBAL_ASSET_MANAGER
    if _GLOBAL_ASSET_MANAGER is None:
        with _GLOBAL_ASSET_MANAGER_LOCK:
            if _GLOBAL_ASSET_MANAGER is None:
                _GLOBAL_ASSET_MANAGER = AssetManager()
    return _GLOBAL_ASSET_MANAGER


_MIME_TO_EXT = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/svg+xml": "svg",
    "image/x-icon": "ico",
    "image/bmp": "bmp",
    "text/css": "css",
    "application/javascript": "js",
    "text/javascript": "js",
    "application/json": "json",
    "font/woff": "woff",
    "font/woff2": "woff2",
    "font/ttf": "ttf",
    "font/otf": "otf",
    "application/font-woff": "woff",
    "application/font-woff2": "woff2",
    "application/vnd.ms-fontobject": "eot",
}


def _guess_ext_from_content_type(content_type: str) -> str:
    """Reverse of api.py's _EXT_TO_MIME fallback table."""
    return _MIME_TO_EXT.get((content_type or "").split(";", 1)[0].strip().lower(), "")


# ---------------------------------------------------------------------------
# Fast in-page serializer
# ---------------------------------------------------------------------------
#
# One CSP-immune evaluate (Runtime.callFunctionOn) serializes the live DOM
# to HTML.  Compared to SingleFile it is ~100x faster because it does NOT
# fetch/inline resources: external URLs stay URLs and are rewritten into
# the asset cache server-side.  Fidelity rules:
#   * live form state (input.value / checked / option.selected / textarea)
#     is materialized into attributes/text so the mirror renders what the
#     victim sees;
#   * open shadow roots serialize as declarative shadow DOM
#     (<template shadowrootmode>); closed roots become a marker comment;
#   * same-origin canvases are rasterized to data-URL <img>; tainted ones
#     are left as-is;
#   * <base> tags are dropped (the server injects its own);
#   * when deltaIds is on, every element is stamped with a stable
#     data-mid from a page-lifetime WeakMap — the delta observer reuses
#     the same ids so client patches address the right nodes after a
#     full resync.
_FAST_SERIALIZE_JS = r"""
(params) => {
  const deltaIds = !!(params && params.deltaIds);
  const MID = 'data-mid';
  if (deltaIds) {
    if (!window.__domMidMap) window.__domMidMap = new WeakMap();
    if (!window.__domMidNext) window.__domMidNext = 1;
  }
  const escText = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  const escAttr = (s) => String(s).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;');
  // CSS-in-JS / constructable stylesheets (styled-components SSR-mode,
  // Lit, MUI, Tailwind JIT injectors): rules living in
  // ``adoptedStyleSheets`` have NO DOM node, so plain HTML serialization
  // loses them.  We re-materialize them as <style> elements.
  const adoptedCss = (root) => {
    try {
      const sheets = root.adoptedStyleSheets || [];
      let css = '';
      for (const sh of sheets) {
        try { for (const r of sh.cssRules) css += r.cssText + '\n'; } catch (e) { /* cross-origin sheet */ }
      }
      return css;
    } catch (e) { return ''; }
  };
  const adoptedStyleTag = (css) => css
    ? '<style data-shf-adopted="1">' + css.replace(/<\//g, '<\\/') + '</style>'
    : '';
  // SingleFile technique: the CSSOM is the truth, DOM text is stale.
  // <style> elements carry their ORIGINAL text even after page scripts
  // insertRule()/deleteRule(); sheet.cssRules reflects the ACTUAL current
  // cascade.  <link> stylesheets that are readable (same-origin or
  // CORS-allowed) get materialized inline with recursive @import
  // expansion, so their styling ships without a network fetch at all.
  const rulesToCss = (rules, depth) => {
    let css = '';
    try {
      for (const r of (rules || [])) {
        if (r && r.type === 3 /* CSSRule.IMPORT_RULE */) {
          try {
            if (depth < 3 && r.styleSheet && r.styleSheet.cssRules) {
              css += rulesToCss(r.styleSheet.cssRules, depth + 1);
              continue;
            }
          } catch (e) { /* cross-origin import */ }
        }
        css += (r ? r.cssText : '') + '\n';
      }
    } catch (e) { /* sheet vanished */ }
    return css;
  };
  const liveSheetCss = (sheet) => {
    try { return (sheet && sheet.cssRules) ? rulesToCss(sheet.cssRules, 0) : ''; }
    catch (e) { return ''; }  // SecurityError on cross-origin sheets
  };
  const styleSafe = (css) => css.replace(/<\//g, '<\\/');

  const VOID = new Set(['area','base','br','col','embed','hr','img','input','link','meta','param','source','track','wbr']);
  const attrsFor = (el, tag) => {
    let s = '';
    const list = el.attributes;
    for (let i = 0; i < list.length; i++) {
      const a = list[i];
      const n = a.name;
      if (n === MID) continue;
      // NO page modifications (policy): attributes go out EXACTLY as the
      // site authored them — nothing materialized from live JS state.
      s += ' ' + n + '="' + escAttr(a.value) + '"';
    }
    if (deltaIds) {
      let m = window.__domMidMap.get(el);
      if (!m) { m = window.__domMidNext++; window.__domMidMap.set(el, m); }
      s += ' ' + MID + '="' + m + '"';
    }
    return s;
  };
  const serialize = (node, out) => {
    const t = node.nodeType;
    if (t === 3) { out.push(escText(node.nodeValue)); return; }
    if (t === 8) { out.push('<!--', String(node.nodeValue).replace(/--/g, '--'), '-->'); return; }
    if (t !== 1) return;
    const el = node;
    const tag = (el.localName || el.tagName.toLowerCase());
    if (tag === 'base') return;   // server injects its own <base href>
    if (tag === 'iframe') {
      // Frames that wouldn't load must never reach the mirror (user policy):
      //   * chrome-error documents (refused/blocked server-side) -> delete.
      //   * cross-origin frames would fail to embed from the mirrored
      //     page's origin anyway (X-Frame-Options / frame-ancestors) and
      //     render as "refused to connect" error islands -> delete.
      //   * same-origin / about: / srcdoc frames are kept.
      let dead = false;
      try {
        const d = el.contentDocument;
        if (d && d.URL && d.URL.indexOf('chrome-error:') === 0) dead = true;
      } catch (e) { /* cross-origin child document: unreadable */ }
      if (dead) return;
      const fsrc = el.src || el.getAttribute('src') || '';
      if (fsrc && fsrc.indexOf('about:') !== 0) {
        try {
          const loc = document.location;
          if (new URL(fsrc, loc.href).origin !== loc.origin) return;
        } catch (e) { /* unparseable src -> keep */ }
      }
    }
    // Same-origin <link rel=stylesheet> -> materialize from the CSSOM
    // (SingleFile's live-read); cross-origin links stay links and go
    // through the server-side asset cache instead.
    if (tag === 'link') {
      const rel = (el.getAttribute('rel') || '').toLowerCase();
      const asA = (el.getAttribute('as') || '').toLowerCase();
      const onload = el.getAttribute('onload') || '';
      // Async-CSS patterns that NEVER fire without JS (no-JS mirror):
      //   <link rel="preload" as="style" onload="this.rel='stylesheet'">
      //   <link rel="stylesheet" media="print" onload="this.media='all'">
      // Detect them as stylesheets too, or normalize the rel/media below.
      const styleish = rel.indexOf('stylesheet') !== -1
        || (rel.indexOf('preload') !== -1 && asA === 'style');
      if (styleish && !el.disabled) {
        const css = liveSheetCss(el.sheet);
        if (css) {
          const media = el.getAttribute('media');
          out.push('<style data-shf-fromlink="1"',
                   media ? ' media="' + escAttr(media) + '"' : '',
                   (el.title ? ' title="' + escAttr(el.title) + '"' : ''),
                   '>', styleSafe(css), '</style>');
          return;
        }
        // Not readable from the CSSOM (cross-origin) — emit a WORKING
        // stylesheet link so the mirror browser loads it directly:
        // rel is always stylesheet, swapped media becomes 'all', the
        // onload swapper is dropped.
        const href = el.getAttribute('href');
        if (href) {
          const media = /media\s*=/.test(onload) ? 'all' : el.getAttribute('media');
          const xo = el.getAttribute('crossorigin');
          out.push('<link rel="stylesheet" href="', escAttr(href), '"',
                   media ? ' media="' + escAttr(media) + '"' : '',
                   xo ? ' crossorigin="' + escAttr(xo) + '"' : '', '>');
          return;
        }
      }
    }
    // Images: pick the URL the PAGE would actually display.  Lazy-load
    // libraries (react-lazyload & co.) leave src empty / a 1x1 gif and
    // swap via JS that never runs in the no-JS mirror.
    if (tag === 'img') {
      let pick = '';
      try { pick = el.currentSrc || ''; } catch (e) { /* not ready */ }
      const authored = el.getAttribute('src') || '';
      if (!pick) pick = authored;
      if (!pick || pick.indexOf('data:image/gif') === 0 || pick === 'about:blank') {
        pick = el.getAttribute('data-src') || el.getAttribute('data-original')
            || el.getAttribute('data-lazy-src') || el.getAttribute('data-image') || pick;
      }
      if (!pick) {
        const ss = el.getAttribute('srcset') || el.getAttribute('data-srcset') || '';
        if (ss) pick = ss.split(',')[0].trim().split(/\s+/)[0];
      }
      out.push('<img');
      const list2 = el.attributes;
      for (let i = 0; i < list2.length; i++) {
        const a = list2[i];
        if (a.name === 'src' || a.name === 'srcset' || a.name === MID) continue;
        if (a.name.indexOf('data-src') === 0 || a.name.indexOf('data-original') === 0
            || a.name.indexOf('data-lazy') === 0 || a.name === 'data-image') continue;
        out.push(' ', a.name, '="', escAttr(a.value), '"');
      }
      if (deltaIds) {
        let m2 = window.__domMidMap.get(el);
        if (!m2) { m2 = window.__domMidNext++; window.__domMidMap.set(el, m2); }
        out.push(' ', MID, '="', String(m2), '"');
      }
      if (pick) out.push(' src="', escAttr(pick), '"');
      out.push('>');
      return;
    }
    out.push('<', tag);
    out.push(attrsFor(el, tag));
    out.push('>');
    if (VOID.has(tag)) return;
    if (tag === 'script' || tag === 'style') {
      let raw;
      if (tag === 'style') {
        // CSSOM wins over stale DOM text (script-mutated sheets).
        raw = liveSheetCss(el.sheet) || el.textContent || '';
        raw = styleSafe(raw);
      } else {
        raw = (el.textContent || '').replace(/<\/script/gi, '<\\/script');
      }
      out.push(raw, '</', tag, '>');
      return;
    }
    const sr = el.shadowRoot;
    if (sr) {
      if (sr.mode === 'open') {
        out.push('<template shadowrootmode="open">');
        const srCss = adoptedCss(sr);
        if (srCss) out.push(adoptedStyleTag(srCss));
        for (const c of sr.childNodes) serialize(c, out);
        out.push('</template>');
      } else {
        out.push('<!--shifix:closed-shadow-->');
      }
    }
    for (const c of el.childNodes) serialize(c, out);
    out.push('</', tag, '>');
  };
  const de = document.documentElement;
  if (!de) return null;
  const out = [];
  out.push('<!DOCTYPE html>\n');
  serialize(de, out);
  let html = out.join('');
  const docCss = adoptedCss(document);
  if (docCss) {
    const tag = adoptedStyleTag(docCss);
    html = /<head[^>]*>/i.test(html)
      ? html.replace(/<head[^>]*>/i, (m) => m + tag)
      : html.replace(/<html[^>]*>/i, (m) => m + tag);
  }
  return html;
}
"""


# The serializer invoked as an IIFE expression (NOT as a function+args
# callFunctionOn payload).  This makes Playwright and raw-CDP (SB) evaluate
# it through the IDENTICAL mechanism (Runtime.evaluate-compatible) — the
# callFunctionOn function+arguments shape is where the two backends
# diverged on the live box (fast capture returned None on SB).
_FAST_SERIALIZE_CALL_JS = (
    "(() => { const fn = "
    + _FAST_SERIALIZE_JS
    + "; return fn(" + _json.dumps({"deltaIds": bool(LIVE_DELTA)}) + "); })();"
)

# Last fast-capture failure, surfaced by capture_page when all tiers fail
# (kept at module level: _capture_fast is a plain function).
_LAST_FAST_CAPTURE_ERROR: str = ""


async def _capture_fast(page: Any) -> Optional[str]:
    """Serialize the live DOM in-page via one evaluate round-trip.

    Returns the HTML string (NOT yet base-href/asset rewritten), or None
    when the page/evaluate is unavailable — the caller decides whether to
    fall back to SingleFile / outerHTML.
    """
    global _LAST_FAST_CAPTURE_ERROR
    if page is None:
        _LAST_FAST_CAPTURE_ERROR = "page is None"
        return None
    try:
        url = page.url
    except Exception as exc:
        _LAST_FAST_CAPTURE_ERROR = f"page.url unreadable: {exc}"
        return None
    if not url:
        _LAST_FAST_CAPTURE_ERROR = "empty page url"
        return None
    try:
        html = await page.evaluate(_FAST_SERIALIZE_CALL_JS)
    except Exception as exc:
        _LAST_FAST_CAPTURE_ERROR = f"evaluate: {exc}"
        logger.debug("dc fast serializer evaluate failed: %s", exc)
        return None
    if not isinstance(html, str) or not html:
        _LAST_FAST_CAPTURE_ERROR = f"serializer returned {type(html).__name__} (empty)"
        return None
    return html


# Cheap DOM checksum for the unchanged-skip.  Deliberately NOT computed
# from the serialized HTML (that would defeat the point — serialization
# is the expensive part we are trying to skip).  Node count + text length
# + scroll height catches every user-visible class of change on mirrored
# pages (element churn, text updates, reflow) at O(1)-ish cost.
_DOM_CHECKSUM_JS = r"""
() => {
  const de = document.documentElement;
  if (!de) return '';
  const els = de.getElementsByTagName('*').length;
  const tl = de.textContent ? de.textContent.length : 0;
  const sh = de.scrollHeight | 0;
  return els + ':' + tl + ':' + sh;
}
"""


_COLD_REASONS = frozenset((
    "click", "mouseup", "text", "keydown", "touchend", "tap", "delta_overflow",
))


def _settle_for_reason(reason: Optional[str]) -> str:
    """Map a capture reason to a settle level.

    Interaction-driven recaptures ("interaction:*", input forwards,
    coalesced follow-ups, delta overflow) snapshot a page that is already
    live — the legacy load/networkidle waits were pure latency.  Initial
    loads and navigations get a light readyState wait so the client never
    sees a parsing document.  DOM_CAPTURE_FULL_SETTLE=1 reverts globally.
    """
    if FULL_SETTLE:
        return "full"
    if not reason:
        return "light"
    r = reason.lower()
    if r.startswith(("interaction:", "coalesce:")) or r in _COLD_REASONS:
        return "none"
    return "light"


# ---------------------------------------------------------------------------
# Asset rewrite: external URLs -> /assets/<sha256>[.<ext>]
# ---------------------------------------------------------------------------
#
# Only applied to FAST captures.  SingleFile output is self-contained
# already.  Every fetch is bounded (per-asset timeout + max bytes +
# max-per-page fan-in + global time budget); any fetch failure simply
# leaves the original URL in place — the injected <base href> then
# resolves it against the origin, exactly like the pre-cache behavior.

_IMG_SRC_RE = re.compile(r'(<img\b[^>]*?\ssrc\s*=\s*)(["\'])([^"\']*)\2', re.IGNORECASE)
_SCRIPT_SRC_RE = re.compile(r'(<script\b[^>]*?\ssrc\s*=\s*)(["\'])([^"\']*)\2', re.IGNORECASE)
_LINK_TAG_RE = re.compile(r'<link\b[^>]*?>', re.IGNORECASE)
_LINK_ATTR_RE = re.compile(r'(\w[\w-]*)\s*=\s*(["\'])([^"\']*)\2')
_LINK_CACHEABLE_REL_RE = re.compile(r'(stylesheet|icon|apple-touch-icon|mask-icon|manifest|shortcut)', re.IGNORECASE)
_CSS_URL_RE = re.compile(r'url\(\s*(["\']?)([^"\')\s][^"\')]*?)\1\s*\)', re.IGNORECASE)
_CSS_IMPORT_RE = re.compile(r'@import\s+(?:url\(\s*)?(["\'])([^"\']+)\1', re.IGNORECASE)
_REWRITE_GLOBAL_BUDGET_S: float = float(os.environ.get("DOM_CAPTURE_ASSET_TOTAL_TIMEOUT_S", "10"))

# Chrome UA fallback for cache fetches (real UA pulled from the page).
_FETCH_UA_FALLBACK = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def _data_uri(data: bytes, content_type: str) -> str:
    """data: URI for small embedded assets (images/logos/SVGs/fonts)."""
    ct = (content_type or "").split(";")[0].strip() or "application/octet-stream"
    return f"data:{ct};base64,{base64.b64encode(data).decode('ascii')}"


def _resolvable_url(ref: str, base_url: str) -> Optional[str]:
    """Absolute http(s) URL for a page ref, or None when not cacheable."""
    if not ref:
        return None
    ref = ref.strip()
    if not ref or ref.startswith(('#', 'data:', 'blob:', 'javascript:', 'mailto:', 'tel:', 'about:', '/assets/')):
        return None
    abs_url = urljoin(base_url, ref)
    if not abs_url.lower().startswith(('http://', 'https://')):
        return None
    return abs_url


async def _fetch_one_asset(client: Any, sem: asyncio.Semaphore, abs_url: str) -> Optional[Tuple[bytes, str]]:
    """Fetch one asset body with hard bounds.  None on any failure."""
    async with sem:
        try:
            resp = await client.get(abs_url, timeout=ASSET_FETCH_TIMEOUT_S)
            if resp.status_code >= 400:
                return None
            data = resp.content
            if not data or len(data) > ASSET_MAX_BYTES:
                return None
            ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
            return (data, ctype)
        except Exception:
            return None


async def _cache_css_text(css_text: str, css_abs_url: str, client: Any, sem: asyncio.Semaphore,
                          manager: AssetManager, depth: int, owner_id: Optional[str] = None) -> bytes:
    """Fetch+rewrite the resources referenced by a CSS body, recursively
    (bounded depth), then return the rewritten CSS bytes.  Small image/font
    refs are data-URI embedded (logo-in-CSS case); larger refs and nested
    stylesheets go through the /assets cache."""
    if depth > 2:
        return css_text.encode("utf-8", errors="ignore")

    refs: List[str] = []
    for m in _CSS_URL_RE.finditer(css_text):
        refs.append(m.group(2))
    for m in _CSS_IMPORT_RE.finditer(css_text):
        refs.append(m.group(2))
    uniq: List[str] = []
    seen = set()
    for ref in refs:
        abs_u = _resolvable_url(ref, css_abs_url)
        if abs_u and abs_u not in seen:
            seen.add(abs_u)
            uniq.append(abs_u)
    uniq = uniq[:96]  # was 24: inner CSS refs (sprites/fonts) were dropped on heavy pages

    results = await asyncio.gather(*(_fetch_one_asset(client, sem, u) for u in uniq))
    repl: Dict[str, str] = {}
    for abs_u, res in zip(uniq, results):
        if not res:
            continue
        data, ctype = res
        # Nested stylesheet?  Recurse so its url()s also resolve locally.
        is_css_ref = ctype in ("text/css", "") or abs_u.lower().split("?", 1)[0].endswith(".css")
        if is_css_ref and depth < 2:
            try:
                sub_text = data.decode("utf-8", errors="ignore")
                data = await _cache_css_text(
                    sub_text, abs_u, client, sem, manager, depth + 1, owner_id=owner_id
                )
                ctype = "text/css"
            except Exception:
                pass
            # Small nested css embeds as a data: URI (works in @import and
            # url()) so an inlined parent <style> stays fully self-contained.
            if CSS_EMBED_MAX_BYTES > 0 and len(data) <= CSS_EMBED_MAX_BYTES:
                manager.register(data, ctype, url=abs_u, owner_id=owner_id)
                repl[abs_u] = _data_uri(data, ctype)
                continue
            digest = manager.register(data, ctype, url=abs_u, owner_id=owner_id)
            ext = _guess_ext_from_content_type(ctype)
            repl[abs_u] = f"/assets/{digest}{'.' + ext if ext else ''}"
            continue
        # Small image/font refs: embed as data URI.
        if (EMBED_MAX_BYTES > 0 and len(data) <= EMBED_MAX_BYTES
                and (ctype.startswith(("image/", "font/", "application/font")) or not ctype)):
            manager.register(data, ctype, url=abs_u, owner_id=owner_id)
            repl[abs_u] = _data_uri(data, ctype)
            continue
        digest = manager.register(data, ctype, url=abs_u, owner_id=owner_id)
        ext = _guess_ext_from_content_type(ctype)
        repl[abs_u] = f"/assets/{digest}{'.' + ext if ext else ''}"

    def _sub_url(m: "re.Match[str]") -> str:
        orig = m.group(2)
        abs_u = _resolvable_url(orig, css_abs_url)
        new = repl.get(abs_u) if abs_u else None
        if not new:
            return m.group(0)
        return m.group(0).replace(orig, new, 1)

    def _sub_import(m: "re.Match[str]") -> str:
        orig = m.group(2)
        abs_u = _resolvable_url(orig, css_abs_url)
        new = repl.get(abs_u) if abs_u else None
        if not new:
            return m.group(0)
        return m.group(0).replace(orig, new, 1)

    css_text = _CSS_URL_RE.sub(_sub_url, css_text)
    css_text = _CSS_IMPORT_RE.sub(_sub_import, css_text)
    return css_text.encode("utf-8", errors="ignore")


# Strip JavaScript from captured pages (default ON, user-requested:
# "html and css only").  The mirror is a RENDERER: the Playwright page
# already ran all page JS, and every interaction is relayed to it, so
# scripts baked into the capture only re-run in the viewer's context and
# build URLs against the VIEWER'S origin (blob: docs inherit it) — the
# root of the glued-host sub-frame failures.  Everything visual is
# already in the DOM we captured; JS in the mirror is pure harm.
DOM_CAPTURE_STRIP_SCRIPTS: bool = os.environ.get("DOM_CAPTURE_STRIP_SCRIPTS", "1") not in ("0", "false", "False")

_SCRIPT_TAG_RE = re.compile(
    r"<script\b[^>]*(?:/>|>[\s\S]*?</script\s*>)",
    re.IGNORECASE,
)


def _strip_script_tags(html: str) -> str:
    """Remove all <script> elements from captured HTML. HTML spec: script
    bodies are raw-text until the FIRST literal '</script', so a
    non-greedy match is spec-correct ('<\\/script>' escapes in JS strings
    are exactly that — escaped — and cannot appear raw)."""
    if not html:
        return html
    out, n = _SCRIPT_TAG_RE.subn("", html)
    if n:
        logger.debug("strip-scripts: removed %d <script> element(s) from capture", n)
    return out


async def _rewrite_assets_to_cache(html: str, base_url: str, page: Any = None,
                                  owner_id: Optional[str] = None) -> Tuple[str, List[Dict[str, Any]]]:
    """Resolve external asset refs in captured HTML: small images/icons are
    data-URI EMBEDDED (see EMBED_MAX_BYTES), small stylesheets become inline
    <style> blocks with their inner refs rewritten (CSS_EMBED_MAX_BYTES),
    scripts/large media/large stylesheets go to the fetch-once
    ``/assets/<sha256>[.<ext>]`` cache.

    Returns ``(new_html, assets_meta)`` where assets_meta lists only the
    /assets-path refs (client pre-warms those; embedded refs need nothing).
    Any fetch failure leaves the original URL — the injected <base href>
    then resolves it against the origin exactly like pre-cache behavior.
    ``ImportError`` (no httpx) propagates so the caller can kill-switch.
    """
    manager = get_global_asset_manager()

    # -- 1. Collect refs with kinds --------------------------------------
    candidates: List[str] = []
    kind_by_url: Dict[str, str] = {}
    seen: set = set()

    def _add(ref: str, kind: str) -> None:
        abs_u = _resolvable_url(ref, base_url)
        if abs_u and abs_u not in seen:
            seen.add(abs_u)
            candidates.append(abs_u)
            kind_by_url[abs_u] = kind

    for m in _IMG_SRC_RE.finditer(html):
        _add(m.group(3), "img")
    if not DOM_CAPTURE_STRIP_SCRIPTS:
        for m in _SCRIPT_SRC_RE.finditer(html):
            _add(m.group(3), "script")
    for tm in _LINK_TAG_RE.finditer(html):
        tag = tm.group(0)
        rel = None
        href = None
        for am in _LINK_ATTR_RE.finditer(tag):
            name = am.group(1).lower()
            if name == "rel":
                rel = am.group(3)
            elif name == "href":
                href = am.group(3)
        if rel and href and _LINK_CACHEABLE_REL_RE.search(rel):
            rel_l = rel.lower()
            kind = "css" if "stylesheet" in rel_l else ("icon" if "icon" in rel_l else "other")
            _add(href, kind)

    candidates = candidates[:ASSET_MAX_PER_PAGE]
    if not candidates:
        return html, []

    # -- 2. URL-memo: reuse bytes fetched in earlier captures --------------
    hits: Dict[str, Tuple[bytes, str]] = {}
    fetch_list: List[str] = []
    for u in candidates:
        ent = manager.get_by_url(u, owner_id=owner_id)
        if ent is not None:
            hits[u] = (ent.data, ent.content_type)
        else:
            fetch_list.append(u)

    # -- 3. Fetch what the memo missed (bounded parallelism + budget) ------
    import httpx  # NOTE: ImportError must propagate (caller kill-switch)

    # User-agent evaluation is only needed when there is an actual network
    # miss.  Cache hits are the normal recapture path and should not pay a
    # browser round trip just to construct an unused HTTP client header.
    ua = _FETCH_UA_FALLBACK
    if fetch_list and page is not None:
        try:
            cached_ua = getattr(page, "_cached_ua", None)
            if cached_ua:
                ua = cached_ua
            else:
                got = await page.evaluate("() => navigator.userAgent")
                if isinstance(got, str) and got:
                    ua = got
                    try:
                        setattr(page, "_cached_ua", got)
                    except Exception:
                        pass
        except Exception:
            pass

    async def _run_fetches() -> List[Any]:
        async with httpx.AsyncClient(follow_redirects=True,
                                     headers={"User-Agent": ua, "Referer": base_url,
                                              "Accept": "*/*",
                                              "Accept-Language": "en-US,en;q=0.9"}) as client:
            sem = asyncio.Semaphore(6)
            return await asyncio.gather(*(_fetch_one_asset(client, sem, u) for u in fetch_list))

    fetched: List[Any] = []
    if fetch_list:
        try:
            fetched = await asyncio.wait_for(_run_fetches(), timeout=_REWRITE_GLOBAL_BUDGET_S)
        except Exception as exc:
            logger.debug("asset rewrite: global fetch budget hit (%s) — partial reuse only", exc)
            fetched = []
    for abs_u, res in zip(fetch_list, fetched):
        if not res:
            continue
        data, ctype = res
        hits[abs_u] = (data, ctype)
        # Stylesheets are registered only AFTER their inner urls are
        # rewritten (step 4) — memoizing raw CSS would both skip the inner
        # rewrite forever and cache the wrong bytes.
        if kind_by_url.get(abs_u) != "css":
            manager.register(data, ctype, url=abs_u, owner_id=owner_id)

    # -- 4. Decision per ref: embed / cache / leave -------------------------
    repl: Dict[str, str] = {}
    inline_css: Dict[str, str] = {}
    meta_by_digest: Dict[str, Dict[str, Any]] = {}
    n_embed = 0
    n_cache = 0
    for abs_u in candidates:
        res = hits.get(abs_u)
        if not res:
            continue
        data, ctype = res
        kind = kind_by_url.get(abs_u, "other")
        try:
            if kind == "css":
                if manager.get_by_url(abs_u, owner_id=owner_id) is None:
                    # Fresh CSS: rewrite its inner url()/@import refs (small
                    # images embed below threshold, fonts/nested css via
                    # cache/data-uri), then memoize the rewritten bytes.
                    try:
                        async with httpx.AsyncClient(follow_redirects=True,
                                                     headers={"User-Agent": ua, "Referer": abs_u,
                                                              "Accept": "*/*",
                                                              "Accept-Language": "en-US,en;q=0.9"}) as client:
                            sem = asyncio.Semaphore(6)
                            sub_text = data.decode("utf-8", errors="ignore")
                            data = await _cache_css_text(
                                sub_text, abs_u, client, sem, manager, depth=0, owner_id=owner_id
                            )
                        ctype = "text/css"
                        manager.register(data, ctype, url=abs_u, owner_id=owner_id)
                    except Exception as exc:
                        logger.debug("css inner rewrite failed for %s: %s", abs_u[:120], exc)
                # Small stylesheets INLINE as <style> (client needs no
                # /assets route to render styled pages — the yahoo case);
                # larger ones keep the cache path below.
                if CSS_EMBED_MAX_BYTES > 0 and len(data) <= CSS_EMBED_MAX_BYTES:
                    inline_css[abs_u] = data.decode("utf-8", errors="ignore")
                    n_embed += 1
                    continue
            elif kind in ("img", "icon") and EMBED_MAX_BYTES > 0 and len(data) <= EMBED_MAX_BYTES:
                repl[abs_u] = _data_uri(data, ctype)
                n_embed += 1
                continue
            digest = manager.digest_for(data)
            ext = _guess_ext_from_content_type(ctype)
            path = f"/assets/{digest}{'.' + ext if ext else ''}"
            repl[abs_u] = path
            meta_by_digest[digest] = {"hash": digest, "ext": ext, "content_type": ctype}
            n_cache += 1
        except Exception as exc:
            logger.debug("asset decision failed for %s: %s", abs_u[:120], exc)
            continue

    if n_embed or n_cache:
        logger.debug("asset rewrite: %d embedded, %d cached, %d untouched (memo_hits=%d)",
                     n_embed, n_cache, len(candidates) - n_embed - n_cache,
                     len(candidates) - len(fetch_list))
    if not repl and not inline_css:
        return html, []

    # -- 4. Rewrite HTML ---------------------------------------------------
    def _lookup(ref: str) -> Optional[str]:
        abs_u = _resolvable_url(ref, base_url)
        return repl.get(abs_u) if abs_u else None

    def _sub_src(m: "re.Match[str]") -> str:
        new = _lookup(m.group(3))
        if not new:
            return m.group(0)
        return m.group(1) + m.group(2) + new + m.group(2)

    html = _IMG_SRC_RE.sub(_sub_src, html)
    html = _SCRIPT_SRC_RE.sub(_sub_src, html)

    def _sub_link(m: "re.Match[str]") -> str:
        tag = m.group(0)
        rel = None
        href = None
        media = None
        for am in _LINK_ATTR_RE.finditer(tag):
            name = am.group(1).lower()
            if name == "rel":
                rel = am.group(3)
            elif name == "href":
                href = am.group(3)
            elif name == "media":
                media = am.group(3)
        if not rel or not _LINK_CACHEABLE_REL_RE.search(rel):
            return tag
        # Inline stylesheet?  Replace the whole <link> with a <style>
        # carrying the rewritten CSS (media attribute preserved).
        if href and inline_css:
            abs_u = _resolvable_url(href, base_url)
            if abs_u and abs_u in inline_css:
                media_attr = ' media="%s"' % media.replace('"', "&quot;") if media else ""
                src_attr = ' data-shfcss="%s"' % abs_u.replace('"', "&quot;")
                return "<style" + src_attr + media_attr + ">" + _inline_css_safe(inline_css[abs_u]) + "</style>"
        def _fix(am2: "re.Match[str]") -> str:
            if am2.group(1).lower() != "href":
                return am2.group(0)
            new = _lookup(am2.group(3))
            if not new:
                return am2.group(0)
            return am2.group(1) + "=" + am2.group(2) + new + am2.group(2)
        return _LINK_ATTR_RE.sub(_fix, tag)

    html = _LINK_TAG_RE.sub(_sub_link, html)

    return html, list(meta_by_digest.values())


# ---------------------------------------------------------------------------
# MHTML capture (archive-quality single artifact via CDP)
# ---------------------------------------------------------------------------

async def capture_page_mhtml(page: Any, *, timeout: float = 15.0) -> Optional[str]:
    """MHTML snapshot of the current page via CDP ``Page.captureSnapshot``.

    Archive-quality single artifact (replaces the SingleFile extension
    round-trip for archive flows when ``ARCHIVE_FORMAT=mhtml``).  NOT used
    for the live mirror: browsers cannot reliably render MHTML inside the
    viewer, and it carries the same base64 bloat as SingleFile.
    """
    cdp = None
    try:
        cdp = await page.context.new_cdp_session(page)
        res = await asyncio.wait_for(
            cdp.send("Page.captureSnapshot", {"format": "mhtml"}),
            timeout=timeout,
        )
        data = (res or {}).get("data")
        if isinstance(data, str) and data:
            return data
        return None
    except Exception as exc:
        logger.debug("MHTML capture failed: %s", exc)
        return None
    finally:
        if cdp is not None:
            try:
                await cdp.detach()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# SingleFile full capture
# ---------------------------------------------------------------------------

async def _capture_with_single_file(
    page: Any,
    *,
    timeout: int = SINGLEFILE_TIMEOUT_S,
    max_attempts: int = 3,
    extension_only: bool = False,
) -> Optional[str]:
    """Capture the current page using SingleFile.

    Default mode is **extension** (the MV3 SingleFile we ship).  We open
    a CDP session to the extension's background service worker, call
    ``business.captureTab`` via the official ``capture-page`` external
    message, and wait for the content script to return the inlined
    HTML.

    Set ``SINGLEFILE_CAPTURE_MODE=library`` to use the legacy JS-library
    injection path instead, unless ``extension_only`` is true.
    ``max_attempts`` lets callers bound one retry batch; callers that need to
    wait for a browser extension may repeat batches while the page is alive.
    """
    if page is None:
        return None
    try:
        url = page.url
    except Exception:
        return None
    if not url:
        return None

    # Fast path: if both capture backends are disabled, don't even try.
    if extension_only and not _is_extension_capture_enabled():
        logger.warning("SingleFile extension capture is unavailable; returning None")
        return None
    if not _is_extension_capture_enabled() and not _is_live_library_enabled():
        logger.warning(
            "SingleFile capture is fully disabled "
            "(ENABLE_EXTENSION_CAPTURE=0 and ENABLE_LIVE_LIBRARY=0); "
            "returning None"
        )
        return None

    mode = "extension" if extension_only else os.environ.get(
        "SINGLEFILE_CAPTURE_MODE", "extension"
    ).strip().lower()
    if mode == "library" and not extension_only:
        if not _is_live_library_enabled():
            logger.warning(
                "SingleFile library capture requested but "
                "ENABLE_LIVE_LIBRARY is off; returning None"
            )
            return None
        return await _capture_via_library_injection(page, timeout=timeout)

    # Default & "extension": try the extension path with retries.
    # Library fallback has been removed per user request — extension is now
    # the single source of truth (system Chrome + bundled Chromium both load
    # the MV3 via --load-extension, visible in chrome://extensions).
    # This avoids the garbled CSP-bypass library path on Yahoo and the
    # extra 8 MB capture fallback that was triggered on transient
    # "message channel closed" errors.
    if _is_extension_capture_enabled():
        last_exc = None
        attempts = max(1, int(max_attempts))
        for attempt in range(attempts):
            try:
                html = await _capture_via_extension(page, timeout=timeout)
                if html:
                    if attempt > 0:
                        logger.debug("SingleFile ext: capture succeeded on retry %s/%s (%d bytes)", attempt + 1, attempts, len(html))
                    return html
                last_exc = None
                if attempt < attempts - 1:
                    logger.debug("SingleFile ext: returned no content, retry %s/%s", attempt + 1, attempts)
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                logger.warning("SingleFile ext: returned no content after %s attempt(s) — not falling back to library (disabled)", attempts)
            except Exception as exc:
                last_exc = exc
                msg = str(exc).lower()
                # Transient "message channel closed" happens when the content
                # script hasn't re-attached yet after navigation or when the
                # service worker was idle. Retry instead of falling back to
                # the garbled library injection.
                if ("message channel closed" in msg or "receiving end does not exist" in msg or "could not establish connection" in msg) and attempt < attempts - 1:
                    logger.debug("SingleFile ext: transient failure retry %s/%s: %s", attempt + 1, attempts, exc)
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                logger.warning("SingleFile ext: capture failed (%s)", exc)
                break
        # No library fallback — return None so caller can decide (avoids 8 MB duplicate + garbled Yahoo)
        if last_exc:
            logger.debug("SingleFile ext: giving up after %s attempt(s), last error: %s", attempts, last_exc)
        return None
    else:
        logger.debug(
            "SingleFile extension capture disabled "
            "(CONFIG.enable_extension_capture=False)"
        )

    # Extension disabled and library not requested via SINGLEFILE_CAPTURE_MODE=library.
    # Library fallback is disabled per user request; only use it when explicitly
    # requested via mode=="library" above. Return None.
    if _is_live_library_enabled():
        logger.debug("SingleFile ext: extension disabled but live library is enabled — not using fallback (explicit mode=library required)")
    return None


# ---------------------------------------------------------------------------
# Extension capture path
# ---------------------------------------------------------------------------

def _is_live_library_enabled() -> bool:
    """True if the user has not disabled the in-page SingleFile JS library
    fallback.  Source of truth is :mod:`server_settings` (admin-toggled
    at runtime); we fall back to ``CONFIG.enable_live_library`` and the
    legacy ``ENABLE_LIVE_LIBRARY`` env var for backward compatibility."""
    try:
        import server_settings as _ss
        v = _ss.get_settings().enable_live_library
        if v is False:
            return False
        if v is True:
            return True
    except Exception:
        pass
    # Fall back to UltraConfig + env
    try:
        from config import CONFIG as _CFG
        return bool(getattr(_CFG, "enable_live_library", True))
    except Exception:
        return True


def _extension_id_from_manifest() -> Optional[str]:
    """Deterministic unpacked extension ID derived from manifest's ``key``.

    Chrome derives the ID as: first 16 bytes of SHA-256(SPKI DER) →
    each nibble mapped 0-15 → 'a'-'p' (32 chars).  The manifest we ship
    has a stable ``key`` so the ID is ``ebclppejdflkgblgpoodlceeeabmlajo``.
    Computing it lets us address the extension even when the service
    worker is idle and ``Target.getTargets`` does not list it (or when a
    stealth patch has overwritten ``window.chrome.runtime.id`` in MAIN
    world).  Falls back to the known hard-coded ID if decode fails.
    """
    try:
        import base64 as _b64
        import hashlib as _hl
        candidates = []
        env_dir = os.environ.get("SINGLEFILE_EXT_DIR", "").strip()
        if env_dir:
            candidates.append(env_dir)
        else:
            # same order as browser_manager
            candidates.append(str(Path(__file__).resolve().parent / "single"))
            candidates.append(str(Path.cwd() / "single"))
            candidates.append(str(Path.home() / "shifixsxs" / "single"))
            candidates.append("/home/user/shifixsxs/single")
        for cand in candidates:
            cand_abs = os.path.abspath(cand)
            mf = Path(cand_abs) / "manifest.json"
            if mf.is_file():
                try:
                    data = json.loads(mf.read_text(encoding="utf-8") or "{}")
                except Exception:
                    continue
                key = data.get("key") or ""
                if key:
                    try:
                        der = _b64.b64decode(key)
                        h = _hl.sha256(der).digest()
                        hx = h[:16].hex()
                        trans = str.maketrans("0123456789abcdef", "abcdefghijklmnop")
                        return hx.translate(trans)
                    except Exception:
                        pass
                # No key → cannot derive ID → try next candidate
        # Hard-coded fallback for the manifest we ship (key above)
        return "ebclppejdflkgblgpoodlceeeabmlajo"
    except Exception:
        return "ebclppejdflkgblgpoodlceeeabmlajo"


def _is_extension_capture_enabled() -> bool:
    """True if the user has not disabled extension capture AND a loadable
    extension is on disk.

    The master switch is :mod:`server_settings` (admin-toggled at runtime).
    We fall back to ``CONFIG.enable_extension_capture`` and the legacy
    ``SINGLEFILE_EXT_MODE`` env var for backward compatibility.
    """
    try:
        import server_settings as _ss
        if _ss.get_settings().enable_extension_capture is False:
            return False
    except Exception:
        pass

    if os.environ.get("SINGLEFILE_EXT_MODE", "1").strip() in ("0", "false", "no", "off"):
        return False

    d = os.environ.get("SINGLEFILE_EXT_DIR", "").strip()
    if not d:
        d = str(Path(__file__).resolve().parent / "single")
    from pathlib import Path as _P
    manifest = _P(d) / "manifest.json"
    if not manifest.is_file():
        try:
            from singlefile_ext import _ensure_extracted
            _ensure_extracted()
        except Exception:
            pass
    return manifest.is_file()


async def _capture_via_extension(page: Any, *, timeout: int) -> Optional[str]:
    """
    Capture the current page through the SingleFile extension.

    IMPORTANT:
    Do NOT try to convert a CDP targetId into a Chrome tab ID.

    CDP target IDs look like:
        0A5CCAD1203D610B8CD18E53A149EBE3

    Chrome tab IDs are numeric:
        123

    Instead, this function uses the SingleFile page bridge. The bridge
    runs inside the actual browser tab and sends the request to the
    extension. Chrome then supplies the correct sender.tab.id to the
    service worker automatically.
    """

    if page is None:
        return None

    try:
        url = page.url
    except Exception:
        return None

    if not url:
        return None

    # Quick pre-check: if the content-script bridge never injected, the
    # extension is not loaded for this tab → skip CDP discovery entirely.
    # The check runs in MAIN world; __singlefile_bridge_installed is set by
    # lib/single-file-page-bridge.js (MAIN) which is injected at document_start.
    try:
        bridge_installed = await page.evaluate("() => !!window.__singlefile_bridge_installed")
    except Exception:
        bridge_installed = False
    if not bridge_installed:
        logger.debug("SingleFile ext: page bridge not yet installed (will try Target discovery anyway)")

    # ---------------------------------------------------------------
    # 1. Get the extension ID.
    #    Preferred: Target.getTargets service_worker URL.
    #    However system Chrome may have multiple extensions (e.g. fign...
    #    plus our SingleFile ebcl...). The first service_worker in the
    #    list may be the wrong extension, which then has no bridge and
    #    forces a garbled library fallback. So we collect ALL IDs and
    #    prefer the manifest-derived one (ebcl...) when it appears.
    #    Fallback (1): deterministic ID from manifest key — works even
    #                  when the service worker is idle (not listed).
    #    Fallback (2): scripts with chrome-extension:// src.
    # ---------------------------------------------------------------
    extension_id: Optional[str] = None
    discovered_via: str = ""
    manifest_id: Optional[str] = _extension_id_from_manifest()
    # Remember if we saw a mismatch so we can retry with manifest later
    discovered_manifest_mismatch: bool = False
    cdp = None
    try:
        try:
            cdp = await page.context.new_cdp_session(page)
        except Exception as exc:
            logger.debug("SingleFile ext: could not open CDP session: %s", exc)
            cdp = None

        if cdp is not None:
            try:
                await cdp.send("Target.setDiscoverTargets", {"discover": True})
                targets = await cdp.send("Target.getTargets")
                target_list = targets.get("targetInfos") or []

                # Collect all chrome-extension IDs; prefer manifest_id
                all_ids = []
                for target in target_list:
                    ttype = target.get("type")
                    turl = target.get("url") or ""
                    if turl.startswith("chrome-extension://"):
                        remainder = turl[len("chrome-extension://"): ]
                        ext_id = remainder.split("/", 1)[0].split("?", 1)[0].strip()
                        if ext_id and len(ext_id) >= 10:
                            all_ids.append((ttype, ext_id, turl))
                # Deduplicate preserving order
                seen = set()
                uniq = []
                for ttype, eid, turl in all_ids:
                    if eid not in seen:
                        seen.add(eid)
                        uniq.append((ttype, eid))
                if uniq:
                    logger.debug("SingleFile ext: all extension targets=%r manifest_id=%s", [(t, i) for t, i in uniq], manifest_id)
                # Prefer manifest_id if it's among discovered
                if manifest_id and any(eid == manifest_id for _, eid in uniq):
                    extension_id = manifest_id
                    for ttype, eid in uniq:
                        if eid == manifest_id:
                            discovered_via = ttype + "(manifest_match)"
                            break
                else:
                    # Otherwise prefer any service_worker, then fallback
                    for ttype, eid in uniq:
                        if ttype == "service_worker":
                            extension_id = eid
                            discovered_via = "service_worker"
                            break
                    if not extension_id and uniq:
                        extension_id = uniq[0][1]
                        discovered_via = f"fallback:{uniq[0][0]}"
                        logger.debug("SingleFile ext: extension ID from fallback type %s: %s", uniq[0][0], extension_id)

                # Remember mismatch for later bridge retry
                if manifest_id and extension_id and extension_id != manifest_id:
                    discovered_manifest_mismatch = True
                    logger.debug("SingleFile ext: discovered ID %s != manifest %s; will retry manifest if bridge missing", extension_id, manifest_id)

                if not extension_id and manifest_id:
                    if bridge_installed:
                        extension_id = manifest_id
                        discovered_via = "manifest+bridge"
                        logger.debug("SingleFile ext: Target.getTargets found no extension; using manifest ID %s (bridge installed)", extension_id)
                    else:
                        try:
                            ext_id_via_script = await page.evaluate('''() => {
                                try {
                                    const scripts = Array.from(document.querySelectorAll('script[src*="chrome-extension://"]'));
                                    for (const s of scripts) {
                                        const m = s.src.match(/chrome-extension:\\/\\/([^\\/]+)\\//);
                                        if (m) return m[1];
                                    }
                                    const links = Array.from(document.querySelectorAll('link[href*="chrome-extension://"]'));
                                    for (const l of links) {
                                        const m = l.href.match(/chrome-extension:\\/\\/([^\\/]+)\\//);
                                        if (m) return m[1];
                                    }
                                } catch(e) {}
                                return null;
                            }''')
                            if ext_id_via_script and isinstance(ext_id_via_script, str) and len(ext_id_via_script) >= 10:
                                extension_id = ext_id_via_script
                                discovered_via = "script_src"
                                logger.debug("SingleFile ext: extension ID via script[src] fallback: %s", extension_id)
                            else:
                                extension_id = manifest_id
                                discovered_via = "manifest"
                                logger.debug("SingleFile ext: Target.getTargets found no extension and no bridge signal; trying manifest ID %s anyway (extension may be starting)", extension_id)
                        except Exception:
                            extension_id = manifest_id
                            discovered_via = "manifest"
                            logger.debug("SingleFile ext: Target.getTargets found no extension; trying manifest ID %s", extension_id)

                if not extension_id:
                    try:
                        dbg = [(t.get("type"), (t.get("url") or "")[:80]) for t in target_list[:10]]
                        logger.debug("SingleFile ext: could not determine extension ID (targets=%r)", dbg)
                    except Exception:
                        logger.debug("SingleFile ext: could not determine extension ID")
                    return None

                logger.debug("SingleFile ext: extension ID = %s (via %s)", extension_id, discovered_via or "unknown")

            except Exception as exc:
                if not extension_id and manifest_id:
                    if bridge_installed:
                        extension_id = manifest_id
                        discovered_via = "manifest+bridge(exc)"
                        logger.debug("SingleFile ext: Target discovery failed (%s); using manifest ID %s (bridge installed)", exc, extension_id)
                    else:
                        logger.debug("SingleFile ext: failed to discover extension ID: %s", exc)
                        return None
                else:
                    logger.debug("SingleFile ext: failed to discover extension ID: %s", exc)
                    return None
            finally:
                try:
                    await cdp.detach()
                except Exception:
                    pass
        else:
            if bridge_installed and manifest_id:
                extension_id = manifest_id
                discovered_via = "manifest(no_cdp)+bridge"
                logger.debug("SingleFile ext: no CDP session; using manifest ID %s (bridge installed)", extension_id)
            else:
                logger.debug("SingleFile ext: no CDP session and no bridge signal — cannot capture via extension")
                return None

    except Exception as exc:
        logger.debug("SingleFile ext: failed to discover extension ID: %s", exc)
        if manifest_id and bridge_installed:
            extension_id = manifest_id
            discovered_via = "manifest(outer_exc)+bridge"
            logger.debug("SingleFile ext: outer exception; falling back to manifest ID %s", extension_id)
        else:
            return None
        if cdp is not None:
            try:
                await cdp.detach()
            except Exception:
                pass

    if not extension_id:
        if manifest_id and bridge_installed:
            extension_id = manifest_id
            discovered_via = "manifest(final)+bridge"
            logger.debug("SingleFile ext: final fallback to manifest ID %s (bridge installed)", extension_id)
        else:
            logger.warning("SingleFile ext: extension ID still unknown after all discovery paths")
            return None

    if not bridge_installed:
        try:
            await page.wait_for_timeout(400)
            bridge_installed = await page.evaluate("() => !!window.__singlefile_bridge_installed")
        except Exception:
            pass
        # If bridge still not installed but we have a manifest mismatch,
        # the discovered ID (e.g. fign...) is likely the wrong extension.
        # Try the manifest ID (ebcl...) before giving up — it may be that
        # Target.getTargets didn't list the idle SingleFile service_worker,
        # but the content script (which sets the bridge) is actually from
        # the manifest extension.
        # If bridge still not installed, try manifest ID regardless of whether we
        # discovered a mismatched ID or found nothing at all. This covers the
        # case where Target.getTargets returned no chrome-extension targets
        # (extension service_worker idle or not loaded) but the SingleFile
        # content script *should* be there. Manual injection of the page bridge
        # can recover.
        if not bridge_installed and manifest_id:
            if discovered_manifest_mismatch:
                logger.debug("SingleFile ext: bridge not installed for discovered ID %s (via %s) — trying manifest ID %s", extension_id, discovered_via, manifest_id)
            elif not extension_id:
                logger.debug("SingleFile ext: bridge not installed and no extension targets found — trying manifest ID %s via manual bridge injection", manifest_id)
            else:
                logger.debug("SingleFile ext: bridge not installed for %s — trying manifest ID %s via manual injection", extension_id, manifest_id)
            # Try to manually inject the SingleFile page bridge from disk so
            # the extension can be addressed even if content_scripts didn't
            # inject (e.g. world:MAIN not supported, or profile stale).
            try:
                import pathlib as _pl
                _candidates = [
                    _pl.Path(__file__).resolve().parent / "single" / "lib" / "single-file-page-bridge.js",
                    _pl.Path.cwd() / "single" / "lib" / "single-file-page-bridge.js",
                    _pl.Path.home() / "shifixsxs" / "single" / "lib" / "single-file-page-bridge.js",
                    _pl.Path("/home/user/shifixsxs/single/lib/single-file-page-bridge.js"),
                ]
                _env = __import__('os').environ.get("SINGLEFILE_EXT_DIR", "").strip()
                if _env:
                    _candidates.insert(0, _pl.Path(_env) / "lib" / "single-file-page-bridge.js")
                _bridge_src = None
                _cand = None
                for _cand in _candidates:
                    if _cand.is_file():
                        _bridge_src = _cand.read_text(encoding="utf-8", errors="ignore")
                        break
                if _bridge_src:
                    await page.evaluate("(code) => { (0, eval)(code); }", _bridge_src)
                    logger.debug("SingleFile ext: manually injected page bridge from %s", _cand)
                    bridge_installed = await page.evaluate("() => !!window.__singlefile_bridge_installed")
                    if bridge_installed:
                        extension_id = manifest_id
                        discovered_via = "manifest+manual_bridge"
                        logger.debug("SingleFile ext: manual bridge injection succeeded, now using manifest ID %s", extension_id)
            except Exception as _ibe:
                logger.debug("SingleFile ext: manual bridge injection failed: %s", _ibe)
        if not bridge_installed:
            logger.warning("SingleFile ext: bridge not installed for %s (extension not loaded for this tab, ID was %s via %s) — falling back", url[:80], extension_id, discovered_via)
            return None
        else:
            logger.debug("SingleFile ext: bridge appeared after short wait")

    # ---------------------------------------------------------------
    # 2. Ask the PAGE bridge to send the capture request.
    #
    # The bridge is already injected by:
    #
    #     lib/single-file-page-bridge.js
    #
    # It receives the window.postMessage(), calls
    # chrome.runtime.sendMessage(), and posts the response back.
    # ---------------------------------------------------------------

    capture_config = {
        "compressHTML": False,
        "blockImages": False,
        "blockFonts": False,
        "removeHiddenElements": False,
        "removeUnusedStyles": False,
        "removeUnusedFonts": False,
        "removeAlternativeFonts": False,
        "removeAlternativeMedias": False,
        "removeAlternativeImages": False,
        "groupDuplicateImages": False,
        "loadDeferredImages": True,
        "loadDeferredImagesMaxIdleTime": 1500,
        "removeFrames": False,
        "compressCSS": False,
        "moveStylesInHead": False,
    }

    timeout_ms = int(timeout * 1000)

    expression = r"""
    async ({ extensionId, captureConfig, timeoutMs }) => {
        const REQUEST_KEY = "__singlefile_bridge_request";
        const RESPONSE_KEY = "__singlefile_bridge_response";

        // Make sure the bridge exists.
        if (!window.__singlefile_bridge_installed) {
            return {
                __sf_err: "page_bridge_not_installed"
            };
        }

        const requestId =
            "__sf_" +
            Date.now().toString(36) +
            "_" +
            Math.random().toString(36).slice(2);

        return await new Promise((resolve) => {
            let finished = false;

            const cleanup = () => {
                window.removeEventListener(
                    "message",
                    onMessage
                );

                if (timer) {
                    clearTimeout(timer);
                }
            };

            const finish = (value) => {
                if (finished) {
                    return;
                }

                finished = true;
                cleanup();
                resolve(value);
            };

            const onMessage = (event) => {
                if (event.source !== window) {
                    return;
                }

                const data = event.data;

                if (!data ||
                    data[RESPONSE_KEY] !== true ||
                    data.requestId !== requestId) {
                    return;
                }

                finish(data.resp || {
                    __sf_err: "empty_bridge_response"
                });
            };

            const timer = setTimeout(() => {
                finish({
                    __sf_err: "bridge_timeout"
                });
            }, timeoutMs + 5000);

            window.addEventListener(
                "message",
                onMessage
            );

            window.postMessage(
                {
                    [REQUEST_KEY]: true,
                    requestId,
                    extId: extensionId,
                    message: {
                        method: "capture-page",
                        trustedCaller: true,
                        ...captureConfig
                    }
                },
                "*"
            );
        });
    }
    """

    try:
        result = await page.evaluate(
            expression,
            {
                "extensionId": extension_id,
                "captureConfig": capture_config,
                "timeoutMs": timeout_ms,
            },
        )
    except Exception as exc:
        logger.warning(
            "SingleFile ext: page bridge evaluation failed: %s",
            exc,
        )
        return None

    if not isinstance(result, dict):
        logger.warning(
            "SingleFile ext: invalid bridge result: %r",
            result,
        )
        return None

    if result.get("__sf_err"):
        logger.warning(
            "SingleFile ext: %s",
            result["__sf_err"],
        )
        return None

    content = result.get("content")

    if not content:
        logger.warning(
            "SingleFile ext: bridge response contained no content: %r",
            result,
        )
        return None

    if not isinstance(content, str):
        logger.warning(
            "SingleFile ext: content was not a string: %s",
            type(content).__name__,
        )
        return None

    logger.debug(
        "SingleFile ext: capture successful (%d bytes)",
        len(content),
    )

    return content


async def _capture_via_library_injection(page: Any, *, timeout: int) -> Optional[str]:
    """Original library-injection path.  Kept as a fallback for environments
    where the extension isn't loaded (or extension mode is off)."""
    sources = await _load_singlefile_sources()
    if not sources:
        return None

    # --- CSP bypass for Yahoo and other strict-CSP sites -----------------
    # SingleFile's library injection uses inline <script> tags which are
    # blocked by nonce/hash CSP (e.g. Yahoo: script-src 'nonce-...').
    # Real browsers honor CSP, but Playwright can bypass it per-page via
    # CDP. We enable bypass for this page only so the injection succeeds
    # without flipping the global `bypass_csp` (which is a bot tell).
    # We also strip CSP headers for future navigations and remove <meta
    # http-equiv="Content-Security-Policy"> already in the DOM.
    try:
        cdp = await page.context.new_cdp_session(page)
        try:
            await cdp.send("Page.setBypassCSP", {"enabled": True})
            logger.debug("SingleFile lib: CSP bypass via CDP enabled")
        finally:
            try:
                await cdp.detach()
            except Exception:
                pass
    except Exception:
        pass

    # Strip CSP headers for subsequent requests (covers navigations after
    # the initial document). Keep per-context and per-page handlers; both
    # are tolerated to be registered multiple times.
    try:
        async def _strip_csp(route, _request):
            try:
                response = await route.fetch()
                headers = {
                    k: v for k, v in response.headers.items()
                    if k.lower() not in (
                        "content-security-policy",
                        "content-security-policy-report-only",
                        "x-content-security-policy",
                    )
                }
                body = await response.body()
                await route.fulfill(response=response, headers=headers, body=body)
            except Exception:
                try:
                    await route.continue_()
                except Exception:
                    pass

        # page-level route is most reliable for the current document;
        # context-level is a safety net for new pages.
        for target in (page, page.context):
            try:
                await target.route("**/*", _strip_csp)
            except Exception:
                pass
    except Exception:
        pass

    # Remove <meta http-equiv="Content-Security-Policy"> already parsed.
    try:
        await page.evaluate('''() => {
            document.querySelectorAll('meta[http-equiv="Content-Security-Policy"], meta[http-equiv="content-security-policy"], meta[http-equiv="Content-Security-Policy-Report-Only"]')
                .forEach(m => m.remove());
        }''')
    except Exception:
        pass

    # Inject SingleFile hook + main scripts.
    # add_script_tag is blocked by nonce-CSP even after header stripping
    # (the initial document's CSP is already enforced). Prefer a CDP
    # evaluation path that is not subject to CSP, then fall back to
    # nonce-aware script-tag injection and finally plain add_script_tag.
    injected = False
    last_exc = None

    # 1) CDP Runtime evaluation (bypasses CSP) — most reliable.
    # Playwright's page.evaluate is implemented via Runtime.callFunctionOn
    # which is outside CSP. Evaluating the script source directly as an
    # expression executes it in the page.
    for key in ("hook", "main"):
        code = sources.get(key)
        if not code:
            continue
        try:
            # Evaluate the script source as a program. Wrapping in an
            # async IIFE ensures top-level await / return handling works
            # while still executing for side effects (window.singlefile).
            await page.evaluate("(code) => { (0, eval)(code); }", code)
            injected = True
            logger.debug("SingleFile lib: injected %s via evaluate", key)
        except Exception as exc:
            last_exc = exc
            logger.debug("SingleFile lib: evaluate inject %s failed: %s", key, exc)
            injected = False
            break

    # Verify
    try:
        has_sf = await page.evaluate("() => !!window.singlefile")
        if has_sf:
            injected = True
        else:
            injected = False
    except Exception:
        injected = False

    if not injected:
        # 2) Nonce-aware <script> injection — reuses the page's own nonce
        # so the inline script passes the nonce check.
        try:
            ok = await page.evaluate('''(codes) => {
                const getNonce = () => {
                    const s = document.querySelector('script[nonce]');
                    if (s) return s.nonce || s.getAttribute('nonce') || s.getAttribute('data-nonce');
                    // some Yahoo pages store nonce on the CSP meta or html
                    const html = document.documentElement;
                    if (html && html.getAttribute('nonce')) return html.getAttribute('nonce');
                    return null;
                };
                const nonce = getNonce();
                for (const code of codes) {
                    const el = document.createElement('script');
                    if (nonce) el.setAttribute('nonce', nonce);
                    // also set data-nonce for frameworks that check it
                    el.textContent = code;
                    (document.head || document.documentElement).appendChild(el);
                    el.remove();
                }
                return !!window.singlefile;
            }''', [sources["hook"], sources["main"]])
            if ok:
                injected = True
                logger.debug("SingleFile lib: injected via nonce-aware script tag")
            else:
                logger.debug("SingleFile lib: nonce-aware injection did not create window.singlefile")
        except Exception as exc:
            last_exc = exc
            logger.debug("SingleFile lib: nonce-aware injection failed: %s", exc)

    if not injected:
        # 3) Plain add_script_tag — last resort (will fail on strict nonce CSP)
        try:
            await page.add_script_tag(content=sources["hook"])
            await page.add_script_tag(content=sources["main"])
            injected = True
            logger.debug("SingleFile lib: injected via add_script_tag fallback")
        except Exception as exc:
            last_exc = exc
            logger.warning("SingleFile lib: script injection failed: %s", exc)
            # keep last_exc for warning below
            pass

    if not injected:
        # Final verify before bailing
        try:
            has_sf2 = await page.evaluate("() => !!window.singlefile")
            if has_sf2:
                injected = True
        except Exception:
            pass

    if not injected:
        if last_exc is not None:
            logger.warning("SingleFile lib: script injection failed: %s", last_exc)
        else:
            logger.warning("SingleFile lib: script injection failed: window.singlefile not present after injection")
        return None

    try:
        page_data = await asyncio.wait_for(
            page.evaluate(
                """async (opts) => {
                    if (!window.singlefile) {
                        throw new Error('window.singlefile not present after injection');
                    }
                    return await window.singlefile.getPageData(opts);
                }""",
                {
                    "zipScript": sources["zip"],
                    "compressHTML": False,
                    "blockImages": False,
                    "blockFonts": False,
                    "removeHiddenElements": False,
                    "removeUnusedStyles": False,
                    "removeUnusedFonts": False,
                    "removeAlternativeFonts": False,
                    "removeAlternativeMedias": False,
                    "removeAlternativeImages": False,
                    "groupDuplicateImages": False,
                    "loadDeferredImages": True,
                    "loadDeferredImagesMaxIdleTime": 1500,
                    "removeFrames": False,
                    "compressCSS": False,
                    "moveStylesInHead": False,
                },
            ),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        logger.warning("SingleFile lib: getPageData timed out after %ss", timeout)
        return None
    except Exception as exc:
        logger.warning("SingleFile lib: getPageData failed: %s", exc)
        return None

    if not page_data:
        return None
    if isinstance(page_data, dict):
        return page_data.get("content") or page_data.get("html")
    if isinstance(page_data, str):
        return page_data
    return None


# ---------------------------------------------------------------------------
# Generic interaction-trigger detection
# ---------------------------------------------------------------------------
#
# Goal: capture when the user ACTIVATES an interactive element, not
# merely when the mouse moves or the page polls.  This JS snippet is
# installed once per page and works for any site because:
#
#   * It uses a single delegated listener on `document` in the CAPTURE
#     phase, so dynamically-added elements are picked up for free
#     (no per-element registration, no MutationObserver).
#
#   * It decides "is this element actually interactive?" with a
#     fully generic predicate -- native semantics, ARIA roles,
#     tabindex, and inline event handlers.  No selectors, no class
#     names, no per-site configuration.
#
#   * It resolves the rightmost *interactive* ancestor of the
#     original event target using event.composedPath() so a click on
#     an <svg> or <span> inside a <button>/<a> still resolves to the
#     <button>/<a> -- without ever firing twice for a single user
#     activation (dedupe is done with a per-event flag set in the
#     capture phase).
#
#   * When a real activation is detected it calls the globally
#     exposed `window.__domCaptureRequest(reason)`.  The Python side
#     wires that to `DOMCaptureSession.send_page` (see
#     `_install_interaction_trigger`).
#
# Mouse clicks are the obvious case, but the snippet also treats
# keyboard activation of an interactive element (Enter / Space on a
# button/anchor, Enter inside a contenteditable, etc.) as activation,
# so a focus-and-Enter flow from the remote-control client also
# triggers a recapture.

_INTERACTION_TRIGGER_JS = r"""
(() => {
    if (window.__domCaptureTriggerInstalled) return;
    window.__domCaptureTriggerInstalled = true;

    // ----------------------------------------------------------------
    // Track which elements have JS-attached interactive listeners.
    // We can't enumerate addEventListener listeners from the page
    // side, so we patch EventTarget.prototype.addEventListener once
    // and record (element, type) pairs.  This catches React/Vue/etc.
    // synthetic listeners, jQuery .on(), and any framework that uses
    // addEventListener under the hood.  It does NOT require us to
    // poll the DOM or to know about specific frameworks.
    // ----------------------------------------------------------------
    if (!window.__domCaptureListenerTrackerInstalled) {
        window.__domCaptureListenerTrackerInstalled = true;
        window.__domCaptureInteractiveListeners = new WeakSet();

        const origAdd = EventTarget.prototype.addEventListener;
        const origRemove = EventTarget.prototype.removeEventListener;
        // Activation-bearing event types.  We only flag elements
        // that have one of these -- a passive scroll listener doesn't
        // make an element "interactive".
        const ACTIVATION_TYPES = new Set([
            "click", "mousedown", "mouseup", "pointerdown",
            "pointerup", "keydown", "keyup", "keypress",
            "touchstart", "touchend", "submit", "change", "input"
        ]);

        EventTarget.prototype.addEventListener = function (type, listener, options) {
            try {
                if (this && this.nodeType === 1 && ACTIVATION_TYPES.has(String(type))) {
                    window.__domCaptureInteractiveListeners.add(this);
                }
            } catch (_) {}
            return origAdd.apply(this, arguments);
        };
        EventTarget.prototype.removeEventListener = function (type, listener, options) {
            // We don't bother removing from the WeakSet on remove --
            // the element dropping the listener still won't be
            // activated, the only effect of leaving it in the set is
            // that we may flag it as interactive when it isn't.  Not
            // a correctness problem because isVisible filters out
            // detached / disabled elements; an extra capture is
            // preferable to false negatives.
            return origRemove.apply(this, arguments);
        };
    }

    // ARIA roles that represent a genuinely interactive widget.
    // Source: https://www.w3.org/TR/wai-aria-1.2/#widget_roles
    // plus the roles in https://www.w3.org/TR/wai-aria-1.2/#document_structure_roles
    // that are user-activated.  Anything not in this list is treated
    // as non-interactive even if the element has role="...".
    const INTERACTIVE_ROLES = new Set([
        "button", "link", "menuitem", "menuitemcheckbox", "menuitemradio",
        "checkbox", "radio", "switch", "tab", "option", "combobox",
        "textbox", "searchbox", "spinbutton", "slider", "treeitem",
        "rowheader", "columnheader", "gridcell", "row"
    ]);

    // <input> types that are actually clickable / activatable.  Things
    // like type="hidden" or type="text" are NOT activation targets on
    // their own (text inputs capture on commit, handled separately).
    const CLICKABLE_INPUT_TYPES = new Set([
        "submit", "reset", "button", "image", "checkbox", "radio",
        "file", "color", "range"
    ]);

    function isVisible(node) {
        if (!node || node.nodeType !== 1) return false;
        // Element.isConnected check first so detached subtrees return
        // false without crawling.
        if (node.isConnected === false) return false;
        // Honor disabled / aria-disabled.
        if (node.disabled === true) return false;
        if (node.getAttribute && node.getAttribute("aria-disabled") === "true") return false;
        // Inline display:none / visibility:hidden make the element
        // not actually clickable.  We check inline style only --
        // we deliberately do NOT walk computed styles because that
        // forces a layout and adds latency to every click handler.
        // Sites that use CSS classes to hide things will still
        // appear "visible" to us; the worst case is a false
        // positive (a capture on a hidden element), not a false
        // negative.
        try {
            if (node.style && (node.style.display === "none" ||
                               node.style.visibility === "hidden")) {
                return false;
            }
        } catch (_) { /* style object unavailable on some node types */ }
        return true;
    }

    function isInteractive(node) {
        if (!node || node.nodeType !== 1) return false;
        const tag = node.tagName;

        // 1. Native interactive elements.
        if (tag === "A") {
            // <a> is only interactive if it has an href, name, or is
            // explicitly a control via role.
            if (node.hasAttribute("href")) return true;
            if (node.hasAttribute("role")) return true;
            return false;
        }
        if (tag === "BUTTON" || tag === "SELECT" || tag === "TEXTAREA" ||
            tag === "SUMMARY" || tag === "DETAILS" || tag === "LABEL" ||
            tag === "OPTION" || tag === "OUTPUT") {
            return true;
        }
        // Apple/iCloud renders some real controls as a custom host around a
        // native button, for example <ui-button role="button"
        // tabindex="0">.  Keep the explicit tag in the activation list even
        // when a framework temporarily removes one of those ARIA/focus
        // attributes; the host is still the element that owns the gesture.
        if (tag === "UI-BUTTON") return true;
        if (tag === "INPUT") {
            const t = (node.getAttribute("type") || "text").toLowerCase();
            return CLICKABLE_INPUT_TYPES.has(t);
        }

        // 2. tabindex >= 0 makes an element focusable and therefore
        //    activatable.
        const ti = node.getAttribute && node.getAttribute("tabindex");
        if (ti !== null && ti !== undefined) {
            const n = Number(ti);
            if (!Number.isNaN(n) && n >= 0) return true;
        }

        // 3. ARIA role.
        const role = (node.getAttribute && node.getAttribute("role") || "").toLowerCase().split(/\s+/)[0];
        if (role && INTERACTIVE_ROLES.has(role)) return true;

        // 4. Inline event-handler attributes: if the site wired a
        //    click/mousedown/pointerdown/keydown/keypress handler
        //    inline, the element is interactive by definition.
        if (node.onclick || node.onmousedown || node.onpointerdown ||
            node.onkeydown || node.onkeypress || node.onkeyup) {
            return true;
        }

        // 5. JS-attached activation listeners (React onClick, jQuery
        //    .on('click', ...), framework addEventListener('click'),
        //    etc.).  We tracked these at install time via a one-time
        //    EventTarget.prototype.addEventListener patch above.
        try {
            if (window.__domCaptureInteractiveListeners &&
                window.__domCaptureInteractiveListeners.has(node)) {
                return true;
            }
        } catch (_) { /* WeakSet not available in some sandboxes */ }

        return false;
    }

    // Walk up the composed path (which crosses shadow boundaries) to
    // find the rightmost interactive ancestor.  This is the key piece
    // for "child of button resolves to button" -- we DO NOT use
    // event.target blindly because the deepest element often isn't the
    // actionable one (icons, spans, SVGs inside real controls).
    function resolveInteractive(path) {
        if (!path || path.length === 0) return null;
        // First-pass: find the rightmost interactive element on the
        // path.  If the event target itself is interactive, we prefer
        // that (it's the most specific).  Otherwise we walk outward.
        for (let i = 0; i < path.length; i++) {
            const n = path[i];
            if (isInteractive(n) && isVisible(n)) {
                return n;
            }
        }
        return null;
    }

    // Dedupe per user activation.  We set a flag on the first
    // interactive element we find; the capture-phase listener sets
    // a "consumed" sentinel on `event` so bubble-phase children of
    // the same activation don't double-fire.
    const CONSUMED = Symbol.for("__domCaptureConsumed");

    function requestCapture(reason, target) {
        try {
            if (typeof window.__domCaptureRequest === "function") {
                window.__domCaptureRequest(reason, target ? (target.tagName + (target.id ? "#" + target.id : "")) : "");
            }
        } catch (_) { /* host not wired -- ignore */ }
    }

    function handleActivation(reason) {
        return function (event) {
            // The flag is checked at the start of the handler.  The
            // capture-phase listener for the SAME event type runs
            // before any bubble-phase child listener, so we set it
            // here on `event` directly -- no reliance on the target
            // being the same node.
            if (event[CONSUMED]) return;
            event[CONSUMED] = true;

            // composedPath includes shadow-DOM roots; walk all of them.
            const path = (typeof event.composedPath === "function")
                ? event.composedPath()
                : [];
            const interactive = resolveInteractive(path);

            if (!interactive) {
                // Real click/Enter, but on plain content.  Do nothing.
                return;
            }

            requestCapture(reason, interactive);
        };
    }

    // Capture-phase listeners.  Using capture (not bubble) means:
    //   1. We see the activation before site handlers, so we never
    //      miss it because stopPropagation() was called.
    //   2. Dedupe via event[CONSUMED] is well-defined: a single
    //      activation yields exactly one capture-phase event per type.
    // We listen to `click` and `auxclick` only -- not pointerup or
    // mousedown.  Listening to multiple activation-bearing events
    // would fire the recapture once per event in the same gesture
    // (a real click fires pointerdown -> pointerup -> click, three
    // events).  `click` already covers normal left-click; `auxclick`
    // covers middle/right-click on platforms that fire it.
    document.addEventListener(
        "click",
        handleActivation("click"),
        true
    );
    document.addEventListener(
        "auxclick",
        handleActivation("auxclick"),
        true
    );

    // Keyboard activation: Enter / Space on a focused interactive
    // element.  We don't recapture on every keydown -- only on keys
    // that actually activate the focused control.  For keyboard
    // events we walk from event.target up to the document root
    // (rather than using composedPath) because keyboard events
    // dispatched via the browser already target the focused
    // element, and the synthetic-event case (test harnesses, some
    // frameworks) may produce a composedPath that omits the
    // actual target.
    function buildPathFromTarget(t) {
        const path = [];
        let n = t;
        while (n && n.nodeType === 1) {
            path.push(n);
            n = n.parentNode;
            if (!n || n === document) { path.push(document); break; }
        }
        return path;
    }

    document.addEventListener(
        "keydown",
        function (event) {
            const key = event.key;
            if (key !== "Enter" && key !== " " && key !== "Spacebar") return;
            const ae = event.target || document.activeElement;
            if (!ae || ae.nodeType !== 1) return;
            if (!isInteractive(ae) || !isVisible(ae)) return;
            // For links, Enter activates.  For buttons, Enter or
            // Space activates.  isInteractive already filtered
            // anchors/buttons/roles; this branch just gates on the
            // key.  We hand-built the path above because
            // composedPath() may not include event.target for
            // synthetic dispatch.
            const path = buildPathFromTarget(ae);
            if (event[CONSUMED]) return;
            event[CONSUMED] = true;
            const interactive = resolveInteractive(path);
            if (!interactive) return;
            requestCapture("keydown", interactive);
        },
        true
    );
})();
"""


# ---------------------------------------------------------------------------
# Live-delta layer (LIVE_DELTA=1)
# ---------------------------------------------------------------------------
#
# In-page MutationObserver that batches compact DOM ops between full
# captures and ships them through the ``__domDelta`` binding.  Op grammar
# (positional arrays, applied in order by client.html):
#   ['a', mid, name, value|null]        attribute set / remove
#   ['t', parentMid, textIndex, value]  characterData on n-th text child
#   ['i', parentMid, refMid|0, html]    insert serialized subtree before ref
#   ['r', mid]                          remove element
#   ['v', mid, value, checked|null]     form control live state
#   ['overflow']                        op budget blown → server recaptures
#
# Node identity: every serialized element carries a data-mid stamped from
# the page-lifetime WeakMap (__domMidMap / __domMidNext) shared with the
# fast full serializer, so ids stay valid across full resyncs.  Inserted
# subtrees get fresh ids before serialization.
#
# Form values are mirrored from 'input'/'change' EVENTS (MutationObserver
# cannot see property-only .value changes — the classic trap), coalesced
# per node per batch so fast typing ships the latest value once.
_DELTA_OBSERVER_JS = r"""
(() => {
  // Init scripts also run in child frames.  The live mirror serializes the
  // main document, so child-frame observers would create colliding mids and
  // false patches for content the client never received.
  if (window.top !== window) return;
  if (window.__shifixDeltaInstalled) return;
  window.__shifixDeltaInstalled = true;
  if (!window.__domMidMap) window.__domMidMap = new WeakMap();
  if (!window.__domMidNext) window.__domMidNext = 1;
  const MID = 'data-mid';
  const DOC_TOKEN = (() => {
    try {
      return String(location.href || '') + '|' + String(performance.timeOrigin || Date.now());
    } catch (e) { return String(Date.now()); }
  })();
  const midOf = (el) => {
    if (!el || el.nodeType !== 1) return 0;
    let m = window.__domMidMap.get(el);
    if (!m) { m = window.__domMidNext++; window.__domMidMap.set(el, m); }
    return m;
  };
  const escText = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  const escAttr = (s) => String(s).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;');
  const VOID = new Set(['area','base','br','col','embed','hr','img','input','link','meta','param','source','track','wbr']);
  const serSubtree = (node, out) => {
    const t = node.nodeType;
    if (t === 3) { out.push(escText(node.nodeValue)); return; }
    if (t === 8) { out.push('<!--', String(node.nodeValue).replace(/--/g, '--'), '-->'); return; }
    if (t !== 1) return;
    const el = node;
    const tag = (el.localName || el.tagName.toLowerCase());
    if (tag === 'base') return;
    out.push('<', tag);
    const isFormCtl = (tag === 'input' || tag === 'option');
    const list = el.attributes;
    for (let i = 0; i < list.length; i++) {
      const a = list[i];
      if (a.name === MID) continue;
      if (isFormCtl && (a.name === 'value' || a.name === 'checked' || a.name === 'selected')) continue;
      out.push(' ', a.name, '="', escAttr(a.value), '"');
    }
    out.push(' ', MID, '="', String(midOf(el)), '"');
    if (tag === 'input') {
      const ty = (el.getAttribute('type') || 'text').toLowerCase();
      if (ty === 'checkbox' || ty === 'radio') { if (el.checked) out.push(' checked'); }
      else if (ty !== 'file') out.push(' value="', escAttr(el.value == null ? '' : el.value), '"');
    } else if (tag === 'option') {
      if (el.selected) out.push(' selected');
    }
    out.push('>');
    if (VOID.has(tag)) return;
    if (tag === 'textarea') { out.push(escText(el.value == null ? '' : el.value), '</textarea>'); return; }
    if (tag === 'script' || tag === 'style') {
      let raw = el.textContent || '';
      if (tag === 'script') raw = raw.replace(/<\/script/gi, '<\\/script');
      out.push(raw, '</', tag, '>');
      return;
    }
    for (const c of el.childNodes) serSubtree(c, out);
    out.push('</', tag, '>');
  };
  const ops = [];
  // Keep the first occurrence's position (important when a framework inserts
  // a node and then mutates it), while replacing repeated writes to the same
  // target with the latest value in this batch.
  const coalesced = new Map();  // logical op key -> index in ops
  let scheduled = false;
  let overflowed = false;
  const OP_CAP = 1500;
  const schedule = () => {
    if (scheduled) return;
    scheduled = true;
    setTimeout(flush, __SHIFIX_DELTA_FLUSH_MS__);
  };
  const markOverflow = () => {
    if (overflowed) return;
    overflowed = true;
    ops.length = 0;
    coalesced.clear();
    ops.push(['overflow']);
    schedule();
  };
  const flush = () => {
    scheduled = false;
    if (!ops.length) return;
    const batch = ops.splice(0, ops.length);
    coalesced.clear();
    overflowed = false;
    try {
      if (typeof window.__domDelta === 'function') {
        window.__domDelta(JSON.stringify(batch), DOC_TOKEN);
      }
    } catch (e) { /* binding not wired yet */ }
  };
  const push = (op) => {
    if (overflowed) return;
    ops.push(op);
    if (ops.length > OP_CAP) markOverflow();
    schedule();
  };
  const pushCoalesced = (key, op) => {
    if (overflowed) return;
    const oldIndex = coalesced.get(key);
    if (oldIndex === undefined) {
      coalesced.set(key, ops.length);
      ops.push(op);
      if (ops.length > OP_CAP) markOverflow();
    } else {
      ops[oldIndex] = op;
    }
    schedule();
  };
  const pushAttr = (el, name) => {
    const id = midOf(el);
    if (!id) return;
    pushCoalesced('a\\0' + id + '\\0' + name, ['a', id, name, el.getAttribute(name)]);
  };
  const pushText = (parent, index, value) => {
    const id = midOf(parent);
    if (!id) return;
    pushCoalesced('t\\0' + id + '\\0' + index, ['t', id, index, value]);
  };
  const pushVal = (el) => {
    const id = midOf(el);
    if (!id) return;
    const checked = (el.tagName === 'INPUT' && (el.type === 'checkbox' || el.type === 'radio')) ? !!el.checked : null;
    pushCoalesced('v\\0' + id, ['v', id, (el.value == null ? '' : String(el.value)), checked]);
  };
  const mo = new MutationObserver((recs) => {
    for (const r of recs) {
      if (r.type === 'attributes') {
        const el = r.target;
        if (r.attributeName === MID) continue;   // our own stamping
        pushAttr(el, r.attributeName);
      } else if (r.type === 'characterData') {
        const p = r.target.parentNode;
        if (!p || p.nodeType !== 1) continue;
        let idx = -1, i = 0;
        for (const c of p.childNodes) {
          if (c.nodeType === 3) { if (c === r.target) { idx = i; break; } i++; }
        }
        if (idx >= 0) pushText(p, idx, r.target.nodeValue);
      } else if (r.type === 'childList') {
        const pm = midOf(r.target);
        if (!pm) { markOverflow(); continue; }
        for (const rem of r.removedNodes) {
          if (rem.nodeType !== 1) { markOverflow(); continue; }
          const m = window.__domMidMap.get(rem);
          if (m) push(['r', m]);
          else markOverflow();
        }
        for (const add of r.addedNodes) {
          if (add.nodeType !== 1) { markOverflow(); continue; }
          let ref = r.nextSibling;
          while (ref && ref.nodeType !== 1) ref = ref.nextSibling;
          const refMid = ref ? midOf(ref) : 0;
          const buf = [];
          serSubtree(add, buf);
          if (!buf.length) { markOverflow(); continue; }
          push(['i', pm, refMid, buf.join('')]);
        }
      }
    }
  });
  mo.observe(document, {
    subtree: true, childList: true, attributes: true, characterData: true,
    attributeOldValue: false, characterDataOldValue: false,
  });
  const onFormEvent = (e) => {
    const t = e.target;
    if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT')) pushVal(t);
  };
  document.addEventListener('input', onFormEvent, true);
  document.addEventListener('change', onFormEvent, true);
  window.__shifixDeltaFlush = flush;
  // A document can reload to the same URL.  Its token changes even when the
  // URL watcher cannot, so the server can force a full navigation resync
  // instead of letting new-document mids patch the old mirror.
  setTimeout(() => {
    try {
      if (typeof window.__domDelta === 'function') {
        window.__domDelta(JSON.stringify([['navigation']]), DOC_TOKEN);
      }
    } catch (e) { /* binding not wired yet */ }
  }, 0);
})();
"""

# Keep the browser-side timer configurable without rebuilding the large script
# at each install.  The resolved script is persistent across navigations.
_DELTA_OBSERVER_JS = _DELTA_OBSERVER_JS.replace(
    "__SHIFIX_DELTA_FLUSH_MS__", str(DELTA_FLUSH_MS)
)


async def _install_delta_observer(page: Any, session: "DOMCaptureSession") -> bool:
    """Install the delta observer + ``__domDelta`` relay binding.

    The binding and init script are installed once per page object.  Both
    survive a normal navigation; only the immediate evaluate is repeated for
    the current document.  Re-exposing a Playwright/SB binding on every URL
    change is an avoidable CDP round trip and can fail with "already exists",
    which used to leave the new document without live patches.
    """
    if not LIVE_DELTA or page is None or session is None:
        return False

    async def _on_delta(payload: Any, doc_token: Any = None) -> None:
        try:
            if session.page is not page:
                return
            if session.websocket is None or session._stopped:
                return
            if not isinstance(payload, str) or not payload or len(payload) > 1_000_000:
                return
            # JSON.stringify in the page always emits a compact array of
            # operation arrays.  Shape-check the text and keep it intact for
            # the normal path: parsing it in Python only to dump it again was
            # pure CPU/memory churn.  The two control batches are exact strings
            # emitted by this observer and are handled before the fast path.
            payload_text = payload.strip()
            if not payload_text.startswith("[[") or not payload_text.endswith(']]'):
                return
            if payload_text == '[["navigation"]]':
                # The observer runs once per main document.  A reload can keep
                # the same URL, so this control op is the navigation signal
                # that the URL poller cannot provide.  Full capture is still
                # suppressed during initial bootstrap, when the initial sender
                # owns the first document.
                session._delta_active = False
                if (session.has_initial_capture
                        and not session._explicit_navigation_in_progress):
                    await session._capture_navigation_once("navigation")
                return
            if payload_text == '[["overflow"]]':
                if not session._delta_active or session._send_inflight:
                    return
                logger.debug("dc delta overflow -> full recapture (client %s)", session.client_id)
                await session.send_page(reason="delta_overflow")
                return
            if payload_text == "[[]]":
                return
            if not session._delta_active:
                return  # current full-capture tier cannot carry safe mids
            if session._send_inflight:
                return  # pending full snapshot supersedes queued patches

            # The browser already paid for JSON.stringify(batch).  Wrap that
            # exact JSON text instead of parsing and serializing the operation
            # list a second time.  Recheck inflight state under the send lock
            # so a full recovery cannot be followed by an old-generation patch.
            async with session._ws_send_lock:
                if session._send_inflight:
                    return
                gen = int(session._gen)
                doc_json = _json.dumps(str(doc_token)[:256], ensure_ascii=False) if doc_token is not None else "null"
                frame = '{"type":"dom_patch","gen":' + str(gen) + ',"doc":' + doc_json + ',"ops":' + payload_text + '}'
                await session.websocket.send_text(frame)
        except Exception as exc:
            logger.debug("delta relay failed: %s", exc)

    if not getattr(session, "_delta_binding_installed", False):
        try:
            await page.expose_function("__domDelta", _on_delta)
            session._delta_binding_installed = True
        except Exception as exc:
            logger.debug("expose_function(__domDelta) failed: %s", exc)
            return False
    if not getattr(session, "_delta_init_script_installed", False):
        try:
            await page.add_init_script(script=_DELTA_OBSERVER_JS)
            session._delta_init_script_installed = True
        except Exception as exc:
            logger.debug("add_init_script(delta observer) failed: %s", exc)
            return False
    try:
        await page.evaluate(_DELTA_OBSERVER_JS)
    except Exception as exc:
        logger.debug("install delta observer (immediate) failed: %s", exc)
    return True


async def _install_interaction_trigger(page: Any, session: "DOMCaptureSession") -> bool:
    """Install the generic interaction-trigger on a Playwright page.

    Idempotent: re-injection is a no-op (the JS guards itself with
    a window flag).  Returns True on success, False on any failure
    -- the page will still work, it just won't auto-recapture.
    """
    if page is None or session is None:
        return False
    try:
        # Expose the Python-side handler to the page.  We pass the
        # reason through; the JS side does the interactivity
        # detection, this side just calls send_page.
        async def _on_request(reason: str, target_desc: str) -> None:
            try:
                # Once the observer is installed, it is the single low-latency
                # visual path.  Do not launch a full serializer after every
                # safe click: full documents are reserved for navigation,
                # observer overflow, structural desync, and explicit resync.
                if getattr(session, "_delta_active", False):
                    return

                # Flood control remains for the no-delta fallback path, where
                # the full document is the only way to reflect an activation.
                now = asyncio.get_event_loop().time()
                last = getattr(session, "_last_interaction_capture_t", 0.0)
                _min_iv = (SNAPSHOT_CAPTURE_MIN_INTERVAL_S
                           if getattr(session, "snapshot_only", False)
                           else INTERACTION_CAPTURE_MIN_INTERVAL_S)
                if (now - last) < _min_iv:
                    logger.debug(
                        "interaction recapture suppressed (debounce): "
                        "reason=%s target=%s",
                        reason, target_desc,
                    )
                    return
                session._last_interaction_capture_t = now
                full_reason = f"interaction:{reason}"
                if target_desc:
                    full_reason = f"{full_reason}:{target_desc}"
                asyncio.create_task(session.send_page(reason=full_reason))
            except Exception as exc:
                logger.debug("interaction recapture dispatch failed: %s", exc)

        if not getattr(session, "_interaction_binding_installed", False):
            await page.expose_function(
                "__domCaptureRequest",
                _on_request,
            )
            session._interaction_binding_installed = True
    except Exception as exc:
        logger.debug("expose_function(__domCaptureRequest) failed: %s", exc)
        return False

    # Install the trigger via add_init_script so it runs at the very
    # start of every page load, BEFORE any page script.  This is the
    # only way to catch addEventListener registrations that happen at
    # module top-level (React/Vue/Material bundle entry points).  If
    # we used page.evaluate here, the patch would land AFTER the
    # page's own scripts had already wired their listeners, and the
    # WeakSet would miss them -- which manifests as "I clicked a
    # Material button and got no recapture".
    #
    # add_init_script is idempotent in the JS-side: the
    # __domCaptureTriggerInstalled guard short-circuits the second
    # onward navigations where the script is re-injected by Playwright.
    # We still attempt a one-time page.evaluate so the very first
    # page (if it was already loaded before this function ran) gets
    # the trigger immediately rather than waiting for the next nav.
    if not getattr(session, "_interaction_init_script_installed", False):
        try:
            await page.add_init_script(script=_INTERACTION_TRIGGER_JS)
            session._interaction_init_script_installed = True
        except Exception as exc:
            logger.debug("add_init_script(interaction trigger) failed: %s", exc)
            return False

    try:
        await page.evaluate(_INTERACTION_TRIGGER_JS)
    except Exception as exc:
        # Not fatal -- the next navigation will re-inject via
        # add_init_script and the JS-side guard handles that.
        logger.debug("install interaction trigger (immediate) failed: %s", exc)

    # Live-delta is also installed for snapshot-preferred hosts.  Those hosts
    # still receive periodic/full recovery documents, but mutation patches
    # keep the visible mirror current between them (hybrid mode).
    try:
        if await _install_delta_observer(page, session):
            session._delta_installed = True
    except Exception as exc:
        logger.debug("delta observer install failed: %s", exc)

    return True


# ---------------------------------------------------------------------------
# DOMCaptureSession
# ---------------------------------------------------------------------------

class DOMCaptureSession:
    """Reusable single-shot capture helper for remote browser sessions.

    Usage::

        session = DOMCaptureSession(page, websocket=ws)
        await session.send_page()       # full SingleFile capture -> ship
        # ...user clicks, types, navigates...
        await session.send_page()       # capture again (explicit)
        await session.shutdown()

    Sync strategy: full documents are the fidelity/recovery floor, while the
    installed MutationObserver ships safe changes as in-place ``dom_patch``
    frames.  The caller still triggers full captures for initial load,
    navigation, overflow, and explicit recovery; ordinary interactions do
    not rebuild the client iframe when the delta generation is healthy.
    """

    CAPTURE_STRATEGY: str = "single_capture"

    @classmethod
    def for_url(cls, page: Any, url: str, websocket: Any = None, client_id: Optional[str] = None) -> "DOMCaptureSession":
        return cls(page, websocket=websocket, client_id=client_id)

    def __init__(self, page: Any, websocket: Any = None, client_id: Optional[str] = None) -> None:
        self.page = page
        self.websocket = websocket
        self.client_id = client_id
        self.capture_strategy: str = self.CAPTURE_STRATEGY

        self.last_sent_url: Optional[str] = None
        self.last_sent_html: Optional[str] = None
        self.has_initial_capture: bool = False
        self._stopped = False
        # URL watch task slot (kept here so session.py's getattr check is happy).
        self.url_watch_task: Optional[asyncio.Task] = None
        # Monotonic timestamp of the last interaction-driven recapture,
        # used purely for flood control.  The actual decision about
        # whether something is interactive is made in-page by
        # _INTERACTION_TRIGGER_JS; this number just prevents an
        # automated click-storm from melting the capture pipeline.
        self._last_interaction_capture_t: float = 0.0
        # The JS listeners/init scripts and exposed bindings are page-object
        # scoped and survive ordinary navigations.  Install each once; repeat
        # only the immediate evaluate for the new document.
        self._interaction_trigger_installed: bool = False
        self._interaction_binding_installed: bool = False
        self._interaction_init_script_installed: bool = False
        self._delta_binding_installed: bool = False
        self._delta_init_script_installed: bool = False
        # Whether the live-delta observer delivered at least install
        # successfully — typing recaptures are redundant while patches
        # ('v' value ops) carry the keystrokes to the client live.
        self._delta_installed: bool = False  # observer/binding is ready
        self._delta_active: bool = False     # current client generation has data-mid
        self._explicit_navigation_in_progress: bool = False
        self._capture_install_lock = asyncio.Lock()
        # ---- fast-pipeline state (MIGRATION_LIVE_MIRROR.md) ----
        # Coalesce: at most one capture in flight; overlaps buffer the
        # latest reason and produce exactly one follow-up send.
        self._send_inflight: bool = False
        # Keep a delta frame and a full-document frame ordered on the same
        # websocket.  Without this, an already-validated patch task can yield
        # just as a recovery capture starts and arrive after its newer gen.
        self._ws_send_lock = asyncio.Lock()
        # Navigation controls and the URL watcher can observe the same change
        # (especially on SB/history.pushState). Serialize their recovery send
        # so the mirror never races two full documents for one URL.
        self._navigation_capture_lock = asyncio.Lock()
        self._pending_reason: Optional[str] = None
        self._overlap_count: int = 0
        # Generation counter — bumped on every full_document send; delta
        # patches carry the generation they apply to.
        self._gen: int = 0
        # Last DOM checksum that produced a send (unchanged-DOM skip).
        self._last_checksum: Optional[str] = None
        # Kill-switch once httpx turns out to be unavailable.
        self._assets_ok: bool = True
        # A full document can mention the same immutable /assets digests on
        # every recovery.  Warm each digest at most once per client session so
        # asset metadata and browser fetch work do not ride along on every
        # resync.
        self._sent_asset_digests: set = set()
        # Consecutive full-capture failures — surfaced as an ERROR once
        # (a client receiving no DOM at all must never be silent).
        self._capture_fail_streak: int = 0
        # Whether the most recent capture came from SingleFile (fast
        # degenerate-fallback) — asset rewrite must not touch those.
        self._last_capture_was_singlefile: bool = False
        # Whether the last capture can carry delta patches (has data-mid
        # stamps) and whether asset rewriting applies to it.  fast=True/True;
        # SingleFile=False/False; outerHTML tier3=False/True.
        # Snapshot mode (default for non-live hosts) still keeps full
        # documents as the fidelity/recovery floor, but the delta observer
        # can patch safe mutations between those documents.  Set per-site by
        # the owning session via prefer_snapshot_for_url().
        self.snapshot_only: bool = False
        self._last_capture_supports_delta: bool = True
        self._last_capture_supports_assets: bool = True
        # Best-effort auto-install on construction: only succeeds if
        # the page object is usable right now.  Failures are silent --
        # call enable_interaction_capture() later, or it will be
        # re-attempted on each navigation / first send_page.
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running() and page is not None:
                # Cannot await from __init__; schedule for next tick.
                loop.create_task(self.enable_interaction_capture())
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def bind_page(self, page: Any) -> None:
        """Move the helper to a different tab/page object.

        A session can switch tabs while reusing one DOMCaptureSession.  The
        exposed bindings and init-script registrations are page-scoped, so
        carrying the old "installed" flags to the new page would silently
        disable interaction/delta delivery there.  Reset only page-local
        state; keep the generation counter monotonic for the client.
        """
        if page is self.page:
            return
        self.page = page
        self._interaction_trigger_installed = False
        self._interaction_binding_installed = False
        self._interaction_init_script_installed = False
        self._delta_binding_installed = False
        self._delta_init_script_installed = False
        self._delta_installed = False
        self._delta_active = False
        self._explicit_navigation_in_progress = False
        self._last_checksum = None
        self.last_sent_url = None
        self.last_sent_html = None
        self._sent_asset_digests.clear()

    async def start(self) -> None:
        """Install the generic interaction trigger.  Idempotent; safe
        to call multiple times.  Callers that previously relied on
        ``start()`` being a no-op will still get a working session;
        we only added the trigger installation, no background polling
        loop."""
        await self.enable_interaction_capture()
        return

    async def enable_interaction_capture(self) -> bool:
        """Install (or re-install) the generic interaction trigger on
        the current page.  Safe to call after navigation: the JS
        guard ensures at most one delegated listener per page, and
        the Python-side ``expose_function`` is also idempotent on
        the Playwright side."""
        if self.page is None:
            return False
        if self._stopped:
            return False
        async with self._capture_install_lock:
            if self._stopped:
                return False
            ok = await _install_interaction_trigger(self.page, self)
            if ok:
                self._interaction_trigger_installed = True
            return ok

    async def shutdown(self) -> None:
        """Mark the session stopped and cancel the URL watcher if any."""
        self._stopped = True
        if self.url_watch_task and not self.url_watch_task.done():
            self.url_watch_task.cancel()
            try:
                await self.url_watch_task
            except (asyncio.CancelledError, Exception):
                pass
        self.url_watch_task = None
        try:
            await get_global_asset_manager().release_session(self.client_id)
        except Exception:
            logger.debug("asset lease release failed for %s", self.client_id, exc_info=True)

    async def _capture_navigation_once(self, reason: str) -> Optional[str]:
        """Serialize one navigation recovery send at a time.

        The delta observer emits a control batch for a new document, while
        the URL watcher also sees same-document history changes. Both are
        needed: the former catches reloads that keep the same URL, and the
        latter catches SPA ``pushState``/``replaceState`` changes. A lock
        keeps the two paths from racing stale full documents.
        """
        async with self._navigation_capture_lock:
            try:
                current_url = self.page.url if self.page is not None else None
            except Exception:
                current_url = None
            # The adapter's cached URL can briefly lag the document event. Ask
            # the live document once on navigation so the full frame is labeled
            # with the page that was actually captured, not the previous URL.
            if self.page is not None:
                try:
                    live_url = await self.page.evaluate("location.href")
                    if isinstance(live_url, str) and live_url:
                        current_url = live_url
                except Exception:
                    pass
            if current_url:
                # The URL watcher is a fallback. If the observer/explicit
                # navigation already sent this URL while it waited for the
                # lock, do not rebuild the mirror a second time.
                if reason == "url_change" and current_url == self.last_sent_url:
                    return current_url
            return await self.send_page(reason=reason, _url_override=current_url)

    async def watch_url(self, interval_s: float = URL_WATCH_INTERVAL_S) -> None:
        """Watch the page URL as a recovery fallback.

        The persistent delta init script emits a navigation control for full
        document loads, including same-URL reloads. This loop also remains a
        fallback for same-document SPA history changes and missed controls;
        duplicate sends are serialized and suppressed by the navigation lock.
        """
        last_url: Optional[str] = None
        try:
            if self.page is not None:
                last_url = self.page.url
        except Exception:
            last_url = None
        while not self._stopped:
            try:
                current_url: Optional[str] = None
                if self.page is not None:
                    try:
                        current_url = self.page.url
                    except Exception:
                        current_url = None
                if current_url and last_url and current_url != last_url:
                    self._delta_active = False
                    logger.debug("URL changed %s -> %s", last_url, current_url)
                    # With persistent init scripts, the new document's
                    # navigation control owns the recovery full capture.  Do
                    # not add a second stability wait or duplicate serializer
                    # here; this watcher remains the fallback for pages where
                    # delta installation failed.
                    if self._delta_installed:
                        # A healthy observer owns normal full-document
                        # navigations, but it cannot observe SPA
                        # history.pushState/replaceState. Keep the watcher as
                        # a cheap URL fallback instead of blindly skipping the
                        # change; _capture_navigation_once deduplicates a
                        # navigation control that arrived first.
                        try:
                            await self._capture_navigation_once("url_change")
                        except Exception as exc:
                            logger.debug("URL-watch delta-path fallback failed: %s", exc)
                        last_url = current_url
                        await asyncio.sleep(interval_s)
                        continue
                    # Re-install the interaction trigger on the new
                    # document when the delta path is unavailable.  Best-effort:
                    # the full-capture fallback below remains authoritative.
                    # Both init scripts persist on this page object across
                    # navigations.  Avoid two immediate evaluate round trips
                    # on every URL change when the registrations are already
                    # present; the new document ran them at parse start.
                    if (not self._interaction_init_script_installed
                            or (LIVE_DELTA and not self._delta_init_script_installed)):
                        try:
                            await self.enable_interaction_capture()
                        except Exception as exc:
                            logger.debug("re-install interaction trigger after URL change: %s", exc)
                    # Wait for the new page to settle -- the same
                    # load/networkidle/readyState sequence the
                    # initial capture uses.  We deliberately do NOT
                    # skip ahead and capture during this wait;
                    # capturing a parsing document produces a
                    # half-rendered snapshot that the client then
                    # has to manually re-request.
                    try:
                        await _ensure_page_stable(self.page)
                    except Exception as exc:
                        logger.debug("post-URL-change page-stability wait failed: %s", exc)
                    # A navigation control batch or an explicit goto may
                    # already have sent this exact URL while the watcher was
                    # waiting for stability.  Do not rebuild the iframe twice.
                    if current_url != self.last_sent_url:
                        try:
                            await self._capture_navigation_once("url_change")
                        except Exception as exc:
                            logger.debug("URL-watch full capture failed: %s", exc)
                    else:
                        logger.debug("URL-watch resync already sent for %s", current_url)
                last_url = current_url or last_url
            except Exception as exc:
                logger.debug("URL watch loop error: %s", exc)
            try:
                await asyncio.sleep(interval_s)
            except asyncio.CancelledError:
                return

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    def request_capture(self, reason: Optional[str] = None) -> None:  # noqa: ARG002
        """No-op kept for API compatibility.  Call ``send_page()`` to
        actually capture."""
        return

    # Backwards-compatible alias.
    request_sync = request_capture

    async def capture_page(self, settle: Optional[str] = None) -> Optional[str]:
        """Capture the current page.  Returns the HTML string, or None.

        ``settle``:
          none   — no waits at all (interaction recapture of a live page)
          light  — readyState-complete only (default; never networkidle)
          full   — legacy load + networkidle + readyState sequence
        ``DOM_CAPTURE_FULL_SETTLE=1`` forces ``full`` everywhere (rollback).
        """
        if self.page is None:
            return None
        if settle is None:
            settle = "light"
        if FULL_SETTLE:
            settle = "full"
        try:
            if settle != "none":
                try:
                    await self.page.wait_for_load_state("domcontentloaded", timeout=10000)
                except Exception:
                    pass
                if settle == "full":
                    await _ensure_page_stable(self.page)
                else:
                    try:
                        await self.page.wait_for_function("document.readyState === 'complete'", timeout=1500)
                    except Exception:
                        pass
                    if PRE_CAPTURE_WAIT > 0:
                        await asyncio.sleep(PRE_CAPTURE_WAIT)
            self._last_capture_was_singlefile = False
            self._last_capture_supports_delta = True
            self._last_capture_supports_assets = True
            if DOM_CAPTURE_MODE == "singlefile":
                self._last_capture_was_singlefile = True
                self._last_capture_supports_delta = False
                self._last_capture_supports_assets = False
                return await _capture_with_single_file(self.page)
            html = await _capture_fast(self.page)
            if html is not None and len(html) < 400 and "<html" not in html.lower():
                html = None  # degenerate output — fall back
            fast_err = _LAST_FAST_CAPTURE_ERROR
            if html is None:
                # Tier 2: SingleFile (requires the MV3 extension — absent on
                # SeleniumBase UC Chrome, so this is a no-op there).
                logger.debug("dc fast capture degenerate/unavailable (%s) — SingleFile fallback", fast_err)
                html = await _capture_with_single_file(self.page)
                if html is not None:
                    self._last_capture_was_singlefile = True
                    self._last_capture_supports_delta = False
                    self._last_capture_supports_assets = False
                    return html
            if html is None:
                # Tier 3: outerHTML — always works if evaluate works at all.
                # No data-mid (delta off) but asset rewrite applies so CSS
                # still lands inline.
                try:
                    html = await self.page.evaluate("document.documentElement.outerHTML")
                    if isinstance(html, str) and html:
                        html = "<!DOCTYPE html>\n" + html
                        self._last_capture_supports_delta = False
                        self._last_capture_supports_assets = True
                        return html
                    html = None
                except Exception as exc:
                    logger.debug("dc outerHTML fallback failed: %s", exc)
                logger.error(
                    "[CAPTURE] all capture tiers failed: fast=%s; singlefile=extension-unavailable/failed; "
                    "outerHTML=failed — check the page/backend (SB evaluate path)",
                    fast_err or "unknown")
                return None
            return html
        except Exception as exc:
            logger.exception("page capture failed: %s", exc)
            return None

    async def _dom_checksum(self) -> Optional[str]:
        """Cheap O(1)-ish page fingerprint for the unchanged-DOM skip."""
        if self.page is None:
            return None
        try:
            v = await self.page.evaluate(_DOM_CHECKSUM_JS)
            return v if isinstance(v, str) and v else None
        except Exception:
            return None

    async def capture_page_delta(self) -> Optional[dict]:
        """Legacy delta-capture entry point.  No-op in single-capture mode."""
        return None

    async def send_page(self, reason: Optional[str] = None, force: bool = False,
                        _url_override: Optional[str] = None) -> Optional[str]:  # noqa: ARG002
        """Run a full SingleFile capture and ship it to the websocket.
        Returns the URL on success, None on failure.

        ``_url_override`` is used only by the navigation recovery path when a
        backend's cached page URL may lag the live document by one CDP event.
        """
        if self.page is None:
            return None
        # Self-heal: if the trigger isn't installed yet (e.g. session
        # was constructed without a running event loop, or the page
        # navigated without going through handle_navigation), install
        # it now.  This keeps the interaction-capture guarantee
        # without forcing every caller to remember to call
        # enable_interaction_capture().
        if (not self._interaction_trigger_installed
                or (LIVE_DELTA and not self._delta_installed)):
            try:
                await self.enable_interaction_capture()
            except Exception:
                pass
        if _url_override is not None:
            url = _url_override
        else:
            try:
                url = self.page.url
            except Exception:
                url = None
        return await self._send_full(url, reason=reason or "manual")

    # ------------------------------------------------------------------
    # Senders
    # ------------------------------------------------------------------

    async def _send_full(self, url: Optional[str], *, reason: str = "desync") -> Optional[str]:
        """Capture and send the page.

        Coalesced (SEND_COALESCE): if a capture is already in flight the
        latest reason is buffered and exactly one follow-up is scheduled
        after the current send — captures never queue or overlap.
        Unchanged pages (checksum-equal, interaction reasons only) are
        skipped before paying for a capture.
        """
        if self.websocket is None:
            return None
        if SEND_COALESCE and self._send_inflight:
            self._pending_reason = reason
            self._overlap_count += 1
            logger.debug("dc coalesce: buffered reason=%s (capture in flight)", reason)
            return None
        self._send_inflight = True
        t_start = time.perf_counter()
        try:
            if url is None:
                try:
                    url = self.page.url
                except Exception:
                    url = None
            settle = _settle_for_reason(reason)

            # ---- unchanged-DOM skip (only for hot interaction reasons) ----
            checksum: Optional[str] = None
            if (SKIP_UNCHANGED and settle == "none"
                    and self._last_checksum is not None
                    and not str(reason).lower().startswith((
                        "delta_overflow", "navigation", "coalesce:delta_overflow",
                        "coalesce:navigation", "resync"
                    ))):
                checksum = await self._dom_checksum()
                if checksum and checksum == self._last_checksum:
                    logger.debug("dc skip(unchanged) reason=%s gen=%s", reason, self._gen)
                    return url

            t_cap = time.perf_counter()
            html_data = await self.capture_page(settle=settle)
            capture_ms = (time.perf_counter() - t_cap) * 1000.0
            if not html_data:
                self._capture_fail_streak += 1
                if self._capture_fail_streak >= 2:
                    logger.error(
                        "[CAPTURE] %d consecutive capture failures (client %s, last reason=%s, "
                        "url=%s) — client receives NO DOM updates; check page/backend health",
                        self._capture_fail_streak, self.client_id, reason, url)
                return None
            html_data = _strip_dead_subframes(html_data, url)
            if url:
                html_data = _inject_base_href(html_data, url)

            # ---- asset-cache rewrite (fast captures only) ----
            assets_meta: List[Dict[str, Any]] = []
            rewrite_ms = 0.0
            supports_assets = self._last_capture_supports_assets
            if supports_assets and url and self._assets_ok:
                t_rw = time.perf_counter()
                try:
                    if DOM_CAPTURE_STRIP_SCRIPTS:
                        html_data = _strip_script_tags(html_data)
                    html_data, assets_meta = await _rewrite_assets_to_cache(
                        html_data, url, self.page, owner_id=self.client_id
                    )
                except ImportError:
                    self._assets_ok = False
                except Exception as exc:
                    logger.debug("asset rewrite failed (serving originals): %s", exc)
                rewrite_ms = (time.perf_counter() - t_rw) * 1000.0

            # ---- preserve site fonts: strip any forced Montserrat or mirror overrides ----
            html_data = _strip_mirror_font(html_data)

            # A SingleFile/outerHTML fallback has no data-mid identity map.
            # Stop relaying patches until a later fast capture restores a
            # patch-capable generation; otherwise the client would receive
            # deltas it can never apply and loop through resyncs.
            capture_delta_active = bool(
                LIVE_DELTA and self._delta_installed and self._last_capture_supports_delta
            )

            # Snapshot mode: skip byte-identical re-sends.  Hybrid delta
            # patches handle the immediate mutation path; this avoids
            # repeatedly parsing/switching an identical full document while
            # retaining full captures for recovery and navigation.
            _rs = str(reason)
            if (self.snapshot_only and url == self.last_sent_url
                    and html_data == self.last_sent_html
                    and not _rs.startswith((
                        "resync", "mirror_err", "recover", "delta_overflow",
                        "navigation", "coalesce:delta_overflow", "coalesce:navigation",
                    ))):
                logger.debug("dc skip(identical snapshot) reason=%s", reason)
                return url

            self._gen += 1
            msg: Dict[str, Any] = {
                "type": "full_document",
                "reason": reason,
                "url": url,
                "html": html_data,
                "gen": self._gen,
            }
            try:
                if self.page is not None:
                    _cur_title = await self.page.title()
                    if _cur_title:
                        msg["title"] = _cur_title
            except Exception:
                pass
            if capture_delta_active:
                # Snapshot-preferred hosts use the same patch channel between
                # full fidelity/recovery documents; the client keeps the
                # generation anchor and can still request a full resync.
                msg["delta"] = True
            assets_to_send = [
                meta for meta in assets_meta
                if isinstance(meta, dict)
                and meta.get("hash") not in self._sent_asset_digests
            ]
            if assets_to_send:
                msg["assets"] = assets_to_send
            t_send = time.perf_counter()
            try:
                frame = json.dumps(msg, ensure_ascii=False)
                async with self._ws_send_lock:
                    await self.websocket.send_text(frame)
            except Exception as exc:
                logger.debug("full_document send failed: %s", exc)
                return None
            send_ms = (time.perf_counter() - t_send) * 1000.0

            # A patch-capable generation no longer launches interaction full
            # captures, so there is no hot-path unchanged check to seed.  Do
            # not add a second browser round trip after every initial/nav
            # document merely to compute a checksum that will not be used.
            if checksum is None and not capture_delta_active:
                checksum = await self._dom_checksum()
            self._last_checksum = checksum or self._last_checksum
            self._capture_fail_streak = 0
            self._delta_active = capture_delta_active
            for meta in assets_to_send:
                digest = meta.get("hash")
                if digest:
                    self._sent_asset_digests.add(digest)
            self.last_sent_html = html_data
            self.last_sent_url = url
            self.has_initial_capture = True
            logger.debug(
                "dc send reason=%s settle=%s capture_ms=%.0f rewrite_ms=%.0f send_ms=%.0f "
                "html_bytes=%d assets=%d gen=%d overlaps=%d total_ms=%.0f",
                reason, settle, capture_ms, rewrite_ms, send_ms, len(html_data),
                len(assets_to_send), self._gen, self._overlap_count,
                (time.perf_counter() - t_start) * 1000.0,
            )
            return url
        finally:
            self._send_inflight = False
            pending = self._pending_reason
            self._pending_reason = None
            if SEND_COALESCE and pending and not self._stopped:
                # Recompute URL/task state inside the follow-up.
                asyncio.create_task(self._send_full(None, reason=f"coalesce:{pending}"))

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    async def handle_click(self, selector: Optional[str] = None, mid: Optional[str] = None) -> bool:
        if self.page is None:
            return False
        try:
            # ELEMENT-PURE relay (no coordinate mapping):
            #   1. data-mid — stamped by the capture serializer, survives DOM
            #      churn between mirror frames better than a CSS path.
            #   2. CSS selector built client-side from the resolved element.
            # Runtime "click" candidates are tried with a short actionability
            # budget (custom-element buttons like Apple's ui-button can stall
            # Playwright's waits). The SB adapter stays on its real pointer
            # path; only the legacy Playwright adapter uses the DOM fallback.
            candidates = []
            if mid is not None:
                safe_mid = re.sub(r'[^0-9A-Za-z_\-]', '', str(mid))
                if safe_mid:
                    candidates.append(f'[data-mid="{safe_mid}"]')
            if selector:
                candidates.append(selector)
            if not candidates:
                return False

            # FAST-PATH: Direct CDP evaluation click in renderer (sub-3ms execution)
            for sel in candidates:
                try:
                    fast_clicked = await self.page.evaluate(
                        """(sel) => {
                            try {
                                const el = document.querySelector(sel);
                                if (!el) return false;
                                const rect = el.getBoundingClientRect();
                                const cx = rect.left + rect.width / 2;
                                const cy = rect.top + rect.height / 2;
                                const opts = { bubbles: true, cancelable: true, view: window, clientX: cx, clientY: cy, button: 0 };
                                el.dispatchEvent(new PointerEvent('pointerdown', opts));
                                el.dispatchEvent(new MouseEvent('mousedown', opts));
                                el.dispatchEvent(new PointerEvent('pointerup', opts));
                                el.dispatchEvent(new MouseEvent('mouseup', opts));
                                if (typeof el.click === 'function') el.click();
                                return true;
                            } catch (e) { return false; }
                        }""",
                        sel,
                    )
                    if fast_clicked:
                        logger.debug("handle_click: fast-path succeeded for sel=%s mid=%s", sel, mid)
                        return True
                except Exception:
                    pass

            for sel in candidates:
                try:
                    await self.page.click(sel, timeout=1500)
                    logger.debug(
                        "handle_click: dispatched selector=%s mid=%s backend=%s",
                        sel, mid, getattr(self.page, "_backend_name", type(self.page).__name__),
                    )
                    return True
                except Exception as exc:
                    logger.debug(
                        "handle_click: candidate=%s failed (%s): %s",
                        sel, type(exc).__name__, exc,
                    )
                    continue
            # The SeleniumBase adapter intentionally uses real CDP pointer
            # events in page.click(). Do not fall back to element.click() on
            # that backend: it bypasses pointerdown/mousedown handlers and
            # was the reason SB behaved differently from Playwright.
            if getattr(self.page, "_is_sb_backend", False):
                logger.debug(
                    "handle_click: SB actionability failed (mid=%s selector=%s)",
                    mid, selector,
                )
                return False
            for sel in candidates:
                try:
                    clicked = await self.page.evaluate(
                        "(sel) => { const el = document.querySelector(sel);"
                        " if (el) { el.click(); return true; } return false; }", sel)
                    if clicked:
                        return True
                except Exception:
                    continue
            logger.debug("handle_click: element not resolvable (mid=%s selector=%s)", mid, selector)
            return False
        except Exception as exc:
            logger.debug("handle_click failed: %s", exc)
            return False

    async def handle_navigation(self, url: str) -> None:
        if self.page is None:
            return
        self._delta_active = False
        self._explicit_navigation_in_progress = True
        try:
            await self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
            # Fresh document -> the JS listener from the previous
            # page is gone.  Re-install the generic interaction
            # trigger on the new document.
            if (not self._interaction_init_script_installed
                    or (LIVE_DELTA and not self._delta_init_script_installed)):
                try:
                    await self.enable_interaction_capture()
                except Exception as exc:
                    logger.debug("re-install interaction trigger after nav: %s", exc)
            # IMPORTANT: do NOT fire _send_full immediately.  A page
            # that just changed URL is in an unstable state: the
            # document is parsing, subresources are still in flight,
            # client-side frameworks are mounting, SPAs are still
            # hydrating.  Capturing right now would snapshot a
            # half-rendered DOM.  Wait for the page to settle the
            # same way the initial capture does, then capture.
            try:
                await _ensure_page_stable(self.page)
            except Exception as exc:
                logger.debug("post-nav page-stability wait failed: %s", exc)
            # Use the live page URL after redirects rather than the requested
            # URL argument; otherwise the client can remain labeled with the
            # pre-redirect page even though the DOM is new.
            await self._capture_navigation_once("url_change")
        except Exception as exc:
            logger.debug("handle_navigation failed: %s", exc)
        finally:
            self._explicit_navigation_in_progress = False

    async def handle_keypress(self, key: str, selector: Optional[str] = None) -> None:
        if self.page is None:
            return
        try:
            if selector:
                try:
                    await self.page.focus(selector)
                except Exception:
                    pass
            if key and len(key) == 1:
                await self.page.keyboard.type(key, delay=0)
            elif key:
                key_map = {
                    "Enter": "Enter", "Backspace": "Backspace", "Delete": "Delete",
                    "Tab": "Tab", "Escape": "Escape",
                    "ArrowUp": "ArrowUp", "ArrowDown": "ArrowDown",
                    "ArrowLeft": "ArrowLeft", "ArrowRight": "ArrowRight",
                }
                if key in key_map:
                    await self.page.keyboard.press(key_map[key])
        except Exception as exc:
            logger.debug("handle_keypress failed: %s", exc)

    async def handle_submit_form(self, selector: str, form_data: Dict[str, Any]) -> None:
        if self.page is None:
            return
        try:
            for field_name, field_value in form_data.items():
                try:
                    await self.page.fill(f'{selector} [name="{field_name}"]', str(field_value))
                except Exception:
                    pass
            try:
                await self.page.eval_on_selector(selector, "form => form.submit()")
            except Exception:
                pass
        except Exception as exc:
            logger.debug("handle_submit_form failed: %s", exc)


__all__ = [
    "DOMCaptureSession",
    "_inject_base_href",
    "_capture_with_single_file",
    "_capture_via_extension",
    "_capture_via_library_injection",
    "_is_extension_capture_enabled",
    "_load_singlefile_sources",
    "_get_page_tab_id",
    "_install_interaction_trigger",
    "_INTERACTION_TRIGGER_JS",
    # fast-capture pipeline (MIGRATION_LIVE_MIRROR.md)
    "AssetEntry",
    "AssetManager",
    "get_global_asset_manager",
    "_guess_ext_from_content_type",
    "_capture_fast",
    "_rewrite_assets_to_cache",
    "_settle_for_reason",
    "_install_delta_observer",
    "_FAST_SERIALIZE_JS",
    "_DELTA_OBSERVER_JS",
    "_DOM_CHECKSUM_JS",
    "capture_page_mhtml",
]
