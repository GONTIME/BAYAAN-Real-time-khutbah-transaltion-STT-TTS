"""Telegram caption mirror (ADR-0016; §107, §134, §149, §186).

Mirrors every finalized caption line - the SAME post-gate event the room
WebSocket receives - to a Telegram bot chat or channel as "source language +
text of translated languages"::

    Bayaan · room ABC12345 · line #12
    ar (العربية): <the spoken source line>
    it (Italiano): <the approved translation>
    en (English): <the approved translation>

Why a mirror is not a second pipeline path (§0 rule 5): the notifier is fed the
event AFTER the §78 router, §79 validator and §95/§167 safety gate, so what the
channel shows is exactly what the room shows - never more. A blocked target
travels as the public fallback message (§134); operator-facing block reasons
stay in the audit log (§98/§168) and are never sent.

Delivery is best-effort and never blocks the live path (§107):
``enqueue_caption`` only appends to a bounded queue (§149) and a single asyncio
worker drains it in order. Overflow drops the OLDEST line (a live mirror must
keep the newest line, unlike the TTS queue which keeps the oldest spoken line
and drops the freshest). A failed send is retried a bounded number of times and
then counted; one bad send can never stall captions or the worker.

Network I/O is stdlib ``urllib.request`` run in an executor - the same choice as
``providers/ollama.py``, so this feature adds no runtime dependency (§186). The
bot token is a secret: it is never logged, and every error string is scrubbed of
it before it leaves this module.
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass

from bayaan_server.config import Settings
from bayaan_server.domain.languages import LANGUAGE_CATALOG
from bayaan_server.logging_setup import get_logger
from bayaan_server.metrics import MetricsRegistry

logger = get_logger(__name__)

# Telegram's hard limit for one message. A caption line is far shorter, but a
# batch or a long translation must never be silently truncated.
MAX_MESSAGE_CHARS = 4096

# Bounded retry budget (§54 spirit): the worker drains a live mirror, so a wait
# is always capped rather than taken from Telegram verbatim.
_MAX_RETRY_SLEEP_SECONDS = 5.0
_RETRY_BACKOFF_SECONDS = 0.5

# Audience-facing labels are ENDONYMS - what the reader calls their own language
# - because this text is read by worshippers, not by the MT prompt. The ISO code
# always comes first so the line stays machine-parseable, and an unknown code
# falls back to the raw code rather than a guess (§0 rule 8). Deliberately NO
# flag emoji: ISO language codes are not country codes ("ar" would render the
# Argentine flag), and a wrong flag is worse than none.
#
# ADR-0017: the labels come from the ONE curated catalog the admin configures
# languages from, so the mirror can never show a different name than the room
# page or the operator picker.
_LANGUAGE_LABELS = {entry.code: entry.endonym for entry in LANGUAGE_CATALOG}

# Escape sets for the opt-in parse modes. Plain text (the default) needs none:
# Arabic carrying <, & or * can never break a message without a parse_mode.
_HTML_ESCAPES = {"&": "&amp;", "<": "&lt;", ">": "&gt;"}
_MARKDOWN_V2_SPECIALS = "\\_*[]()~`>#+-=|{}.!"


class TelegramDeliveryError(RuntimeError):
    """A send that failed after the bounded retries (never carries the token)."""


class _RateLimited(TelegramDeliveryError):
    """HTTP 429 with the ``retry_after`` Telegram asked for (seconds)."""

    def __init__(self, retry_after: float) -> None:
        super().__init__(f"rate limited (retry_after={retry_after})")
        self.retry_after = retry_after


@dataclass
class _Job:
    """One queued message. ``text`` is already formatted (and escaped)."""

    text: str


def _escape(text: str, parse_mode: str) -> str:
    """Escape a message body for the configured parse mode (no-op by default)."""
    mode = (parse_mode or "").strip().lower()
    if mode == "html":
        for raw, entity in _HTML_ESCAPES.items():
            text = text.replace(raw, entity)
        return text
    if mode == "markdownv2":
        for char in _MARKDOWN_V2_SPECIALS:
            text = text.replace(char, f"\\{char}")
        return text
    return text


def _chunks(text: str, limit: int = MAX_MESSAGE_CHARS) -> list[str]:
    """Split at line boundaries so a long line set still delivers (§149 bounds)."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        # A single line longer than the limit is hard-split, never dropped.
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


