"""Tests for the Bayaan Control Panel (backend/tools/control_panel.py).

Focused on the launcher essentials:
  * option enumeration fully covers the pydantic settings model,
  * .env read/write preserves existing configuration and comments,
  * value validation accepts real values and rejects garbage,
  * secret values are never exposed through the API payload,
  * the double-click launcher script exists and points at the tool.

These tests never start any real servers and never touch the real
``backend/.env``: all file operations use tmp_path.
"""

from __future__ import annotations

import json
from pathlib import Path

from tools import control_panel as cp


# ---------------------------------------------------------------------------
# Option enumeration
# ---------------------------------------------------------------------------


def test_enumerates_every_setting_with_description() -> None:
    options = cp.build_options()
    assert len(options) > 100
    keys = {opt["key"] for opt in options}
    assert {"BAYAAN_APP_NAME", "BAYAAN_PUBLIC_BASE_URL", "BAYAAN_MAX_ACTIVE_SESSIONS"} <= keys
    assert "BAYAAN_LANGUAGES__SOURCE_LANGUAGE" in keys
    assert "BAYAAN_AUDIO__CAPTURE_ENABLED" in keys
    assert "BAYAAN_PROVIDERS__OLLAMA_CLOUD_API_KEY" in keys
    assert "BAYAAN_SAFETY__TTS_ENABLED" in keys
    assert "BAYAAN_TTS__REQUIRE_TRANSLATION_CONFIDENCE" in keys
    # The UI needs a short description for every option.
    missing = [opt["key"] for opt in options if not opt["description"]]
    assert not missing, f"options missing a description: {missing}"


def test_pydantic_env_key_mapping_matches_reality() -> None:
    options = {opt["key"]: opt for opt in cp.build_options()}
    # AuthSettings is the one nested model with its own env_prefix.
    assert "BAYAAN_AUTH__USERNAME" in options
    assert "BAYAAN_AUTH__PASSWORD" in options
    # Deeply nested ConfidenceWeights inside SafetySettings.
    assert "BAYAAN_SAFETY__WEIGHTS__ASR" in options
    assert "BAYAAN_SAFETY__WEIGHTS__VALIDATION" in options
    # list[str] fields are flagged as list kind (JSON editing in the UI).
    assert options["BAYAAN_LANGUAGES__TARGET_LANGUAGES"]["kind"] == "list"
    assert options["BAYAAN_SERMON__ALLOWED_EXTENSIONS"]["kind"] == "list"


def test_secret_flags() -> None:
    assert cp.is_secret_key("BAYAAN_PROVIDERS__WHISPER_API_KEY")
    assert cp.is_secret_key("BAYAAN_PROVIDERS__OLLAMA_CLOUD_API_KEY")
    assert cp.is_secret_key("BAYAAN_AUTH__PASSWORD")
    assert cp.is_secret_key("BAYAAN_AUTH__OPERATOR_TOKEN_SECRET")
    assert cp.is_secret_key("BAYAAN_SECURITY__VISITOR_TOKEN_SECRET")
    assert cp.is_secret_key("BAYAAN_TELEGRAM__BOT_TOKEN")
    # Not secrets: plain TTL integers and ordinary connection settings.
    assert not cp.is_secret_key("BAYAAN_AUTH__TOKEN_TTL_HOURS")
    assert not cp.is_secret_key("BAYAAN_SECURITY__VISITOR_TOKEN_TTL_MINUTES")
    assert not cp.is_secret_key("BAYAAN_DATABASE__URL")
    assert not cp.is_secret_key("BAYAAN_PUBLIC_BASE_URL")


# ---------------------------------------------------------------------------
# .env read / write
# ---------------------------------------------------------------------------


SAMPLE_ENV = (
    "# Bayaan configuration\n"
    "BAYAAN_APP_NAME=Bayaan Server\n"
    "BAYAAN_LANGUAGES__SOURCE_LANGUAGE=ar\n"
    "#BAYAAN_LOG_LEVEL=INFO\n"
    "BAYAAN_AUDIO__CAPTURE_ENABLED=true\n"
)


def _write_sample(tmp_path: Path) -> Path:
    path = tmp_path / ".env"
    path.write_text(SAMPLE_ENV, encoding="utf-8")
    return path


