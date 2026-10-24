"""Language catalog + per-room target-language tests (ADR-0017; §63, §184).

Before ADR-0017 the target languages were hard-coded to Italian/English in
several places: ``test-translation`` only accepted a ``Literal["en","it"]``, the
Dashboard always started rooms with ``["it","en"]``, and the caption pipeline
translated the *global* config list for every room. These tests pin the new
contract:

  * the curated catalog is the single source of truth (labels never guessed),
  * bad configuration fails at startup *with the supported codes listed*,
  * the diagnostics playground can test every configured target,
  * a room broadcasts exactly the languages it was started with,
  * the Telegram mirror labels new languages from the same catalog.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.testclient import WebSocketDisconnect

from bayaan_server.config import (
    DatabaseSettings,
    LanguageSettings,
    ProviderSettings,
    Settings,
    TelegramSettings,
)
from bayaan_server.domain.languages import (
    LANGUAGE_CATALOG,
    LANGUAGE_CODES,
    SOURCE_LANGUAGE,
    TARGET_LANGUAGE_CODES,
    catalog_payload,
    language_english_name,
    language_label,
    supported_codes_csv,
    target_codes_csv,
)
from bayaan_server.metrics import MetricsRegistry
from bayaan_server.notify.telegram import TelegramNotifier

from .conftest import make_client


def _client(tmp_path, name: str = "lang", **languages) -> TestClient:
    """A client whose languages come from the catalog (mock MT for determinism)."""
    return make_client(
        Settings(
            database=DatabaseSettings(url=f"sqlite+aiosqlite:///{tmp_path}/{name}.db"),
            languages=LanguageSettings(**languages),
            providers=ProviderSettings(mock_translation_confidence=0.99),
        )
    )


def _live(client: TestClient, active_languages: list[str]) -> tuple[dict, dict]:
    """Start a room with an explicit language set and bring it LIVE."""
    started = client.post(
        "/api/v1/rooms/start", json={"active_languages": active_languages}
    ).json()
    sid = started["session_id"]
    for status in ("READY", "LIVE"):
        client.post(f"/api/v1/sessions/{sid}/status", json={"status": status})
    room = client.get(f"/api/v1/rooms/{started['code']}").json()
    return started, room


# -- catalog integrity (§0 rule 8: never guess a label) -----------------------


def test_catalog_codes_are_unique_and_labelled() -> None:
    codes = [entry.code for entry in LANGUAGE_CATALOG]
    assert len(codes) == len(set(codes))
    assert all(entry.endonym and entry.english for entry in LANGUAGE_CATALOG)
    # §63: Arabic is the source, so it can never be configured as a target.
    assert SOURCE_LANGUAGE in LANGUAGE_CODES
    assert SOURCE_LANGUAGE not in TARGET_LANGUAGE_CODES
    # The supported set the admin picks from (requested list + defaults).
    assert {
        "it", "en", "fr", "ur", "id", "bn", "fa", "ps", "tr", "sw",
    } <= TARGET_LANGUAGE_CODES


def test_labels_are_endonyms_and_unknown_codes_stay_codes() -> None:
    assert language_label("ur") == "اردو"
    assert language_label("ps") == "پښتو"
    assert language_label("sw") == "Kiswahili"
    # An unknown code is reported as itself - never invented, never a flag.
    assert language_label("zz") == "zz"
    assert language_english_name("zz") == "zz"
    assert "ur" in supported_codes_csv()
    assert SOURCE_LANGUAGE not in target_codes_csv()


def test_catalog_payload_shape() -> None:
    payload = catalog_payload()
    assert {"code", "label", "english"} == set(payload[0])
    assert {entry["code"] for entry in payload} == LANGUAGE_CODES


# -- configuration (§184: fail where the admin can fix it) --------------------


def test_config_defaults_and_deduplication() -> None:
    assert LanguageSettings().target_languages == ["it", "en"]
    assert LanguageSettings(target_languages=["ur", "it", "ur"]).target_languages == [
        "ur", "it",
    ]


@pytest.mark.parametrize("bad", [["it", "zz"], ["ar"], []])
def test_config_rejects_bad_target_sets(bad: list[str]) -> None:
    with pytest.raises(ValidationError) as exc:
        LanguageSettings(target_languages=bad)
    # The message must name the codes that ARE supported.
    assert "Supported targets" in str(exc.value)


def test_config_rejects_an_unknown_source_language() -> None:
    with pytest.raises(ValidationError):
        LanguageSettings(source_language="xx")


def test_config_accepts_the_whole_catalog() -> None:
    codes = [entry.code for entry in LANGUAGE_CATALOG if entry.code != SOURCE_LANGUAGE]
    assert LanguageSettings(target_languages=codes).target_languages == codes


# -- diagnostics playground (§195) -------------------------------------------


def test_test_translation_accepts_every_configured_target(tmp_path) -> None:
    """The old Literal["en","it"] 422'd every other configured language."""
    with _client(tmp_path, target_languages=["it", "ur", "tr"]) as client:
        for code in ("it", "ur", "tr"):
            resp = client.post(
                "/api/v1/diagnostics/test-translation",
                json={"text": "الحمد لله رب العالمين", "target_language": code},
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["success"] is True
            assert body["target_language"] == code
            assert f"MOCK-TRANSLATION:{code}" in body["text"]


def test_test_translation_defaults_to_the_first_configured_target(tmp_path) -> None:
    with _client(tmp_path, target_languages=["ur", "it"]) as client:
        body = client.post(
            "/api/v1/diagnostics/test-translation", json={"text": "الحمد لله"}
        ).json()
        assert body["target_language"] == "ur"


def test_test_translation_rejects_unconfigured_target_with_the_list(tmp_path) -> None:
    with _client(tmp_path, target_languages=["it", "en"]) as client:
        resp = client.post(
            "/api/v1/diagnostics/test-translation",
            json={"text": "الحمد لله", "target_language": "ur"},
        )
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert "ur" in detail and "it, en" in detail


def test_system_summary_exposes_the_language_picker(tmp_path) -> None:
    with _client(tmp_path, target_languages=["it", "ur"]) as client:
        body = client.get("/api/v1/diagnostics/system-summary").json()
    languages = body["languages"]
    assert languages["source"] == SOURCE_LANGUAGE
    assert [target["code"] for target in languages["targets"]] == ["it", "ur"]
    assert languages["targets"][1]["label"] == "اردو"
    # The full catalog travels too, so the picker can offer what is not enabled.
    assert any(entry["code"] == "ps" for entry in languages["catalog"])


# -- rooms: the selection is real, not cosmetic (§63) ------------------------


def test_room_broadcasts_only_its_own_languages(tmp_path) -> None:
    """ADR-0017: config has it/en/ur, the room was started for ur only."""
    with _client(tmp_path, target_languages=["it", "en", "ur"]) as client:
        started, room = _live(client, ["ur"])
        assert started["languages"] == ["ur"]
        # Discovery reports the ROOM's languages, not the global defaults.
        assert room["languages"] == ["ur"]
        token = room["visitor_token"]
        with client.websocket_connect(
            f"/ws/rooms/{room['room_code']}?lang=ur&token={token}"
        ) as ws:
            hello = ws.receive_json()
            assert hello["type"] == "hello"
            assert hello["languages"] == ["ur"]
            resp = client.post(
                f"/api/v1/sessions/{started['session_id']}/demo-line",
                json={"arabic": "مرحبا بكم في مسجدنا"},
            )
            assert resp.status_code == 200, resp.text
            caption = ws.receive_json()
            assert caption["type"] == "caption"
            # Only the room's own language was translated - no wasted provider
            # calls for languages nobody in this room selected.
            assert set(caption["targets"]) == {"ur"}
            assert "MOCK-TRANSLATION:ur" in caption["targets"]["ur"]["text"]


def test_ws_rejects_a_language_the_room_does_not_broadcast(tmp_path) -> None:
    """§188: 'ur' is configured, but this room only broadcasts 'it' -> 4400."""
    with _client(tmp_path, target_languages=["it", "en", "ur"]) as client:
        _started, room = _live(client, ["it"])
        token = room["visitor_token"]
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect(
                f"/ws/rooms/{room['room_code']}?lang=ur&token={token}"
            ) as ws:
                ws.receive_json()
                raise AssertionError("expected rejection")
        assert exc.value.code == 4400


def test_room_without_a_selection_falls_back_to_the_config(tmp_path) -> None:
    with _client(tmp_path, target_languages=["it", "ur"]) as client:
        started = client.post("/api/v1/rooms/start", json={}).json()
        assert started["languages"] == ["it", "ur"]


def test_start_room_rejection_names_the_available_languages(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/rooms/start", json={"active_languages": ["it", "zz"]}
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "zz" in detail
    assert "Supported languages" in detail and "ur" in detail


# -- Telegram mirror labels (ADR-0016 + ADR-0017) -----------------------------


def test_telegram_mirror_labels_come_from_the_catalog() -> None:
    notifier = TelegramNotifier(
        Settings(
            telegram=TelegramSettings(
                enabled=True, bot_token="123456:AA-test-token", chat_id="@bayaan_test"
            )
        ),
        MetricsRegistry(),
    )
    event = {
        "type": "caption",
        "sequence": 1,
        "content_type": "GENERAL",
        "created_at": "2026-10-24T00:00:00+00:00",
        "arabic": "السلام عليكم",
        "targets": {
            "ur": {"text": "خوش آمدید", "state": "ok", "speak": False},
            "tr": {"text": "Hoş geldiniz", "state": "ok", "speak": False},
        },
    }
    text = notifier.format_caption(event, "ABC12345")
    assert "ur (اردو): خوش آمدید" in text
    assert "tr (Türkçe): Hoş geldiniz" in text


# -- Control Panel: the admin picks from a list -------------------------------


def test_control_panel_offers_the_curated_language_picker() -> None:
    from tools import control_panel as cp

    opt = next(
        o for o in cp.build_options() if o["key"] == "BAYAAN_LANGUAGES__TARGET_LANGUAGES"
    )
    codes = [choice["code"] for choice in opt["choices"]]
    assert codes and "ur" in codes
    # The source language is never offered as a target (§63).
    assert SOURCE_LANGUAGE not in codes
    # Still stored as a JSON list in .env, so nothing else changes.
    assert opt["kind"] == "list"
    assert "ur" in opt["description"]

    ok, saved, _ = cp.validate_value(opt, '["it","ur"]')
    assert ok and saved == '["it", "ur"]'
    ok, _, err = cp.validate_value(opt, '["it","zz"]')
    assert not ok and "Unsupported language" in (err or "")
    # An empty target set would make the server refuse to start (§63).
    assert not cp.validate_value(opt, "[]")[0]



