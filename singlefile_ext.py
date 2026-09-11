"""Shared SingleFile extension resolution + Chrome launch-arg application.

Two failure modes killed the SB/PCM capture path:

1. The SB (UC) browser simply never received ``--load-extension`` — the
   flag only existed on the Playwright/direct launch path.
2. Chrome 137+ ignores ``--load-extension`` on branded stable Chrome
   unless ``DisableLoadExtensionCommandLineSwitch`` is turned off.

This module resolves the extension directory once, applies the flags
(with correct ``--disable-features`` *merging* — Chrome only honors the of
LAST occurrence of that switch), and both launchers share it.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional

_EXT_SWITCH_FEATURE = "DisableLoadExtensionCommandLineSwitch"


def singlefile_ext_enabled() -> bool:
    return os.environ.get("SINGLEFILE_EXT_MODE", "1").strip() not in (
        "0", "false", "no", "off")


def _valid_ext_dir(cand: str) -> bool:
    """A dir counts as the extension only if manifest.json parses as JSON
    and declares manifest_version (guards against pointing at an outer
    folder of a double-nested zip extract)."""
    try:
        import json
        mf = Path(cand) / "manifest.json"
        if not mf.is_file():
            return False
        data = json.loads(mf.read_text(encoding="utf-8"))
        return bool((data or {}).get("manifest_version"))
    except Exception:
        return False


def resolve_extension_dir() -> Optional[str]:
    """Location of the bundled SingleFile MV3 extension — ALWAYS found
    relative to this module (never a hardcoded absolute path), tolerant of
    double-nested zip extracts and working-directory surprises."""
    cands: List[str] = []
    env_dir = os.environ.get("SINGLEFILE_EXT_DIR", "").strip()
    if env_dir:
        cands.append(env_dir)
    here = Path(__file__).resolve().parent
    cands.append(str(here / "single"))
    cands.append(str(here.parent / "single"))          # one level up
    cands.append(str(Path.cwd() / "single"))
    cands.append(str(Path.cwd().parent / "single"))
    cands.append(str(Path.home() / "shifixsxs" / "single"))
    cands.append("/home/user/shifixsxs/single")
    seen = set()
    for cand in cands:
        cand_abs = os.path.abspath(cand)
        if cand_abs in seen:
            continue
        seen.add(cand_abs)
        if os.path.isdir(cand_abs) and _valid_ext_dir(cand_abs):
            return cand_abs
    # Last resort: bounded scan for any nested 'single/manifest.json'
    # (double-nested zip extracts like 'repo (30)/repo/single').
    for root in (here, Path.cwd()):
        try:
            for mpath in list(root.glob("*/single/manifest.json")) +                         list(root.glob("*/*/single/manifest.json")):
                cand = str(mpath.parent)
                if cand not in seen and _valid_ext_dir(cand):
                    return cand
        except Exception:
            pass
    return None


def _merge_disable_features(args: List[str], *features: str) -> List[str]:
    """Chrome honors only the LAST --disable-features occurrence, so all
    values must be merged into ONE switch."""
    existing: List[str] = []
    out: List[str] = []
    for a in args:
        if a.startswith("--disable-features="):
            for part in a.split("=", 1)[1].split(","):
                part = part.strip()
                if part and part not in existing:
                    existing.append(part)
        else:
            out.append(a)
    for f in features:
        if f not in existing:
            existing.append(f)
    if existing:
        out.append("--disable-features=" + ",".join(existing))
    return out


def apply_singlefile_ext_args(args: List[str], log=None,
                              merge_features: bool = True) -> List[str]:
    """Return ``args`` with the SingleFile extension wired for loading.
    Strips conflicting switches first and merges disable-features.
    ``merge_features=False`` appends a single-value --disable-features flag
    instead (needed by SeleniumBase: it re-splits our arg string on
    commas, so a multi-feature merged flag would arrive corrupted)."""
    if not singlefile_ext_enabled():
        return args
    ext_dir = resolve_extension_dir()
    if not ext_dir:
        if log:
            log("No valid SingleFile extension dir found — extension NOT loaded")
        return args
    import logging
    logging.getLogger(__name__).info("SingleFile extension dir: %s", ext_dir)
    args = [a for a in args
            if a != "--disable-extensions"
            and not a.startswith("--load-extension=")
            and not a.startswith("--disable-extensions-except=")]
    args.append(f"--disable-extensions-except={ext_dir}")
    args.append(f"--load-extension={ext_dir}")
    # Chrome 137+ (branded stable) ignores --load-extension unless the
    # kill-switch feature is disabled.
    if merge_features:
        args = _merge_disable_features(args, _EXT_SWITCH_FEATURE)
    else:
        args.append("--disable-features=" + _EXT_SWITCH_FEATURE)
    if log:
        log(f"Loading SingleFile extension from {ext_dir}")
    return args
