"""Domain model contract tests (§64)."""

from __future__ import annotations

import uuid

import pydantic
import pytest

from bayaan_server.domain.enums import ContentType, SessionStatus
from bayaan_server.domain.models import LiveSegment, LiveSession


def test_live_session_defaults_match_spec() -> None:
    room_id = uuid.uuid4()
    session = LiveSession(room_id=room_id)
    assert session.status is SessionStatus.CREATED
    assert session.started_at is None
    assert session.ended_at is None
    assert session.sermon_version_id is None
    assert session.active_languages == []


def test_live_segment_confidences_are_bounded() -> None:
    session_id = uuid.uuid4()
    with pytest.raises(pydantic.ValidationError):
        LiveSegment(
            session_id=session_id,
            sequence=0,
            raw_arabic="نص",
            normalized_arabic="نص",
            asr_confidence=1.5,  # out of range
            alignment_confidence=0.9,
        )


def test_content_types_are_the_spec_set() -> None:
    assert {c.value for c in ContentType} == {
        "GENERAL", "QURAN", "HADITH", "DUA", "QUOTE",
        "PROPER_NAME", "NUMBER_SENSITIVE", "UNKNOWN",
    }