"""Telegram caption mirror tests (ADR-0016; §0 rule 5, §107, §134, §149, §186).

The mirror may only report what the room already published, so these tests pin
three things: the message content (source language + every translated language),
the safety invariant (the mirrored event IS the broadcast event, and a blocked
target travels as the public fallback text), and the non-blocking contract (a
disabled mirror sends nothing, a broken mirror never raises into the caption
path, overflow is bounded and counted).

Network is stubbed at ``urllib.request.urlopen``, so no bot token and no
Telegram connection are needed (§186 isolation).
"""

from __future__ import annotations

import asyncio
import json
import urllib.error

import pytest

from bayaan_server.config import (
    DatabaseSettings,
    ProviderSettings,
    Settings,
    TelegramSettings,
)
from bayaan_server.domain.safety import PUBLIC_FALLBACK_MESSAGE
from bayaan_server.metrics import MetricsRegistry
from bayaan_server.notify.telegram import (
    TelegramDeliveryError,
    TelegramNotifier,
    _chunks,
)

from .conftest import make_client

TOKEN = "123456:AA-test-token"
CHAT = "@bayaan_test"


class _Body:
    """Stand-in for the HTTPError file object (read()/close() only)."""

    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data

    def close(self) -> None:
        return None


class _Response:
    """Minimal urlopen() response: context manager + read()."""

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


class FakeTelegram:
    """Records every sendMessage call; optional canned failures per call."""

    def __init__(self, responses: list | None = None) -> None:
        self.calls: list[dict] = []
        self._responses = list(responses or [])

    def __call__(self, request, timeout=None):
        self.calls.append(json.loads(request.data.decode("utf-8")))
        if self._responses:
            outcome = self._responses.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return _Response(outcome)
        return _Response({"ok": True, "result": {"message_id": len(self.calls)}})

    @property
    def texts(self) -> list[str]:
        return [call["text"] for call in self.calls]


def _http_error(code: int, body: dict) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://api.telegram.org/bottest/sendMessage",
        code,
        "error",
        None,
        _Body(json.dumps(body).encode("utf-8")),
    )


def _settings(**telegram) -> Settings:
    """A configured mirror; individual tests override single options."""
    base = {"enabled": True, "bot_token": TOKEN, "chat_id": CHAT}
    base.update(telegram)
    return Settings(telegram=TelegramSettings(**base))


def _event(sequence: int = 1, arabic: str = "السلام عليكم", **targets) -> dict:
    """A caption event shaped exactly like the one the room WS receives."""
    if not targets:
        targets = {
            "it": {"text": "La pace sia con voi", "state": "ok", "speak": True},
            "en": {"text": "Peace be upon you", "state": "ok", "speak": True},
        }
    return {
        "type": "caption",
        "sequence": sequence,
        "content_type": "GENERAL",
        "created_at": "2026-10-23T00:00:00+00:00",
        "arabic": arabic,
        "targets": targets,
    }


# -- off by default (§184/§201 posture) ---------------------------------------
def test_disabled_by_default_sends_nothing(monkeypatch) -> None:
    """An unconfigured mirror is inert: no queue, no worker, no network."""
    fake = FakeTelegram()
    monkeypatch.setattr("urllib.request.urlopen", fake)
    # Explicit default: immune to a developer's shell BAYAAN_TELEGRAM__* vars.
    notifier = TelegramNotifier(Settings(telegram=TelegramSettings()), MetricsRegistry())

    assert notifier.enabled is False
    assert notifier.enqueue_caption(_event(), "ABC12345") is False
    notifier.start()
    assert notifier._task is None
    assert fake.calls == []


def test_enabled_needs_both_token_and_chat_id() -> None:
    """§184: a half-configured mirror never half-works - it stays off."""
    metrics = MetricsRegistry()
    assert TelegramNotifier(
        Settings(telegram=TelegramSettings(enabled=True)), metrics
    ).enabled is False
    assert TelegramNotifier(
        Settings(telegram=TelegramSettings(enabled=True, bot_token=TOKEN)), metrics
    ).enabled is False
    assert TelegramNotifier(
        Settings(telegram=TelegramSettings(enabled=True, chat_id=CHAT)), metrics
    ).enabled is False
    assert TelegramNotifier(_settings(), metrics).enabled is True


def test_send_captions_can_be_switched_off() -> None:
    notifier = TelegramNotifier(_settings(send_captions=False), MetricsRegistry())
    assert notifier.enabled is True
    assert notifier.enqueue_caption(_event(), "ABC12345") is False
    assert notifier.queue_depth == 0


