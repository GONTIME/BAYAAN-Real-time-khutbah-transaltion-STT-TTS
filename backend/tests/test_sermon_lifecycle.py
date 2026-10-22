"""Phase 3 acceptance tests (§192): a prepared sermon can be published
immutably, a live session only accepts PUBLISHED versions, and the
approved draft is reused live via exact retrieval (§78).
"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from .conftest import make_client

# Two Arabic sentences long enough to survive min_segment_chars (30).
AR_TEXT = (
    "الحمد لله رب العالمين والصلاة والسلام على رسول الله خاتم النبيين. "
    "أما بعد فإن تقوى الله نور يهدي القلب إلى الحق والرشاد دائما."
)


def _upload(client: TestClient) -> dict:
    resp = client.post(
        "/api/v1/sermons/upload",
        files={"file": ("khutbah.txt", AR_TEXT.encode("utf-8"), "text/plain")},
        data={"title": "خطبة الجمعة"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _drafts(client: TestClient, version_id: str) -> list[dict]:
    segments = client.get(
        f"/api/v1/sermons/versions/{version_id}/segments"
    ).json()
    out: list[dict] = []
    for seg in segments:
        drafts = client.get(f"/api/v1/sermons/segments/{seg['id']}/drafts").json()
        out.extend(drafts)
    return out


def _to_review_approved(client: TestClient, vid: str) -> None:
    client.post(
        f"/api/v1/sermons/versions/{vid}/status",
        json={"status": "REVIEW", "approved_by": "reviewer"},
    )
    client.post(
        f"/api/v1/sermons/versions/{vid}/status",
        json={"status": "APPROVED", "approved_by": "reviewer"},
    )


def test_upload_creates_draft_version_with_segments(client: TestClient) -> None:
    version = _upload(client)
    assert version["version"] == 1
    assert version["status"] == "DRAFT"
    assert version["content_hash"]

    segments = client.get(
        f"/api/v1/sermons/versions/{version['id']}/segments"
    ).json()
    assert segments, "upload must produce at least one segment"
    assert [s["segment_order"] for s in segments] == list(range(len(segments)))


def test_generate_drafts_covers_every_language(client: TestClient) -> None:
    version = _upload(client)
    resp = client.post(f"/api/v1/sermons/versions/{version['id']}/drafts")
    assert resp.status_code == 200, resp.text
    assert resp.json()["drafts"] >= 2  # default targets it + en
    drafts = _drafts(client, version["id"])
    languages = {d["language"] for d in drafts}
    assert {"it", "en"} <= languages
    # §69: provenance travels on every draft.
    assert all(d["provider"] and d["prompt_version"] for d in drafts)


def test_generate_drafts_provider_outage_is_503_not_500(tmp_path) -> None:
    """§87 SAFE_UNAVAILABLE: when every MT provider in the chain fails (e.g.
    ollama activated but the daemon is down), the console must get a typed
    503 with an actionable message - never an opaque 500 - and no partial
    drafts may be persisted (the failed pass rolls back; §169 re-click is
    idempotent and regenerates)."""
    from bayaan_server.config import DatabaseSettings, ProviderSettings, Settings

    settings = Settings(
        database=DatabaseSettings(url=f"sqlite+aiosqlite:///{tmp_path / 'out.db'}"),
        providers=ProviderSettings(
            translation="mock-failing",  # always raises (§195 injection hook)
            translation_fallback="",  # no fallback -> chain exhausts
        ),
    )
    with make_client(settings) as client:
        version = _upload(client)
        resp = client.post(f"/api/v1/sermons/versions/{version['id']}/drafts")
        assert resp.status_code == 503, resp.text
        detail = resp.json()["detail"]
        assert "translation unavailable" in detail
        assert "BAYAAN_PROVIDERS__TRANSLATION=mock" in detail
        # Rollback: a failed pass writes nothing, so a later healthy re-click
        # sees zero existing drafts (idempotency table stays clean).
        assert _drafts(client, version["id"]) == []


def test_publish_requires_all_drafts_approved(client: TestClient) -> None:
    version = _upload(client)
    vid = version["id"]
    client.post(f"/api/v1/sermons/versions/{vid}/drafts")
    _to_review_approved(client, vid)
    # §41: publishing with unapproved drafts must be refused.
    resp = client.post(
        f"/api/v1/sermons/versions/{vid}/status", json={"status": "PUBLISHED"}
    )
    assert resp.status_code == 409


def test_published_version_is_immutable(client: TestClient) -> None:
    """§192/§41: publish freezes content; drafts cannot be regenerated."""
    version = _upload(client)
    vid = version["id"]
    client.post(f"/api/v1/sermons/versions/{vid}/drafts")
    _to_review_approved(client, vid)
    for draft in _drafts(client, vid):
        client.post(
            f"/api/v1/sermons/drafts/{draft['id']}/approve",
            json={"approved_by": "reviewer"},
        )

    resp = client.post(
        f"/api/v1/sermons/versions/{vid}/status", json={"status": "PUBLISHED"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "PUBLISHED"
    assert resp.json()["approved_by"] == "reviewer"

    regen = client.post(f"/api/v1/sermons/versions/{vid}/drafts")
    assert regen.status_code == 409  # §41 immutable


def test_editing_draft_captures_correction(client: TestClient) -> None:
    """§169: reviewer edit keeps before/after/reason/reviewer/timestamp."""
    version = _upload(client)
    vid = version["id"]
    client.post(f"/api/v1/sermons/versions/{vid}/drafts")
    draft = _drafts(client, vid)[0]

    resp = client.patch(
        f"/api/v1/sermons/drafts/{draft['id']}",
        json={
            "text": "Traduzione rivista e corretta dal revisore umano.",
            "corrected_by": "reviewer",
            "correction_reason": "terminology",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["text"] == "Traduzione rivista e corretta dal revisore umano."
    assert body["corrected_from"] == draft["text"]
    assert body["correction_reason"] == "terminology"
    assert body["corrected_by"] == "reviewer"
    assert body["corrected_at"]
    assert body["approved"] is False  # an edit invalidates prior approval


def test_session_rejects_unpublished_version(client: TestClient) -> None:
    """§1.2: only PUBLISHED prepared sermons are reachable live."""
    version = _upload(client)
    resp = client.post(
        "/api/v1/sessions",
        json={
            "room_id": str(uuid.uuid4()),
            "active_languages": ["it"],
            "sermon_version_id": version["id"],
        },
    )
    assert resp.status_code == 422


def test_review_panel_emits_validation_array(client: TestClient) -> None:
    """§96/§79/§168: the review panel runs the full validator chain per draft
    and serializes an explainable `validation` array. Each entry carries the
    §79 decision, §23 risk/risk_level and the classified warnings (§101).
    """
    version = _upload(client)
    vid = version["id"]
    client.post(f"/api/v1/sermons/versions/{vid}/drafts")
    segments = client.get(f"/api/v1/sermons/versions/{vid}/segments").json()
    segment = segments[0]

    resp = client.get(f"/api/v1/sermons/segments/{segment['id']}/review")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # One validation entry per draft language; every field is JSON-serializable.
    assert "validation" in body
    assert len(body["validation"]) == len(body["drafts"])
    for entry in body["validation"]:
        assert entry["decision"] in {
            "ALLOW", "DISPLAY_ONLY", "WARNING", "FALLBACK", "BLOCK"
        }
        assert entry["risk_level"] in {"NONE", "LOW", "MEDIUM", "HIGH"}
        assert 0.0 <= entry["risk"] <= 1.0
        assert isinstance(entry["warnings"], list)
        for w in entry["warnings"]:
            assert w["severity"] in {"LOW", "MEDIUM", "CRITICAL"}
            assert w["code"] and w["message"]


def test_review_panel_blocks_scripture_without_reference(client: TestClient) -> None:
    """§78/§206: a QURAN segment draft has no approved reference in preparation,
    so the §79 validator returns BLOCK with a scripture_unverified warning -
    surfaced to the reviewer (§96) rather than silently published.
    """
    # §65 rule detector tags QURAN only via the ornate parentheses markers
    # (﴾ ﴿); the basmala text alone would be GENERAL, so wrap the verse.
    quran = (
        "﴿ان مع العسر يسرا﴾ فمن صبر واحتسب كان له أجر عظيم عند ربه "
        "في الدنيا والآخرة وان ذلك لهو الفوز المبين."
    )
    resp = client.post(
        "/api/v1/sermons/upload",
        files={"file": ("q.txt", quran.encode("utf-8"), "text/plain")},
        data={"title": "فاتحة"},
    )
    assert resp.status_code == 201, resp.text
    vid = resp.json()["id"]
    client.post(f"/api/v1/sermons/versions/{vid}/drafts")
    segments = client.get(f"/api/v1/sermons/versions/{vid}/segments").json()
    segment = next(s for s in segments if s["content_type"] == "QURAN")

    body = client.get(f"/api/v1/sermons/segments/{segment['id']}/review").json()
    scripture = [
        v for v in body["validation"] if v["decision"] == "BLOCK"
    ]
    assert scripture, f"expected a BLOCK verdict for QURAN, got {body['validation']}"
    assert any(
        w["code"] == "scripture_unverified" and w["severity"] == "CRITICAL"
        for v in scripture for w in v["warnings"]
    )


def test_generate_drafts_is_idempotent(client: TestClient) -> None:
    """§75/§169: re-clicking Generate drafts fills only gaps - it never
    duplicates or overwrites an existing draft."""
    version = _upload(client)
    vid = version["id"]
    first = client.post(f"/api/v1/sermons/versions/{vid}/drafts").json()
    assert first["created"] >= 2
    assert first["skipped"] == 0

    before = _drafts(client, vid)
    second = client.post(f"/api/v1/sermons/versions/{vid}/drafts").json()
    assert second["created"] == 0
    assert second["skipped"] == first["created"]
    assert second["total"] == first["total"]

    after = _drafts(client, vid)
    assert len(after) == len(before)  # no duplicate rows


def test_approve_all_drafts(client: TestClient) -> None:
    """§96: approve-all approves every draft and records the reviewer."""
    version = _upload(client)
    vid = version["id"]
    client.post(f"/api/v1/sermons/versions/{vid}/drafts")

    resp = client.post(
        f"/api/v1/sermons/versions/{vid}/approve-all",
        json={"approved_by": "reviewer"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["approved"] >= 2

    drafts = _drafts(client, vid)
    assert drafts and all(d["approved"] for d in drafts)
    assert all(d["approved_by"] == "reviewer" for d in drafts)


def test_delete_sermon_removes_it(client: TestClient) -> None:
    """§119: an uploaded (unpublished) sermon can be deleted."""
    version = _upload(client)
    sid = version["sermon_id"]
    client.post(f"/api/v1/sermons/versions/{version['id']}/drafts")

    resp = client.delete(f"/api/v1/sermons/{sid}")
    assert resp.status_code == 204, resp.text

    listing = client.get("/api/v1/sermons").json()
    assert all(s["id"] != sid for s in listing)


def test_delete_refuses_published_sermon(client: TestClient) -> None:
    """§41: a published sermon is immutable and cannot be deleted (409)."""
    version = _upload(client)
    vid = version["id"]
    sid = version["sermon_id"]
    client.post(f"/api/v1/sermons/versions/{vid}/drafts")
    client.post(
        f"/api/v1/sermons/versions/{vid}/approve-all",
        json={"approved_by": "reviewer"},
    )
    _to_review_approved(client, vid)
    client.post(f"/api/v1/sermons/versions/{vid}/status", json={"status": "PUBLISHED"})

    resp = client.delete(f"/api/v1/sermons/{sid}")
    assert resp.status_code == 409, resp.text


def test_delete_draft_removes_only_that_draft(client: TestClient) -> None:
    """§96: a reviewer deletes one draft; siblings stay untouched."""
    version = _upload(client)
    vid = version["id"]
    client.post(f"/api/v1/sermons/versions/{vid}/drafts")
    drafts = _drafts(client, vid)
    assert len(drafts) >= 2
    target = drafts[0]
    survivor = next(d for d in drafts if d["id"] != target["id"])

    resp = client.delete(f"/api/v1/sermons/drafts/{target['id']}")
    assert resp.status_code == 204, resp.text

    remaining = _drafts(client, vid)
    assert all(d["id"] != target["id"] for d in remaining)
    assert any(d["id"] == survivor["id"] for d in remaining)


def test_delete_draft_unknown_id_404(client: TestClient) -> None:
    """Reasoned 404 instead of a silent 200 when the draft is already gone."""
    resp = client.delete(f"/api/v1/sermons/drafts/{uuid.uuid4()}")
    assert resp.status_code == 404, resp.text
    assert "not found" in resp.json()["detail"]


def test_delete_draft_refused_on_published_version(client: TestClient) -> None:
    """§41: a draft inside a PUBLISHED version is immutable -> 409."""
    version = _upload(client)
    vid = version["id"]
    client.post(f"/api/v1/sermons/versions/{vid}/drafts")
    client.post(
        f"/api/v1/sermons/versions/{vid}/approve-all",
        json={"approved_by": "reviewer"},
    )
    _to_review_approved(client, vid)
    client.post(f"/api/v1/sermons/versions/{vid}/status", json={"status": "PUBLISHED"})

    drafts = _drafts(client, vid)
    assert drafts
    resp = client.delete(f"/api/v1/sermons/drafts/{drafts[0]['id']}")
    assert resp.status_code == 409, resp.text


def test_edit_draft_accepts_empty_text(client: TestClient) -> None:
    """§96: clearing the text field with PATCH is a valid reviewer action, so
    the API must accept an empty (but non-null) string rather than 422."""
    version = _upload(client)
    vid = version["id"]
    client.post(f"/api/v1/sermons/versions/{vid}/drafts")
    draft = _drafts(client, vid)[0]

    resp = client.patch(
        f"/api/v1/sermons/drafts/{draft['id']}",
        json={"text": "", "corrected_by": "reviewer", "correction_reason": "remove"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["text"] == ""
    assert resp.json()["approved"] is False