class TelegramNotifier:
    """Bounded, non-blocking caption mirror for a bot chat or channel.

    Inert unless ``telegram.enabled`` AND both ``bot_token`` and ``chat_id`` are
    set - the same "off by default, explicit opt-in" posture as TTS (§201).
    """

    def __init__(self, settings: Settings, metrics: MetricsRegistry) -> None:
        self._cfg = settings.telegram
        self._languages = settings.languages
        self._metrics = metrics
        # deque(maxlen=...) gives drop-oldest overflow for free (§149); the drop
        # is counted in _enqueue so a throttled mirror is visible, never silent.
        self._queue: deque[_Job] = deque(maxlen=self._cfg.queue_maxsize)
        self._task: asyncio.Task | None = None
        self._closed = False
        self._last_sent_monotonic_ms: float | None = None

    # -- configuration surface ------------------------------------------------
    @property
    def enabled(self) -> bool:
        """True only when explicitly configured (§184: no silent assumptions)."""
        return bool(self._cfg.enabled and self._cfg.bot_token and self._cfg.chat_id)

    @property
    def queue_depth(self) -> int:
        return len(self._queue)

    @property
    def configured(self) -> dict:
        """Token-free description for diagnostics (never leaks the secret)."""
        return {
            "enabled": self._cfg.enabled,
            "active": self.enabled,
            "chat_id_set": bool(self._cfg.chat_id),
            "send_captions": self._cfg.send_captions,
            "queue_maxsize": self._cfg.queue_maxsize,
            "queue_depth": len(self._queue),
            "include_source": self._cfg.include_source,
            "include_room_code": self._cfg.include_room_code,
            "skip_unavailable": self._cfg.skip_unavailable,
            "parse_mode": self._cfg.parse_mode,
        }

    # -- message formatting ---------------------------------------------------
    def format_caption(self, event: dict, room_code: str | None = None) -> str:
        """Render one caption event as the mirror message (§0 rule 8: honest).

        Reads only fields the room already received: ``arabic`` (the source
        line) and ``targets`` (each language's already-gated text + state).
        """
        lines: list[str] = []
        if self._cfg.include_room_code and room_code:
            lines.append(
                f"Bayaan · room {room_code} · line #{event.get('sequence', 0)}"
            )

        if self._cfg.include_source:
            source = str(event.get("arabic") or "").strip()
            if source:
                lines.append(
                    f"{self._label(self._languages.source_language)}: {source}"
                )

        targets = event.get("targets") or {}
        # Configured order first (it, en...), then any extra key the event
        # carries that config does not list - never drop a language silently.
        ordered = [lang for lang in self._languages.target_languages if lang in targets]
        ordered += [lang for lang in targets if lang not in ordered]
        for lang in ordered:
            entry = targets.get(lang) or {}
            if self._cfg.skip_unavailable and entry.get("state") == "unavailable":
                continue
            text = str(entry.get("text") or "").strip()
            if not text:
                continue
            lines.append(f"{self._label(lang)}: {text}")

        return _escape("\n".join(lines), self._cfg.parse_mode)

    @staticmethod
    def _label(language: str) -> str:
        """``ar (العربية)`` - the ISO code first, endonym in parentheses."""
        label = _LANGUAGE_LABELS.get(language)
        return f"{language} ({label})" if label else language

    # -- enqueue (never blocks the live path, §107) ---------------------------
    def enqueue_caption(self, event: dict, room_code: str | None = None) -> bool:
        """Queue one caption line for the mirror. True only if it was queued."""
        if not self.enabled or not self._cfg.send_captions or self._closed:
            return False
        text = self.format_caption(event, room_code)
        if not text.strip():
            return False
        return self._enqueue(text)

    def enqueue_text(self, text: str) -> bool:
        """Queue a raw message (diagnostics/test send). Never raises."""
        if not self.enabled or self._closed:
            return False
        body = _escape(str(text or ""), self._cfg.parse_mode).strip()
        if not body:
            return False
        return self._enqueue(body)

    def _enqueue(self, text: str) -> bool:
        if len(self._queue) == self._queue.maxlen:
            # The drop itself happens inside deque.append; count it (§168) so a
            # throttled mirror is visible to the operator, never silent.
            self._metrics.incr("bayaan_telegram_dropped_total")
        self._queue.append(_Job(text=text))
        self._metrics.set_gauge("bayaan_telegram_queue_depth", len(self._queue))
        return True

    # -- delivery -------------------------------------------------------------
    def _scrub(self, message: str) -> str:
        """The bot token must never travel in an error string (§58 secrets)."""
        token = self._cfg.bot_token
        return message.replace(token, "***") if token else message

    def _endpoint(self) -> str:
        base = self._cfg.api_base_url.rstrip("/")
        return f"{base}/bot{self._cfg.bot_token}/sendMessage"

    def _post_sync(self, text: str) -> None:
        """One blocking POST (runs in an executor, never on the event loop)."""
        payload: dict = {
            "chat_id": self._cfg.chat_id,
            "text": text,
            "disable_notification": self._cfg.disable_notification,
        }
        if self._cfg.parse_mode:
            payload["parse_mode"] = self._cfg.parse_mode
        request = urllib.request.Request(
            self._endpoint(),
            data=json.dumps(payload).encode("utf-8"),
            headers={"content-type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self._cfg.timeout_seconds
            ) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raw = ""
            try:
                raw = exc.read().decode("utf-8")
            except Exception:  # noqa: BLE001 - body may already be consumed
                raw = ""
            parsed: dict = {}
            try:
                parsed = json.loads(raw) if raw else {}
            except ValueError:
                parsed = {}
            if exc.code == 429:
                params = parsed.get("parameters") or {}
                try:
                    retry_after = float(params.get("retry_after", 1))
                except (TypeError, ValueError):
                    retry_after = 1.0
                raise _RateLimited(retry_after) from exc
            detail = str(parsed.get("description") or exc.reason or "")
            raise TelegramDeliveryError(
                self._scrub(f"HTTP {exc.code}: {detail}")
            ) from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise TelegramDeliveryError(
                self._scrub(f"request failed: {exc}")
            ) from exc
        if not isinstance(body, dict) or not body.get("ok"):
            description = body.get("description") if isinstance(body, dict) else None
            raise TelegramDeliveryError(
                self._scrub(
                    f"telegram rejected the message: {description or 'malformed response'}"
                )
            )

    async def _deliver(self, text: str) -> None:
        """Send with bounded retries; raises TelegramDeliveryError when spent."""
        loop = asyncio.get_running_loop()
        attempt = 0
        while True:
            attempt += 1
            try:
                await loop.run_in_executor(None, self._post_sync, text)
                return
            except _RateLimited as exc:
                if attempt >= self._cfg.max_attempts:
                    raise
                # Honour the wait Telegram asked for, but bound it (§149): a long
                # retry_after must not freeze the worker that drains the mirror.
                await asyncio.sleep(min(exc.retry_after, _MAX_RETRY_SLEEP_SECONDS))
            except TelegramDeliveryError:
                if attempt >= self._cfg.max_attempts:
                    raise
                await asyncio.sleep(_RETRY_BACKOFF_SECONDS * attempt)

    async def _send_job(self, job: _Job) -> None:
        """Deliver one queued message; failures are counted, never raised."""
        start = time.perf_counter()
        try:
            for chunk in _chunks(job.text):
                await self._deliver(chunk)
        except TelegramDeliveryError as exc:
            # §107/§195: a failed mirror is reported and dropped. It never raises
            # into the caption path and never degrades the service - captions
            # themselves are unaffected, so the room keeps working.
            self._metrics.incr("bayaan_telegram_failed_total")
            logger.warning(
                "telegram mirror send failed",
                extra={
                    "event": "telegram_send_failed",
                    "data": {"error": str(exc)[:300]},
                },
            )
            return
        self._metrics.incr("bayaan_telegram_sent_total")
        self._metrics.observe(
            "bayaan_telegram_latency_ms", (time.perf_counter() - start) * 1000.0
        )

    async def _throttle(self) -> None:
        """Space sends by ``min_interval_ms`` (0 = every line, no wait)."""
        interval = self._cfg.min_interval_ms
        if interval <= 0 or self._last_sent_monotonic_ms is None:
            return
        elapsed = time.monotonic() * 1000.0 - self._last_sent_monotonic_ms
        if elapsed < interval:
            await asyncio.sleep((interval - elapsed) / 1000.0)

    async def _worker(self) -> None:
        """Drain the queue in order until cancelled (lifespan-managed)."""
        while not self._closed:
            job = self._queue.popleft() if self._queue else None
            if job is None:
                await asyncio.sleep(0.02)
                continue
            self._metrics.set_gauge("bayaan_telegram_queue_depth", len(self._queue))
            await self._throttle()
            try:
                await self._send_job(job)
            except Exception:  # noqa: BLE001 - one bad job must not kill the mirror
                self._metrics.incr("bayaan_telegram_failed_total")
                logger.exception(
                    "telegram worker error",
                    extra={"event": "telegram_worker_error", "data": {}},
                )
            self._last_sent_monotonic_ms = time.monotonic() * 1000.0

    # -- lifecycle (§194 lifespan-managed, like the TTS worker) ---------------
    def start(self) -> None:
        """Start the worker. No-op unless configured (§201 off-by-default)."""
        if self._task is None and self.enabled and not self._closed:
            self._task = asyncio.create_task(self._worker())

    async def stop(self) -> None:
        self._closed = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self._queue.clear()
        self._metrics.set_gauge("bayaan_telegram_queue_depth", 0)

    async def send_now(self, text: str) -> None:
        """Deliver one message immediately (diagnostics); raises on failure.

        Bypasses the queue on purpose: the operator asked for a verification
        send and needs the real result, not "queued" (§0 rule 8).
        """
        if not self.enabled:
            raise TelegramDeliveryError(
                "telegram mirror is not configured (enabled/bot_token/chat_id)"
            )
        body = _escape(str(text or ""), self._cfg.parse_mode).strip()
        if not body:
            raise TelegramDeliveryError("nothing to send")
        await self._deliver(body[:MAX_MESSAGE_CHARS])
