#!/usr/bin/env python3
"""
SingleFile Python Wrapper
=========================
Captures a web page (e.g. outlook.com) as a single self-contained HTML file
by driving the `single-file` command-line tool from Python.

Prerequisites
-------------
    npm install -g single-file-cli
    # single-file-cli installs its own Chromium on first run.

Quick start
-----------
    # Public / unauthenticated page
    python archiver.py https://outlook.com

    # Capture with a longer wait (Outlook is a heavy SPA)
    python archiver.py https://outlook.office.com/mail --wait-time 10000 -o inbox.html

    # Authenticated capture using an exported cookies file
    python archiver.py https://outlook.office.com/mail ^
        --browser-cookies-file outlook-cookies.json --wait-time 10000 -o inbox.html
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional


def find_single_file() -> str:
    """Locate the `single-file` executable or exit with a helpful error."""
    path = shutil.which("single-file")
    if path is None:
        sys.stderr.write(
            "[ERROR] 'single-file' CLI not found on PATH.\n"
            "        Install it with:  npm install -g single-file-cli\n"
        )
        sys.exit(127)
    return path


def capture_page(
    url: str,
    output: str = "outlook.html",
    *,
    headless: bool = True,
    wait_delay_ms: int = 5000,
    load_max_time_ms: int = 60000,
    capture_max_time_ms: int = 60000,
    wait_until: str = "networkIdle",
    wait_until_fallback: bool = True,
    user_agent: Optional[str] = None,
    http_headers: Optional[List[str]] = None,
    cookies_file: Optional[str] = None,
    browser_executable_path: Optional[str] = None,
    output_directory: Optional[str] = None,
    filename_template: Optional[str] = None,
    user_script_enabled: bool = True,
    extra_args: Optional[List[str]] = None,
    timeout: int = 180,
) -> int:
    """
    Capture a single page using single-file-cli.

    All flag names here are taken from `single-file --help`.

    Parameters
    ----------
    url                   : Page to capture.
    output                : Destination HTML file.
    headless              : Run the browser in headless mode.
    wait_delay_ms         : ms to wait after the page is loaded before capturing.
    load_max_time_ms      : Max ms to wait for the page to load.
    capture_max_time_ms   : Max ms to wait for the capture to finish.
    wait_until            : networkIdle | networkAlmostIdle | load | domContentLoaded | InteractiveTime.
    wait_until_fallback   : Retry with a less strict wait condition on timeout.
    user_agent            : Override the User-Agent string.
    http_headers          : Extra HTTP headers, e.g. ["Accept-Language: en-US"].
    cookies_file          : Path to a JSON / Netscape cookies file for authenticated captures.
    browser_executable_path : Path to a specific browser binary.
    output_directory      : Directory where the output file is written.
    filename_template     : Filename template (see single-file --help).
    user_script_enabled   : Enable the SingleFile "world" / event API. Set to False to
                            work around "Execution context not found for SingleFile world"
                            errors on pages that redirect or fail to expose the world.
    extra_args            : Raw extra CLI args appended verbatim.
    timeout               : Max seconds for the whole subprocess.
    """
    binary = find_single_file()

    cmd: List[str] = [binary, url, output]
    cmd += ["--browser-headless", "true" if headless else "false"]
    cmd += ["--browser-wait-delay", str(wait_delay_ms)]
    cmd += ["--browser-load-max-time", str(load_max_time_ms)]
    cmd += ["--browser-capture-max-time", str(capture_max_time_ms)]
    cmd += ["--browser-wait-until", wait_until]
    cmd += ["--browser-wait-until-fallback", "true" if wait_until_fallback else "false"]

    if user_agent:
        cmd += ["--user-agent", user_agent]
    if cookies_file:
        cmd += ["--browser-cookies-file", cookies_file]
    if browser_executable_path:
        cmd += ["--browser-executable-path", browser_executable_path]
    if output_directory:
        cmd += ["--output-directory", output_directory]
    if filename_template:
        cmd += ["--filename-template", filename_template]
    cmd += ["--user-script-enabled", "true" if user_script_enabled else "false"]
    if http_headers:
        for h in http_headers:
            cmd += ["--http-header", h]
    if extra_args:
        cmd += list(extra_args)

    print("[SingleFile] Capturing page")
    print(f"  URL           : {url}")
    print(f"  Output        : {output}")
    print(f"  Headless      : {headless}")
    print(f"  Wait delay    : {wait_delay_ms} ms")
    print(f"  Wait until    : {wait_until}")
    print(f"  Load max time : {load_max_time_ms} ms")
    print(f"  Capture max   : {capture_max_time_ms} ms")
    print(f"  Command       : {' '.join(cmd)}\n")

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        sys.stderr.write(f"[ERROR] single-file timed out after {timeout}s.\n")
        return 124

    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)

    # single-file-cli sometimes exits 0 even when it printed an Error: line.
    # Treat any "Error:" in stderr as a failure. Also detect the specific
    # "Execution context not found for SingleFile world" message which means
    # the capture was aborted (and the output file is likely missing).
    stderr_text = result.stderr or ""
    stdout_text = result.stdout or ""
    combined = stderr_text + "\n" + stdout_text
    reported_error = (
        "Error:" in combined
        or "Execution context not found" in combined
    )
    ok = result.returncode == 0 and not reported_error

    if ok:
        if Path(output).exists():
            size = Path(output).stat().st_size
            print(f"\n[OK] Captured {size/1024:.1f} KB -> {output}")
        else:
            # File may be at output_directory / filename-template result
            print(f"\n[OK] single-file reported success (rc={result.returncode}).")
    else:
        print(
            f"\n[FAIL] single-file failed "
            f"(rc={result.returncode}, error_in_output={reported_error})."
        )
        if "Execution context not found for SingleFile world" in combined:
            print(
                "       Hint: re-run with --no-user-script to disable the\n"
                "             SingleFile 'world' event API. This usually\n"
                "             fixes capture on pages that redirect or use\n"
                "             strict CSP (e.g. outlook.com / live.com)."
            )

    return 0 if ok else (result.returncode or 1)


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Capture a web page as a single self-contained HTML file using SingleFile CLI.",
    )
    p.add_argument("url", help="URL to capture, e.g. https://outlook.com")
    p.add_argument(
        "-o", "--output", default="outlook.html",
        help="Output HTML file path (default: outlook.html)",
    )
    p.add_argument(
        "--no-headless", action="store_true",
        help="Run browser with a visible window (useful for debugging / first login).",
    )
    p.add_argument(
        "--wait-time", type=int, default=5000, dest="wait_delay_ms",
        help="Milliseconds to wait after page load before capturing (default: 5000).",
    )
    p.add_argument(
        "--load-max-time", type=int, default=60000,
        help="Max ms to wait for the page to load (default: 60000).",
    )
    p.add_argument(
        "--capture-max-time", type=int, default=60000,
        help="Max ms for the capture itself (default: 60000).",
    )
    p.add_argument(
        "--wait-until", default="networkIdle",
        choices=["networkIdle", "networkAlmostIdle", "load", "domContentLoaded", "InteractiveTime"],
        help="When to consider the page is loaded (default: networkIdle).",
    )
    p.add_argument(
        "--no-wait-until-fallback", action="store_false", dest="wait_until_fback",
        help="Disable retrying with a less strict wait condition on timeout.",
    )
    p.add_argument(
        "--user-agent", help="Override User-Agent string.",
    )
    p.add_argument(
        "--http-header", action="append", default=[],
        help="Extra HTTP header, e.g. 'Accept-Language: en-US'. Repeatable.",
    )
    p.add_argument(
        "--browser-cookies-file",
        help="Path to a JSON or Netscape cookies file for authenticated captures.",
    )
    p.add_argument(
        "--browser-executable-path",
        help="Path to a specific browser binary (e.g. C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe).",
    )
    p.add_argument(
        "--output-directory",
        help="Directory where the output file is written (must exist).",
    )
    p.add_argument(
        "--filename-template",
        help='Template for the output filename, e.g. "{page-title} ({date-locale}).html".',
    )
    p.add_argument(
        "--no-user-script", action="store_false", dest="user_script_enabled",
        help="Disable the SingleFile 'world' event API. Workaround for "
             "'Execution context not found for SingleFile world' errors.",
    )
    p.add_argument(
        "--timeout", type=int, default=180,
        help="Max seconds for the capture subprocess (default: 180).",
    )
    p.add_argument(
        "--extra", nargs=argparse.REMAINDER, default=[],
        help="Pass any extra raw single-file flags at the end of the command.",
    )
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    return capture_page(
        url=args.url,
        output=args.output,
        headless=not args.no_headless,
        wait_delay_ms=args.wait_delay_ms,
        load_max_time_ms=args.load_max_time,
        capture_max_time_ms=args.capture_max_time,
        wait_until=args.wait_until,
        wait_until_fallback=args.wait_until_fback,
        user_agent=args.user_agent,
        http_headers=args.http_header or None,
        cookies_file=args.browser_cookies_file,
        browser_executable_path=args.browser_executable_path,
        output_directory=args.output_directory,
        filename_template=args.filename_template,
        user_script_enabled=args.user_script_enabled,
        extra_args=args.extra or None,
        timeout=args.timeout,
    )


if __name__ == "__main__":
    sys.exit(main())
