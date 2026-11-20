"""Deliver the digest to one WhatsApp number.

WhatsApp has no official "message myself" API for individuals, so delivery goes
through a small notification gateway chosen by ``WHATSAPP_PROVIDER``:

* ``callmebot``  — https://www.callmebot.com/blog/free-api-whatsapp-messages/
  Free, personal use.  Their bot number messages you; they never get access to
  your account.  You opt in from your own phone and receive an API key.
* ``textmebot``  — https://textmebot.com  (paid sibling service, same call shape).

Both take the recipient number and key as query parameters — that is their API,
there is no header alternative — so this module never logs a request URL.

Configuration (environment variables, never the repo):

    WHATSAPP_PROVIDER     callmebot (default) | textmebot
    WHATSAPP_PHONE        recipient in international format, e.g. +34600000000
    WHATSAPP_APIKEY       the key the provider gave you
    WHATSAPP_CHUNK_CHARS  optional — longest single message, default 2000
"""
from __future__ import annotations

import asyncio
import html
import os
import re
from dataclasses import dataclass
from typing import List, Mapping, Optional

import httpx

# Messages are sent as GET requests with the text in the URL.  2000 characters
# puts a full digest in two messages (today / tomorrow) while keeping the
# encoded URL around 4 KB — emoji and accents expand a lot when encoded.
# Neither provider documents a limit, so WHATSAPP_CHUNK_CHARS can lower it.
CHUNK_CHARS = 2000
PAUSE_BETWEEN_CHUNKS_SECS = 6.0
SECTION_MARK = "━━"   # how render.py opens the TODAY / TOMORROW headers

_PROVIDERS = {
    "callmebot": ("https://api.callmebot.com/whatsapp.php", "phone"),
    "textmebot": ("https://api.textmebot.com/send.php", "recipient"),
}
# Neither provider documents its responses; these words in a 200 body have
# meant "not delivered" in practice, so treat them as failure rather than assume.
_FAILURE_WORDS = re.compile(r"\b(error|invalid|not allowed|not activated|disconnected|failed|blocked)\b", re.I)


class NotConfigured(Exception):
    """WhatsApp delivery has not been set up; the message names what is missing."""


@dataclass(frozen=True)
class WhatsAppConfig:
    provider: str
    phone: str
    apikey: str
    chunk_chars: int = CHUNK_CHARS


@dataclass
class SendResult:
    ok: bool
    status_code: int
    detail: str


def config_from_env(env: Optional[Mapping[str, str]] = None) -> WhatsAppConfig:
    env = os.environ if env is None else env
    provider = (env.get("WHATSAPP_PROVIDER") or "callmebot").strip().lower()
    if provider not in _PROVIDERS:
        raise NotConfigured("WHATSAPP_PROVIDER=%r is not supported (use one of: %s)"
                            % (provider, ", ".join(sorted(_PROVIDERS))))
    phone = re.sub(r"[\s().-]", "", env.get("WHATSAPP_PHONE") or "")
    apikey = (env.get("WHATSAPP_APIKEY") or "").strip()
    missing = [name for name, value in (("WHATSAPP_PHONE", phone), ("WHATSAPP_APIKEY", apikey)) if not value]
    if missing:
        raise NotConfigured("missing %s" % " and ".join(missing))
    if not re.fullmatch(r"\+?\d{8,15}", phone):
        raise NotConfigured("WHATSAPP_PHONE must be digits in international format, e.g. +34600000000")
    if not phone.startswith("+"):
        phone = "+" + phone
    chunk = (env.get("WHATSAPP_CHUNK_CHARS") or "").strip()
    if chunk and not (chunk.isdigit() and 300 <= int(chunk) <= 4000):
        raise NotConfigured("WHATSAPP_CHUNK_CHARS must be a number between 300 and 4000")
    return WhatsAppConfig(provider, phone, apikey, int(chunk) if chunk else CHUNK_CHARS)


def split_message(text: str, limit: int = CHUNK_CHARS) -> List[str]:
    """Split on blank lines so an event block is never cut in half.

    A message that fits is sent whole.  One that does not is broken at a
    section header when possible, so "today" and "tomorrow" arrive as two tidy
    messages rather than wherever the character count happened to run out.
    """
    blocks = [block.strip() for block in text.strip().split("\n\n") if block.strip()]
    whole = "\n\n".join(blocks)
    if len(whole) <= limit:
        return [whole] if whole else []
    chunks: List[str] = []
    current = ""
    for block in blocks:
        if block.startswith(SECTION_MARK) and len(current) > limit * 0.35:
            chunks.append(current)
            current = ""
        while len(block) > limit:           # a single oversized block: hard-wrap on lines
            cut = block.rfind("\n", 0, limit)
            cut = cut if cut > 0 else limit
            if current:
                chunks.append(current)
                current = ""
            chunks.append(block[:cut].rstrip())
            block = block[cut:].lstrip()
        if not current:
            current = block
        elif len(current) + 2 + len(block) <= limit:
            current += "\n\n" + block
        else:
            chunks.append(current)
            current = block
    if current:
        chunks.append(current)
    return chunks


def _summarise(body: str) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", " ", body or ""))
    return re.sub(r"\s+", " ", text).strip()[:200]


async def _send_chunk(chunk: str, cfg: WhatsAppConfig, client: httpx.AsyncClient) -> SendResult:
    url, phone_param = _PROVIDERS[cfg.provider]
    try:
        response = await client.get(url, params={phone_param: cfg.phone, "text": chunk, "apikey": cfg.apikey})
    except httpx.HTTPError as exc:
        # str(exc) can embed the request URL (which carries the key) — report the type only.
        return SendResult(False, 0, "network error: %s" % type(exc).__name__)
    if response.headers.get("x-deny-reason"):
        return SendResult(False, response.status_code,
                          "blocked by the sandbox network policy — allow %s in the cloud environment" % response.request.url.host)
    detail = _summarise(response.text)
    ok = response.status_code == 200 and not _FAILURE_WORDS.search(detail)
    return SendResult(ok, response.status_code, detail or "(empty response)")


async def send_message(text: str, cfg: WhatsAppConfig, client: httpx.AsyncClient,
                       pause: float = PAUSE_BETWEEN_CHUNKS_SECS) -> List[SendResult]:
    """Send ``text`` as one or more WhatsApp messages.  Stops at the first failure."""
    results: List[SendResult] = []
    chunks = split_message(text, cfg.chunk_chars)
    for position, chunk in enumerate(chunks):
        if position:
            await asyncio.sleep(pause)
        result = await _send_chunk(chunk, cfg, client)
        results.append(result)
        if not result.ok:
            break
    return results