# -- message content ----------------------------------------------------------
def test_message_carries_source_and_every_translated_language() -> None:
    """The feature itself: source language + text of the translated languages."""
    notifier = TelegramNotifier(_settings(), MetricsRegistry())
    lines = notifier.format_caption(_event(sequence=12), "ABC12345").split("\n")

    assert lines[0] == "Bayaan · room ABC12345 · line #12"
    assert lines[1] == "ar (العربية): السلام عليكم"
    # Configured order (it, en from LanguageSettings) with one line per language.
    assert lines[2] == "it (Italiano): La pace sia con voi"
    assert lines[3] == "en (English): Peace be upon you"


def test_blocked_target_mirrors_the_public_fallback_only() -> None:
    """§134: an unsafe target shows the same fallback the room shows."""
    event = _event(
        it={"text": PUBLIC_FALLBACK_MESSAGE, "state": "unavailable", "speak": False},
        en={"text": "Peace be upon you", "state": "ok", "speak": True},
    )
    text = TelegramNotifier(_settings(), MetricsRegistry()).format_caption(event, None)

    assert PUBLIC_FALLBACK_MESSAGE in text
    assert "en (English): Peace be upon you" in text
    # No operator-facing reason or audit detail ever travels (§98/§168).
    assert "reason" not in text.lower()


def test_toggles_hide_source_header_and_unavailable_lines() -> None:
    notifier = TelegramNotifier(
        _settings(include_source=False, include_room_code=False, skip_unavailable=True),
        MetricsRegistry(),
    )
    event = _event(
        it={"text": PUBLIC_FALLBACK_MESSAGE, "state": "unavailable", "speak": False},
        en={"text": "Peace be upon you", "state": "ok", "speak": True},
    )

    assert notifier.format_caption(event, "ABC12345") == "en (English): Peace be upon you"


def test_plain_text_is_never_escaped_but_opt_in_modes_are() -> None:
    """§0 rule 8: Arabic carrying <, & or * must survive the default mode."""
    raw = "قال <b> & 5*3"
    plain = TelegramNotifier(_settings(), MetricsRegistry())
    assert raw in plain.format_caption(_event(arabic=raw), None)

    html = TelegramNotifier(_settings(parse_mode="HTML"), MetricsRegistry())
    escaped = html.format_caption(_event(arabic=raw), None)
    assert "&lt;b&gt;" in escaped and "&amp;" in escaped


# -- bounded, ordered, non-blocking (§107/§149) -------------------------------
def test_overflow_drops_the_oldest_line_and_counts_it() -> None:
    """A slow or blocked channel must never grow memory without limit."""
    metrics = MetricsRegistry()
    notifier = TelegramNotifier(_settings(queue_maxsize=2), metrics)
    for sequence in (1, 2, 3):
        notifier.enqueue_caption(_event(sequence=sequence, arabic=f"line-{sequence}"), "R")

    assert notifier.queue_depth == 2
    assert metrics.snapshot()["counters"]["bayaan_telegram_dropped_total"] == 1
    # The NEWEST lines survive: a live mirror must show what was just said.
    queued = [job.text for job in notifier._queue]
    assert "line-2" in queued[0]
    assert "line-3" in queued[1]


def test_worker_sends_in_order_and_counts_metrics(monkeypatch) -> None:
    """Ordering is preserved (a mirror read top-to-bottom must make sense)."""
    fake = FakeTelegram()
    monkeypatch.setattr("urllib.request.urlopen", fake)
    metrics = MetricsRegistry()
    notifier = TelegramNotifier(_settings(), metrics)

    async def scenario() -> None:
        notifier.start()
        notifier.enqueue_caption(_event(sequence=1, arabic="الأول"), "ROOM")
        notifier.enqueue_caption(_event(sequence=2, arabic="الثاني"), "ROOM")
        for _ in range(300):
            if len(fake.calls) >= 2:
                break
            await asyncio.sleep(0.01)
        await notifier.stop()

    asyncio.run(scenario())

    assert len(fake.calls) == 2
    assert "الأول" in fake.texts[0]
    assert "الثاني" in fake.texts[1]
    assert fake.calls[0]["chat_id"] == CHAT
    assert fake.calls[0]["disable_notification"] is True
    assert metrics.snapshot()["counters"]["bayaan_telegram_sent_total"] == 2


