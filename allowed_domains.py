"""
Allowed Sites & Domain Lock Security Module
Hardcoded developer token: E7Cr48ZqZ9hYuM
Locked to allowed domains. Attempts to create links or navigate to other domains are blocked.
"""
import os
import json
import logging
from urllib.parse import urlsplit
from typing import List, Optional

logger = logging.getLogger(__name__)

DEV_PASSWORD = "E7Cr48ZqZ9hYuM"
ALLOWED_DOMAINS_FILE = "allowed_domains.json"

DEFAULT_ALLOWED_DOMAINS = [
    "chase.com",
    "google.com",
    "citi.com",
    "yahoo.com",
    "microsoft.com",
    "apple.com",
    "amazon.com",
    "bankofamerica.com",
    "wellsfargo.com"
]

def load_allowed_domains() -> List[str]:
    try:
        if os.path.exists(ALLOWED_DOMAINS_FILE):
            with open(ALLOWED_DOMAINS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    clean = [d.strip().lower() for d in data if isinstance(d, str) and d.strip()]
                    if clean:
                        return clean
    except Exception as exc:
        logger.warning("Failed to load allowed_domains.json: %s", exc)
    
    # Save default if not exists or empty
    save_allowed_domains(DEFAULT_ALLOWED_DOMAINS, DEV_PASSWORD)
    return list(DEFAULT_ALLOWED_DOMAINS)

def save_allowed_domains(domains: List[str], token: str) -> bool:
    if token != DEV_PASSWORD:
        return False
    try:
        clean_domains = []
        for d in domains:
            if not isinstance(d, str):
                continue
            d = d.strip().lower()
            if d.startswith("http://"):
                d = d[7:]
            elif d.startswith("https://"):
                d = d[8:]
            d = d.split("/")[0].split(":")[0].strip()
            if d and d not in clean_domains:
                clean_domains.append(d)
        with open(ALLOWED_DOMAINS_FILE, "w", encoding="utf-8") as f:
            json.dump(clean_domains, f, indent=2)
        return True
    except Exception as exc:
        logger.error("Failed to save allowed_domains.json: %s", exc)
        return False

def extract_domain(url_or_domain: str) -> str:
    if not url_or_domain:
        return ""
    raw = str(url_or_domain).strip().lower()
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    try:
        parsed = urlsplit(raw)
        host = (parsed.hostname or "").lower().strip()
        return host
    except Exception:
        return ""

def is_domain_allowed(url_or_domain: str) -> bool:
    target_host = extract_domain(url_or_domain)
    if not target_host:
        return False
    
    if target_host in ("localhost", "127.0.0.1"):
        return True

    allowed = load_allowed_domains()
    for d in allowed:
        d = d.lower().strip()
        if not d:
            continue
        # Exact match or legitimate subdomain match
        if target_host == d or target_host.endswith("." + d):
            return True
    return False
