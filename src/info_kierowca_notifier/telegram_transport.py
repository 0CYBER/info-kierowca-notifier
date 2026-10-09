"""Optional Telegram Bot API transport, used alongside ntfy.

Telegram is a second, opt-in delivery channel: every caller still sends its
ntfy push first and treats Telegram as best effort.  Like
:mod:`ntfy_transport`, this module never puts the bot token, a URL, a request
body, or an exception string into an outcome's ``detail`` (urllib errors embed
the request URL, and a Bot API URL contains the token).
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

from info_kierowca_notifier import tls_transport
from info_kierowca_notifier.ntfy_transport import (
    HTTP_ERROR,
    NETWORK_ERROR,
    NOT_CONFIGURED,
    SUCCESS,
    TLS_CONFIGURATION_ERROR,
    TLS_ERROR,
    NotificationOutcome,
    _tls_error,
)

TELEGRAM_API = "https://api.telegram.org"
DEFAULT_TIMEOUT = 15
MAX_MESSAGE_LENGTH = 4096

# "123456789:AA..." as issued by @BotFather. The token goes into the Bot API
# URL path, so only this charset is ever accepted.
BOT_TOKEN_PATTERN = re.compile(r"^[0-9]{5,15}:[A-Za-z0-9_-]{30,64}$")
# A numeric user/group id (groups and channels are negative) or a public
# channel username such as "@my_channel".
CHAT_ID_PATTERN = re.compile(r"^(-?[0-9]{1,20}|@[A-Za-z][A-Za-z0-9_]{4,31})$")

BOT_TOKEN_ERROR = (
    "Telegram bot token looks wrong. Copy it from @BotFather "
    "(it looks like 123456789:AAE...)."
)
CHAT_ID_ERROR = (
    "Telegram chat ID must be a number (e.g. 123456789 or -100123...) "
    "or a public channel name like @my_channel."
)

QUIET_PRIORITIES = {"min", "low", "1", "2"}
PRIORITY_PREFIX = {"urgent": "\U0001F6A8 ", "max": "\U0001F6A8 ", "5": "\U0001F6A8 "}


def valid_token(token) -> bool:
    return isinstance(token, str) and bool(BOT_TOKEN_PATTERN.match(token))


def valid_chat_id(chat_id) -> bool:
    return isinstance(chat_id, str) and bool(CHAT_ID_PATTERN.match(chat_id))


def config_channel(config):
    """Return ``(token, chat_id)`` when Telegram is enabled and fully,
    validly configured in ``config``; otherwise ``None``."""
    if not isinstance(config, dict) or not config.get("telegram_enabled"):
        return None
    token = str(config.get("telegram_bot_token") or "").strip()
    chat_id = str(config.get("telegram_chat_id") or "").strip()
    if not (valid_token(token) and valid_chat_id(chat_id)):
        return None
    return token, chat_id


def format_text(title, message, priority="default"):
    prefix = PRIORITY_PREFIX.get(str(priority), "")
    text = f"{prefix}{title}\n\n{message}" if message else f"{prefix}{title}"
    if len(text) > MAX_MESSAGE_LENGTH:
        text = text[: MAX_MESSAGE_LENGTH - 1] + "\u2026"
    return text


def _describe_api_error(code, description):
    """Map a Bot API error to a safe, actionable message."""
    text = (description or "").lower()
    if code == 401 or code == 404:
        return "Telegram rejected the bot token. Check it in @BotFather."
    if "chat not found" in text:
        return "Telegram chat not found. Open your bot, press Start (/start), then check the chat ID."
    if code == 403:
        return "The bot can't message this chat. Open the bot, press Start (/start) and don't block it."
    if code == 429:
        return "Telegram is rate limiting this bot. Try again in a minute."
    if code == 409:
        return "This bot has a webhook set, so the chat ID can't be looked up here. Enter it manually."
    if code == 400:
        return "Telegram rejected the request. Check the chat ID."
    return f"Telegram returned HTTP {code}."


def _read_error_description(error):
    try:
        body = json.loads(error.read().decode("utf-8", "replace"))
        return str(body.get("description") or "")
    except Exception:
        return ""


def _call(token, method, payload=None, *, timeout=DEFAULT_TIMEOUT):
    """Call one Bot API method. Returns ``(outcome, result_or_None)``."""
    if not valid_token(token):
        return NotificationOutcome(NOT_CONFIGURED, BOT_TOKEN_ERROR), None
    data = json.dumps(payload or {}).encode("utf-8")
    request = urllib.request.Request(
        f"{TELEGRAM_API}/bot{token}/{method}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with tls_transport.urlopen(request, timeout=timeout) as response:
            status = getattr(response, "status", None)
            if status is None:
                status = response.getcode()
            raw = response.read()
        if not 200 <= status < 300:
            return NotificationOutcome(HTTP_ERROR, _describe_api_error(status, ""), status), None
        try:
            body = json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            body = None
        if not isinstance(body, dict) or not body.get("ok"):
            code = body.get("error_code", status) if isinstance(body, dict) else status
            desc = body.get("description", "") if isinstance(body, dict) else ""
            return NotificationOutcome(HTTP_ERROR, _describe_api_error(code, desc), status), None
        return NotificationOutcome(SUCCESS, "Message accepted by Telegram.", status), body.get("result")
    except urllib.error.HTTPError as error:
        desc = _read_error_description(error)
        return NotificationOutcome(HTTP_ERROR, _describe_api_error(error.code, desc), error.code), None
    except Exception as error:
        if _tls_error(error):
            if isinstance(error, tls_transport.TLSConfigurationError):
                return NotificationOutcome(
                    TLS_CONFIGURATION_ERROR,
                    "TLS trust configuration is invalid. Check the configured CA bundle.",
                ), None
            return NotificationOutcome(
                TLS_ERROR, "TLS certificate verification failed. Check your system trust settings."
            ), None
        return NotificationOutcome(
            NETWORK_ERROR, "Could not reach Telegram. Check your network connection."
        ), None


def push_telegram(token, chat_id, title, message, priority="default", *, timeout=DEFAULT_TIMEOUT):
    """Send one Telegram message and return its verified delivery outcome."""
    token = (token or "").strip()
    chat_id = str(chat_id or "").strip()
    if not token or not chat_id:
        return NotificationOutcome(NOT_CONFIGURED, "Telegram is not configured.")
    if not valid_token(token):
        return NotificationOutcome(NOT_CONFIGURED, BOT_TOKEN_ERROR)
    if not valid_chat_id(chat_id):
        return NotificationOutcome(NOT_CONFIGURED, CHAT_ID_ERROR)
    payload = {
        "chat_id": chat_id,
        "text": format_text(title, message, priority),
        "disable_web_page_preview": True,
    }
    if str(priority) in QUIET_PRIORITIES:
        payload["disable_notification"] = True
    outcome, _ = _call(token, "sendMessage", payload, timeout=timeout)
    return outcome


def push_from_config(config, title, message, priority="default", *, timeout=DEFAULT_TIMEOUT):
    """Send via Telegram if ``config`` enables it; NOT_CONFIGURED otherwise."""
    channel = config_channel(config)
    if channel is None:
        return NotificationOutcome(NOT_CONFIGURED, "Telegram is disabled or not configured.")
    token, chat_id = channel
    return push_telegram(token, chat_id, title, message, priority, timeout=timeout)


def find_chat_ids(token, *, timeout=DEFAULT_TIMEOUT):
    """List chats that recently messaged the bot (via ``getUpdates``).

    Returns ``(outcome, chats)`` where ``chats`` is a list of
    ``{"id": str, "type": str, "name": str}``, newest first. Only works while
    the bot has no webhook; the user must send the bot a message first.
    """
    token = (token or "").strip()
    if not valid_token(token):
        return NotificationOutcome(NOT_CONFIGURED, BOT_TOKEN_ERROR), []
    outcome, result = _call(
        token, "getUpdates", {"limit": 100, "timeout": 0}, timeout=timeout
    )
    if not outcome.ok:
        return outcome, []
    chats = []
    seen = set()
    for update in reversed(result if isinstance(result, list) else []):
        if not isinstance(update, dict):
            continue
        for key in ("message", "edited_message", "channel_post", "my_chat_member"):
            chat = (update.get(key) or {}).get("chat") if isinstance(update.get(key), dict) else None
            if not isinstance(chat, dict) or "id" not in chat:
                continue
            chat_id = str(chat["id"])
            if chat_id in seen:
                continue
            seen.add(chat_id)
            name = chat.get("title") or " ".join(
                part for part in (chat.get("first_name"), chat.get("last_name")) if part
            ) or (("@" + chat["username"]) if chat.get("username") else "")
            chats.append({"id": chat_id, "type": str(chat.get("type") or ""), "name": str(name)})
    return outcome, chats
