"""Persistent browser-profile importer and launcher.

This is intentionally a small, local profile manager rather than a one-file
``cookies.json`` loader.  A profile export can contain a complete Chromium
user-data directory, a cookies.json file, or both.  Every imported profile is
kept under PROFILE_BASE_PATH and can be selected again on a later run.

Usage::

    python cookies.py

The menu lets you import more ZIPs, select any saved profile, and launch it at
Google or another URL.  SeleniumBase is imported only when a browser is
launched, so archive management remains usable in environments without a
browser installation.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlparse


BASE_DIR = Path(__file__).resolve().parent
PROFILE_BASE_PATH = Path(
    os.environ.get("PROFILE_BASE_PATH", str(BASE_DIR / "profiles"))
).expanduser()
if not PROFILE_BASE_PATH.is_absolute():
    PROFILE_BASE_PATH = (BASE_DIR / PROFILE_BASE_PATH).resolve()

MAX_ZIP_BYTES = 4 * 1024 * 1024 * 1024
MAX_UNPACKED_BYTES = 8 * 1024 * 1024 * 1024
MAX_MEMBERS = 100_000
GENERIC_WRAPPERS = {"profiles", "browser_profiles", "browser-profiles"}


class ProfileError(RuntimeError):
    """An expected profile import or launch error."""


def _safe_profile_name(value: str) -> str:
    """Validate one directory name and reject path traversal."""
    name = str(value or "").strip()
    if (
        not name
        or name in {".", ".."}
        or name.startswith(".")
        or "/" in name
        or "\\" in name
        or len(name) > 120
    ):
        raise ProfileError("Profile names must be a single non-hidden directory name")
    return name


def _safe_archive_parts(member_name: str) -> List[str]:
    """Return safe path components for a ZIP member."""
    normalized = member_name.replace("\\", "/")
    parts = [part for part in normalized.split("/") if part]
    if (
        not parts
        or normalized.startswith("/")
        or ":" in parts[0]
        or any(part in {".", ".."} for part in parts)
    ):
        raise ProfileError(f"Unsafe ZIP path: {member_name!r}")
    return parts


def _is_zip_symlink(info: zipfile.ZipInfo) -> bool:
    mode = (info.external_attr >> 16) & 0o170000
    return stat.S_ISLNK(mode)


def _extract_zip_safely(zip_path: Path, destination: Path) -> int:
    """Extract a profile ZIP without zip-slip, symlink, or bomb hazards."""
    if zip_path.stat().st_size > MAX_ZIP_BYTES:
        raise ProfileError("The profile ZIP is too large")
    destination.mkdir(parents=True, exist_ok=False)
    unpacked = 0
    with zipfile.ZipFile(zip_path, "r") as archive:
        members = archive.infolist()
        if len(members) > MAX_MEMBERS:
            raise ProfileError("The profile ZIP contains too many files")
        for info in members:
            parts = _safe_archive_parts(info.filename)
            if _is_zip_symlink(info):
                raise ProfileError("Symlinks are not allowed in profile ZIPs")
            unpacked += int(info.file_size or 0)
            if unpacked > MAX_UNPACKED_BYTES:
                raise ProfileError("The unpacked profile is too large")
            target = (destination.joinpath(*parts)).resolve()
            try:
                target.relative_to(destination.resolve())
            except ValueError as exc:
                raise ProfileError("The profile ZIP attempts to leave its staging directory") from exc
            if info.is_dir() or info.filename.replace("\\", "/").endswith("/"):
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info, "r") as source, target.open("xb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
    return unpacked


_PROFILE_ROOT_MARKERS = {
    "Default",
    "Local State",
    "Preferences",
    "Cookies",
    "Network",
    "Local Storage",
    "Session Storage",
    "Extensions",
    "History",
    "Login Data",
}


def _looks_like_browser_profile(path: Path) -> bool:
    """Recognise a Chromium user-data root without requiring cookies.json."""
    return any((path / marker).exists() for marker in _PROFILE_ROOT_MARKERS)


def _choose_source_root(extracted: Path, requested_name: str = "") -> Tuple[Path, str]:
    """Find the complete browser user-data directory inside an export.

    File Manager downloads use ``profiles/<name>/...`` while other tools may
    wrap a profile in an arbitrary export directory. This removes wrappers,
    but never removes the profile's own ``Default`` directory or treats a
    multi-profile store as one profile.
    """
    children = list(extracted.iterdir())
    if len(children) != 1 or not children[0].is_dir():
        return extracted, ""

    wrapper = children[0]
    nested_dirs = [child for child in wrapper.iterdir() if child.is_dir() and not child.name.startswith(".")]
    wrapper_files = [child for child in wrapper.iterdir() if child.is_file()]

    if wrapper.name in GENERIC_WRAPPERS:
        if requested_name:
            requested_child = wrapper / requested_name
            if requested_child.is_dir():
                return requested_child, requested_name
        if len(nested_dirs) == 1 and not wrapper_files:
            return nested_dirs[0], nested_dirs[0].name
        if len(nested_dirs) > 1:
            raise ProfileError(
                "This ZIP contains multiple profiles; import one profile folder at a time"
            )
        return wrapper, ""

    # A named profile export is commonly <profile-name>/Default/..., and a
    # file-manager export may have an extra arbitrary wrapper around it.
    if requested_name and wrapper.name == requested_name:
        return wrapper, wrapper.name
    if _looks_like_browser_profile(wrapper):
        return wrapper, wrapper.name
    if requested_name:
        requested_child = wrapper / requested_name
        if requested_child.is_dir():
            return requested_child, requested_name
    profile_children = [child for child in nested_dirs if _looks_like_browser_profile(child)]
    if len(profile_children) == 1 and not wrapper_files:
        return profile_children[0], profile_children[0].name

    # Keep a single non-profile-looking directory intact rather than
    # guessing; it may still be a valid custom Chromium profile.
    return wrapper, wrapper.name


def _find_cookie_file(profile_path: Path) -> Optional[Path]:
    direct = profile_path / "cookies.json"
    if direct.is_file():
        return direct
    # Some exports put the cookie file in a single wrapper folder. Do not scan
    # the Chrome Cookies SQLite database; this is specifically the optional JSON
    # interchange format.
    matches = [candidate for candidate in profile_path.rglob("cookies.json") if candidate.is_file()]
    return matches[0] if len(matches) == 1 else None


def _has_native_cookie_store(profile_path: Path) -> bool:
    """Return whether the imported folder already has Chromium cookie state."""
    return any(
        (profile_path / relative).is_file()
        for relative in (
            Path("Default") / "Cookies",
            Path("Network") / "Cookies",
            Path("Cookies"),
        )
    )


def import_profile_zip(
    zip_path: Path | str,
    profile_name: str = "",
    replace: bool = False,
) -> Path:
    """Safely import a complete profile ZIP and return its persisted path."""
    source = Path(zip_path).expanduser().resolve()
    if not source.is_file() or not zipfile.is_zipfile(source):
        raise ProfileError("Choose a valid profile ZIP archive")

    requested_name = _safe_profile_name(profile_name) if profile_name.strip() else ""
    PROFILE_BASE_PATH.mkdir(parents=True, exist_ok=True)
    staging_parent = Path(tempfile.mkdtemp(prefix="profile-import-", dir=str(PROFILE_BASE_PATH)))
    extracted = staging_parent / "extracted"
    target: Optional[Path] = None
    try:
        _extract_zip_safely(source, extracted)
        source_root, inferred_name = _choose_source_root(extracted, requested_name)
        final_name = requested_name or (_safe_profile_name(inferred_name) if inferred_name else "")
        if not final_name:
            final_name = f"profile-{uuid.uuid4().hex[:10]}"
        target = (PROFILE_BASE_PATH / final_name).resolve()
        try:
            target.relative_to(PROFILE_BASE_PATH.resolve())
        except ValueError as exc:
            raise ProfileError("Invalid profile destination") from exc
        if target == PROFILE_BASE_PATH.resolve():
            raise ProfileError("Invalid profile destination")
        if target.exists() and not replace:
            raise ProfileError(f"Profile {final_name!r} already exists; choose another name or replace it")
        if target.exists() or target.is_symlink():
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink()
        shutil.move(str(source_root), str(target))

        cookie_file = _find_cookie_file(target)
        metadata = {
            "profile_name": final_name,
            "imported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "source": source.name,
            "profile_type": "chromium-user-data-directory",
            "persistent": True,
            "persistent_user_data_dir": str(target),
            "cookies_json": bool(cookie_file),
        }
        (target / ".profile-manager.json").write_text(
            json.dumps(metadata, indent=2) + "\n", encoding="utf-8"
        )
        return target
    finally:
        shutil.rmtree(staging_parent, ignore_errors=True)


def list_profiles() -> List[Path]:
    """Return all persisted named profile directories, in stable order."""
    PROFILE_BASE_PATH.mkdir(parents=True, exist_ok=True)
    return sorted(
        (
            path
            for path in PROFILE_BASE_PATH.iterdir()
            if path.is_dir() and not path.name.startswith(".")
        ),
        key=lambda path: path.name.casefold(),
    )


def _normalise_cookie_records(raw: Any) -> List[Dict[str, Any]]:
    """Accept common cookies.json exports without requiring one format."""
    if isinstance(raw, list):
        return [dict(item) for item in raw if isinstance(item, dict)]
    if not isinstance(raw, dict):
        return []
    if isinstance(raw.get("cookies"), list):
        return [dict(item) for item in raw["cookies"] if isinstance(item, dict)]
    records: List[Dict[str, Any]] = []
    for domain, value in raw.items():
        if not isinstance(value, dict) or not isinstance(value.get("cookies"), list):
            continue
        for item in value["cookies"]:
            if isinstance(item, dict):
                cookie = dict(item)
                cookie.setdefault("domain", domain)
                records.append(cookie)
    return records


def _cookie_domain(cookie: Dict[str, Any], fallback_url: str) -> str:
    domain = str(cookie.get("domain") or "").strip().lstrip(".")
    if domain:
        return domain
    parsed = urlparse(fallback_url)
    return parsed.hostname or "localhost"


def _load_cookies_json(driver: Any, cookie_file: Optional[Path], url: str) -> int:
    """Best-effort import of JSON cookies into the selected browser profile."""
    if not cookie_file:
        return 0
    try:
        raw = json.loads(cookie_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Warning: could not read {cookie_file.name}: {exc}")
        return 0

    records = _normalise_cookie_records(raw)
    by_domain: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for cookie in records:
        by_domain[_cookie_domain(cookie, url)].append(cookie)

    imported = 0
    for domain, domain_cookies in by_domain.items():
        try:
            driver.get("https://" + domain)
        except Exception as exc:
            print(f"Warning: could not open {domain}: {exc}")
            continue
        for source in domain_cookies:
            cookie = {
                key: source[key]
                for key in ("name", "value", "path", "domain", "secure", "httpOnly", "expiry", "sameSite")
                if key in source and source[key] is not None
            }
            if not cookie.get("name") or "value" not in cookie:
                continue
            # Selenium accepts only these SameSite spellings.
            if isinstance(cookie.get("sameSite"), str):
                same_site = cookie["sameSite"].capitalize()
                if same_site not in {"Strict", "Lax", "None"}:
                    cookie.pop("sameSite", None)
                else:
                    cookie["sameSite"] = same_site
            try:
                driver.add_cookie(cookie)
                imported += 1
            except Exception:
                # A domain/path mismatch should not prevent the other cookies
                # or the persisted browser profile from being used.
                continue
    try:
        driver.get(url)
    except Exception:
        pass
    return imported


class PrivateXvfb:
    """Own one headed Xvfb display for one Linux SeleniumBase browser.

    Windows and macOS already provide a native headed display, so they do not
    need (and should not try to start) Xvfb.
    """

    def __init__(self, width: int = 1440, height: int = 1000):
        self.width = width
        self.height = height
        self.display: Optional[str] = None
        self.process: Optional[subprocess.Popen] = None
        self.previous_display: Optional[str] = None
        self._manages_display = False

    @staticmethod
    def _needs_xvfb() -> bool:
        return sys.platform.startswith("linux")

    def start(self) -> None:
        if not self._needs_xvfb():
            # SeleniumBase will use the native headed desktop on Windows and
            # macOS. In particular, do not require an X server on Windows.
            return
        xvfb = shutil.which("Xvfb")
        if not xvfb:
            raise ProfileError("SeleniumBase needs Xvfb for this headed profile launcher")
        for number in range(90, 190):
            display = f":{number}"
            lock_file = Path(f"/tmp/.X{number}-lock")
            socket_file = Path(f"/tmp/.X11-unix/X{number}")
            if lock_file.exists() or socket_file.exists():
                continue
            process = subprocess.Popen(
                [
                    xvfb,
                    display,
                    "-screen",
                    "0",
                    f"{self.width}x{self.height}x24",
                    "-nolisten",
                    "tcp",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            time.sleep(0.25)
            if process.poll() is None:
                self.process = process
                self.display = display
                self.previous_display = os.environ.get("DISPLAY")
                os.environ["DISPLAY"] = display
                self._manages_display = True
                return
            process.stderr.close() if process.stderr else None
        raise ProfileError("Could not allocate a private Xvfb display")

    def stop(self) -> None:
        if not self._manages_display:
            return
        if self.previous_display is None:
            os.environ.pop("DISPLAY", None)
        else:
            os.environ["DISPLAY"] = self.previous_display
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
        if self.process and self.process.stderr:
            self.process.stderr.close()
        self.process = None
        self.display = None
        self.previous_display = None
        self._manages_display = False


def launch_profile(profile_path: Path, url: str = "https://www.google.com") -> None:
    """Launch a complete persisted profile at Google or another URL.

    ``profile_path`` is passed as SeleniumBase's Chromium ``user_data_dir``.
    That makes the imported folder itself the persistent browser context:
    Preferences, Local Storage, IndexedDB, history, extensions, native
    Cookies DB, session state, and every other profile file remain available.
    ``cookies.json`` is only an optional compatibility import for exports that
    do not contain Chromium's native Cookies database.
    """
    profile_path = Path(profile_path).expanduser().resolve()
    if not profile_path.is_dir():
        raise ProfileError("That profile no longer exists")
    if not url.strip():
        url = "https://www.google.com"
    if "://" not in url:
        url = "https://" + url

    try:
        from seleniumbase import Driver
    except ImportError as exc:
        raise ProfileError("SeleniumBase is not installed; install requirements.txt first") from exc

    display = PrivateXvfb()
    driver: Any = None
    try:
        display.start()
        # Do not create a fresh temporary context and do not reduce the
        # profile to cookies.json. SeleniumBase opens this exact directory as
        # Chrome's persistent user-data-dir, just like Neo Stream's persistent
        # browser context.
        driver = Driver(
            browser="chrome",
            uc=True,
            headless=False,
            user_data_dir=str(profile_path),
        )
        driver.get(url)
        cookie_file = _find_cookie_file(profile_path)
        imported = 0
        if cookie_file and not _has_native_cookie_store(profile_path):
            imported = _load_cookies_json(driver, cookie_file, url)
        elif cookie_file:
            print("Using the profile's native Chromium Cookies database; cookies.json remains preserved")
        if imported:
            print(f"Merged {imported} compatibility cookies into the persistent profile")
        print(f"Running persistent profile {profile_path.name} at {url}")
        print(f"All browser state is saved in: {profile_path}")
        print("Press Enter here to close the browser.")
        input()
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass
        display.stop()


def _choose_zip_path() -> Optional[Path]:
    value = input("Path to a profile ZIP (blank to cancel): ").strip()
    if not value:
        return None
    return Path(value).expanduser()


def _import_menu() -> None:
    source = _choose_zip_path()
    if not source:
        return
    default_name = source.stem
    requested = input(f"Profile name [{default_name}]: ").strip() or default_name
    try:
        target = import_profile_zip(source, requested, replace=False)
        print(f"Saved profile: {target}")
    except ProfileError as exc:
        print(f"Import failed: {exc}")


def _select_profile() -> Optional[Path]:
    profiles = list_profiles()
    if not profiles:
        print(f"No profiles are saved in {PROFILE_BASE_PATH}")
        return None
    print("\nSaved profiles:")
    for index, profile in enumerate(profiles, start=1):
        print(f"  {index}. {profile.name}")
    choice = input("Select a profile number (blank to cancel): ").strip()
    if not choice:
        return None
    try:
        selected = profiles[int(choice) - 1]
    except (ValueError, IndexError):
        print("Invalid profile selection")
        return None
    return selected


def main() -> None:
    print(f"Persistent profile manager: {PROFILE_BASE_PATH}")
    while True:
        print("\n1. Import another profile ZIP\n2. Launch a saved profile\n3. List saved profiles\n4. Exit")
        choice = input("> ").strip()
        if choice == "1":
            _import_menu()
        elif choice == "2":
            selected = _select_profile()
            if selected:
                url = input("URL [https://www.google.com]: ").strip() or "https://www.google.com"
                try:
                    launch_profile(selected, url)
                except ProfileError as exc:
                    print(f"Launch failed: {exc}")
        elif choice == "3":
            profiles = list_profiles()
            if profiles:
                for profile in profiles:
                    print(f"- {profile.name}")
            else:
                print("No saved profiles")
        elif choice == "4":
            return
        else:
            print("Choose 1, 2, 3, or 4")


if __name__ == "__main__":
    main()