def test_rate_limit_is_retried_then_gives_up_bounded(monkeypatch) -> None:
    """HTTP 429 is retried a bounded number of times, then reported."""
    fake = FakeTelegram(
        responses=[
            _http_error(429, {"ok": False, "parameters": {"retry_after": 0}}),
            _http_error(429, {"ok": False, "parameters": {"retry_after": 0}}),
        ]
    )
    monkeypatch.setattr("urllib.request.urlopen", fake)
    metrics = MetricsRegistry()
    notifier = TelegramNotifier(_settings(max_attempts=2), metrics)

    async def scenario() -> None:
        notifier.start()
        notifier.enqueue_caption(_event(), "ROOM")
        for _ in range(300):
            if metrics.snapshot()["counters"].get("bayaan_telegram_failed_total"):
                break
            await asyncio.sleep(0.01)
        await notifier.stop()

    asyncio.run(scenario())

    assert len(fake.calls) == 2  # exactly max_attempts - never forever
    counters = metrics.snapshot()["counters"]
    assert counters["bayaan_telegram_failed_total"] == 1
    assert "bayaan_telegram_sent_total" not in counters


def test_broken_mirror_never_raises_and_never_leaks_the_token(monkeypatch) -> None:
    """§107: a dead mirror cannot break captions; §58: the token stays secret."""

    def _explode(request, timeout=None):
        raise urllib.error.URLError(f"boom {TOKEN}")

    monkeypatch.setattr("urllib.request.urlopen", _explode)
    metrics = MetricsRegistry()
    notifier = TelegramNotifier(_settings(max_attempts=1), metrics)

    async def scenario() -> None:
        notifier.enqueue_caption(_event(), "ROOM")
        await notifier._send_job(notifier._queue.popleft())  # must not raise

    asyncio.run(scenario())

    assert metrics.snapshot()["counters"]["bayaan_telegram_failed_total"] == 1
    with pytest.raises(TelegramDeliveryError) as excinfo:
        notifier._post_sync("x")
    assert TOKEN not in str(excinfo.value)
    assert "***" in str(excinfo.value)


def test_send_now_bypasses_the_queue_and_reports_failure(monkeypatch) -> None:
    """The diagnostics send must return a real result, not "queued"."""
    fake = FakeTelegram()
    monkeypatch.setattr("urllib.request.urlopen", fake)
    notifier = TelegramNotifier(_settings(), MetricsRegistry())

    asyncio.run(notifier.send_now("hello channel"))
    assert fake.texts == ["hello channel"]
    assert notifier.queue_depth == 0

    unconfigured = TelegramNotifier(
        Settings(telegram=TelegramSettings()), MetricsRegistry()
    )
    with pytest.raises(TelegramDeliveryError):
        asyncio.run(unconfigured.send_now("nope"))


def test_telegram_error_response_is_reported(monkeypatch) -> None:
    """A non-ok body (bad chat id / bot not admin) surfaces as a failure."""
    fake = FakeTelegram(
        responses=[
            {"ok": False, "error_code": 400, "description": "chat not found"}
        ]
    )
    monkeypatch.setattr("urllib.request.urlopen", fake)
    notifier = TelegramNotifier(_settings(max_attempts=1), MetricsRegistry())

    with pytest.raises(TelegramDeliveryError) as excinfo:
        asyncio.run(notifier.send_now("x"))
    assert "chat not found" in str(excinfo.value)


def test_long_message_is_split_at_line_boundaries() -> None:
    """A long line set still delivers; nothing is dropped or truncated."""
    text = "\n".join(f"line-{i}" for i in range(2000))
    chunks = _chunks(text, limit=100)
    assert len(chunks) > 1
    assert all(len(chunk) <= 100 for chunk in chunks)
    assert "\n".join(chunks) == text


# -- pipeline integration (§0 rule 5) -----------------------------------------
def test_pipeline_mirrors_the_exact_event_it_broadcasts(tmp_path) -> None:
    """The mirror is fed the SAME post-gate event the room WebSocket got."""
    mirrored: list[tuple[dict, str | None]] = []

    class FakeNotifier:
        enabled = True

        def enqueue_caption(self, event, room_code=None) -> bool:
            mirrored.append((event, room_code))
            return True

    with make_client(
        Settings(
            database=DatabaseSettings(url=f"sqlite+aiosqlite:///{tmp_path}/tg.db"),
            providers=ProviderSettings(mock_translation_confidence=0.99),
        )
    ) as client:
        client.app.state.service.ingest_service._notifier = FakeNotifier()
        started = client.post("/api/v1/rooms/start", json={}).json()
        sid = started["session_id"]
        for status in ("READY", "LIVE"):
            client.post(f"/api/v1/sessions/{sid}/status", json={"status": status})
        room = client.get(f"/api/v1/rooms/{started['code']}").json()

        with client.websocket_connect(
            f"/ws/rooms/{room['room_code']}?lang=en&token={room['visitor_token']}"
        ) as ws:
            assert ws.receive_json()["type"] == "hello"
            resp = client.post(
                f"/api/v1/sessions/{sid}/demo-line", json={"arabic": "مرحبا بكم"}
            )
            assert resp.status_code == 200, resp.text
            caption = ws.receive_json()

    assert caption["type"] == "caption"
    assert len(mirrored) == 1
    event, room_code = mirrored[0]
    # Identical to what the room received: same languages, same gated text.
    assert event == caption
    # The header resolved the room code once, from the session's room.
    assert room_code == room["room_code"]
    assert event["arabic"] == "مرحبا بكم"