def test_read_env_preserves_comments_but_skips_them(tmp_path: Path) -> None:
    path = _write_sample(tmp_path)
    values = cp.read_env_values(path)
    assert values["BAYAAN_APP_NAME"] == "Bayaan Server"
    assert values["BAYAAN_LANGUAGES__SOURCE_LANGUAGE"] == "ar"
    assert values["BAYAAN_AUDIO__CAPTURE_ENABLED"] == "true"
    assert "BAYAAN_LOG_LEVEL" not in values
def test_apply_env_updates_preserves_untouched_lines(tmp_path: Path) -> None:
    path = _write_sample(tmp_path)
    changed = cp.apply_env_updates(path, {"BAYAAN_APP_NAME": "New Name"})
    assert changed == ["BAYAAN_APP_NAME"]
    text = path.read_text(encoding="utf-8")
    assert "# Bayaan configuration" in text
    assert "BAYAAN_LANGUAGES__SOURCE_LANGUAGE=ar" in text
    assert "#BAYAAN_LOG_LEVEL=INFO" in text
    lines = text.splitlines()
    assert lines[1] == "BAYAAN_APP_NAME=New Name"


def test_apply_env_updates_keeps_existing_active_lines_active(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("BAYAAN_LOG_LEVEL=DEBUG\n", encoding="utf-8")
    # Requirement 3: preserve existing configuration. An ACTIVE line stays
    # active even when its value is changed to the built-in default.
    changed = cp.apply_env_updates(path, {"BAYAAN_LOG_LEVEL": "INFO"})
    assert changed == ["BAYAAN_LOG_LEVEL"]
    assert path.read_text(encoding="utf-8") == "BAYAAN_LOG_LEVEL=INFO\n"

def test_apply_env_updates_commented_lines_stay_commented_on_default(tmp_path: Path) -> None:
    # A COMMENTED line stays commented when it is set to the default; the same
    # line becomes ACTIVE only when the value moves off-default.
    path = tmp_path / ".env"
    path.write_text("#BAYAAN_LOG_LEVEL=WARNING\n", encoding="utf-8")
    changed = cp.apply_env_updates(path, {"BAYAAN_LOG_LEVEL": "INFO"})
    assert changed == ["BAYAAN_LOG_LEVEL"]
    assert path.read_text(encoding="utf-8") == "#BAYAAN_LOG_LEVEL=INFO\n"
    changed = cp.apply_env_updates(path, {"BAYAAN_LOG_LEVEL": "DEBUG"})
    assert changed == ["BAYAAN_LOG_LEVEL"]
    assert path.read_text(encoding="utf-8") == "BAYAAN_LOG_LEVEL=DEBUG\n"

def test_apply_env_updates_appends_new_keys(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("BAYAAN_APP_NAME=Bayaan Server\n", encoding="utf-8")
    changed = cp.apply_env_updates(path, {"BAYAAN_LANGUAGES__SOURCE_LANGUAGE": "fr"})
    assert changed == ["BAYAAN_LANGUAGES__SOURCE_LANGUAGE"]
    text = path.read_text(encoding="utf-8")
    assert "BAYAAN_APP_NAME=Bayaan Server" in text
    assert "BAYAAN_LANGUAGES__SOURCE_LANGUAGE=fr" in text


def test_writes_are_atomic(tmp_path: Path) -> None:
    path = _write_sample(tmp_path)
    cp.apply_env_updates(path, {"BAYAAN_APP_NAME": "Atomic"})
    assert not (tmp_path / ".env.tmp").exists()


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _opt(key: str) -> dict:
    return next(o for o in cp.build_options() if o["key"] == key)


def test_validate_bool() -> None:
    opt = _opt("BAYAAN_AUDIO__CAPTURE_ENABLED")
    assert cp.validate_value(opt, "true") == (True, "true", None)
    assert cp.validate_value(opt, "0") == (True, "false", None)
    assert cp.validate_value(opt, "") == (True, "false", None)
    ok, _, err = cp.validate_value(opt, "not-a-bool")
    assert not ok and err


def test_validate_numbers() -> None:
    opt = _opt("BAYAAN_MAX_ACTIVE_SESSIONS")
    assert cp.validate_value(opt, "6") == (True, "6", None)
    assert not cp.validate_value(opt, "abc")[0]
    opt_f = _opt("BAYAAN_PROVIDERS__MOCK_ASR_CONFIDENCE")
    assert cp.validate_value(opt_f, "0.95") == (True, "0.95", None)
    assert not cp.validate_value(opt_f, "high")[0]


def test_validate_select_and_list() -> None:
    opt = _opt("BAYAAN_AUDIO__CAPTURE_MODE")
    assert cp.validate_value(opt, "browser") == (True, "browser", None)
    assert not cp.validate_value(opt, "telepathy")[0]
    opt_list = _opt("BAYAAN_LANGUAGES__TARGET_LANGUAGES")
    ok, saved, _ = cp.validate_value(opt_list, '["fr", "en"]')
    assert ok and json.loads(saved) == ["fr", "en"]
    ok, saved, _ = cp.validate_value(opt_list, "fr, en")
    assert ok and json.loads(saved) == ["fr", "en"]
    assert not cp.validate_value(opt_list, "not a list")[0]
# ---------------------------------------------------------------------------
# API payload — secrets must never leak
# ---------------------------------------------------------------------------


def test_payload_masks_secrets(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text(
        "BAYAAN_PROVIDERS__OLLAMA_CLOUD_API_KEY=super-secret-value-12345\n"
        "BAYAAN_AUTH__PASSWORD=hunter2\n",
        encoding="utf-8",
    )
    payload = cp.build_options_payload(path)
    serialized = json.dumps(payload)
    assert "super-secret-value-12345" not in serialized
    assert "hunter2" not in serialized
    by_key = {o["key"]: o for o in payload["options"]}
    assert by_key["BAYAAN_PROVIDERS__OLLAMA_CLOUD_API_KEY"]["value"] == ""
    assert by_key["BAYAAN_PROVIDERS__OLLAMA_CLOUD_API_KEY"]["source"] == "env"
    assert by_key["BAYAAN_AUTH__PASSWORD"]["value"] == ""


def test_payload_reports_default_source(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("BAYAAN_APP_NAME=Custom\n", encoding="utf-8")
    payload = cp.build_options_payload(path)
    by_key = {o["key"]: o for o in payload["options"]}
    assert by_key["BAYAAN_APP_NAME"]["value"] == "Custom"
    assert by_key["BAYAAN_APP_NAME"]["source"] == "env"
    # BAYAAN_ENVIRONMENT is not in the file -> default value.
    assert by_key["BAYAAN_ENVIRONMENT"]["source"] == "default"
    assert by_key["BAYAAN_ENVIRONMENT"]["value"] == "development"


# ---------------------------------------------------------------------------
# Launcher
# ---------------------------------------------------------------------------


def test_launcher_script_exists_at_repo_root() -> None:
    launcher = cp.PROJECT_ROOT / "Start Bayaan.bat"
    assert launcher.is_file(), "expected Start Bayaan.bat at the repo root"
    text = launcher.read_text(encoding="utf-8", errors="ignore")
    assert "control_panel" in text.lower()


def test_check_deps_cli_reports_missing_python(monkeypatch) -> None:
    monkeypatch.setattr(cp, "resolve_python", lambda: None)
    rc = cp.main(["--check-deps"])
    assert rc == 1


def test_dump_options_cli_prints_valid_json(capsys) -> None:
    rc = cp.main(["--dump-options"])
    out = capsys.readouterr().out
    assert rc == 0
    parsed = json.loads(out)
    assert len(parsed) > 100
    assert all(opt.get("description") for opt in parsed)


def test_frontend_command_falls_back_to_npm_when_vite_missing(monkeypatch) -> None:
    import tempfile

    monkeypatch.setattr(cp, "resolve_node", lambda: None)
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setattr(cp, "FRONTEND_DIR", Path(tmp))
        cmd = cp.frontend_command("127.0.0.1")
    assert cmd[0] == "npm.cmd"
    assert "--host" in cmd and "127.0.0.1" in cmd


def test_backend_command_uses_factory_import() -> None:
    cmd = cp.backend_command("127.0.0.1")
    assert "bayaan_server.api.app:create_app" in cmd
    assert "--factory" in cmd
    assert "--port" in cmd and "8000" in cmd
    assert "--host" in cmd and "127.0.0.1" in cmd


def test_panel_binds_to_localhost_by_default(tmp_path: Path, monkeypatch) -> None:
    # Requirement 5: the panel itself must always stay on 127.0.0.1.
    monkeypatch.setattr(cp, "PANEL_STATE_FILE", tmp_path / "state.json")
    assert cp.DEFAULT_HOST == "127.0.0.1"
    assert cp.lan_enabled() is False  # LAN access is opt-in, default off
    cp.set_lan_enabled(True)
    assert cp.lan_enabled() is True
    cp.set_lan_enabled(False)
    assert cp.lan_enabled() is False