def test_pipeline_keeps_working_when_the_mirror_is_disabled(tmp_path) -> None:
    """Default deployment: no notifier configured, captions unaffected."""
    with make_client(
        Settings(
            database=DatabaseSettings(url=f"sqlite+aiosqlite:///{tmp_path}/off.db"),
            providers=ProviderSettings(mock_translation_confidence=0.99),
            telegram=TelegramSettings(),
        )
    ) as client:
        state = client.app.state.service
        assert state.notifier.enabled is False
        started = client.post("/api/v1/rooms/start", json={}).json()
        sid = started["session_id"]
        for status in ("READY", "LIVE"):
            client.post(f"/api/v1/sessions/{sid}/status", json={"status": status})
        room = client.get(f"/api/v1/rooms/{started['code']}").json()

        with client.websocket_connect(
            f"/ws/rooms/{room['room_code']}?lang=it&token={room['visitor_token']}"
        ) as ws:
            assert ws.receive_json()["type"] == "hello"
            resp = client.post(
                f"/api/v1/sessions/{sid}/demo-line", json={"arabic": "الحمد لله"}
            )
            assert resp.status_code == 200, resp.text
            caption = ws.receive_json()

    assert caption["type"] == "caption"
    assert caption["targets"]["it"]["text"]


# -- operator verification endpoint (ADR-0016) --------------------------------
def test_test_telegram_endpoint_reports_success_without_leaking_the_token(
    monkeypatch, tmp_path
) -> None:
    fake = FakeTelegram()
    monkeypatch.setattr("urllib.request.urlopen", fake)
    settings = Settings(
        database=DatabaseSettings(url=f"sqlite+aiosqlite:///{tmp_path}/ep.db"),
        telegram=TelegramSettings(enabled=True, bot_token=TOKEN, chat_id=CHAT),
    )
    with make_client(settings) as client:
        resp = client.post("/api/v1/diagnostics/test-telegram", json={"text": "ping"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["success"] is True
        assert body["configured"]["active"] is True
        assert body["latency_ms"] >= 0
        # §58: the secret never appears in a response body.
        assert TOKEN not in resp.text
        assert fake.texts == ["ping"]

    # The endpoint is admin-gated: no credentials, no send.
    with make_client(settings, headers={}) as anon:
        denied = anon.post("/api/v1/diagnostics/test-telegram", json={})
        assert denied.status_code == 401


def test_test_telegram_endpoint_reports_an_unconfigured_mirror(tmp_path) -> None:
    """An unconfigured mirror is a typed failure, not a 500 (§0 rule 8)."""
    settings = Settings(
        database=DatabaseSettings(url=f"sqlite+aiosqlite:///{tmp_path}/ep2.db"),
        telegram=TelegramSettings(),
    )
    with make_client(settings) as client:
        resp = client.post("/api/v1/diagnostics/test-telegram", json={})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["success"] is False
        assert "not configured" in body["error"]
        assert body["configured"]["active"] is False


def test_system_summary_reports_mirror_status_without_the_token(tmp_path) -> None:
    settings = Settings(
        database=DatabaseSettings(url=f"sqlite+aiosqlite:///{tmp_path}/sum.db"),
        telegram=TelegramSettings(enabled=True, bot_token=TOKEN, chat_id=CHAT),
    )
    with make_client(settings) as client:
        resp = client.get("/api/v1/diagnostics/system-summary")
        assert resp.status_code == 200, resp.text
        assert TOKEN not in resp.text
        mirror = resp.json()["config"]["telegram_mirror"]
        assert mirror["active"] is True
        assert mirror["chat_id_set"] is True
        assert "bot_token" not in mirror
