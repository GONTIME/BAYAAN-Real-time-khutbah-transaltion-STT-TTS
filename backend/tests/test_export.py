from __future__ import annotations

import asyncio
import pytest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient
from bayaan_server.api.app import create_app
from bayaan_server.config import Settings
from bayaan_server.db.repository import SermonRepository, SermonStatus

@pytest.fixture
def app():
    settings = Settings()
    app = create_app(settings)
    # Manual injection of state.service because lifespan doesn't run in simple TestClient(app)
    # without the context manager.
    from bayaan_server.api.app import ServiceState
    app.state.service = ServiceState(settings)
    return app

@pytest.mark.asyncio
async def test_export_published_sermon(app):
    """Verify that PUBLISHED sermons can be exported to SRT."""
    client = TestClient(app, headers={"Authorization": "Basic YWRtaW46YWRtaW4="})
    
    # We need a published version. We'll mock the repository.
    with patch("bayaan_server.db.repository.SermonRepository.get_version") as mock_get:
        mock_version = MagicMock()
        mock_version.status = SermonStatus.PUBLISHED
        mock_get.return_value = mock_version
        
        with patch("bayaan_server.db.repository.SermonRepository.list_segments") as mock_segs:
            mock_seg = MagicMock()
            mock_seg.id = "seg1"
            mock_seg.segment_order = 0
            mock_segs.return_value = [mock_seg]
            
            with patch("bayaan_server.db.repository.SermonRepository.list_drafts") as mock_drafts:
                mock_draft = MagicMock()
                mock_draft.language = "en"
                mock_draft.approved = True
                mock_draft.text = "Hello world"
                mock_drafts.return_value = [mock_draft]
                
                version_id = "00000000-0000-0000-0000-000000000000"
                response = client.get(f"/api/v1/sermons/versions/{version_id}/export/srt/en")
                
                assert response.status_code == 200
                assert "1" in response.text
                assert "00:00:00,000 --> 00:00:05,000" in response.text
                assert "Hello world" in response.text

@pytest.mark.asyncio
async def test_export_draft_sermon_forbidden(app):
    """Verify that DRAFT sermons cannot be exported."""
    client = TestClient(app, headers={"Authorization": "Basic YWRtaW46YWRtaW4="})
    
    with patch("bayaan_server.db.repository.SermonRepository.get_version") as mock_get:
        mock_version = MagicMock()
        mock_version.status = SermonStatus.DRAFT
        mock_get.return_value = mock_version
        
        version_id = "00000000-0000-0000-0000-000000000000"
        response = client.get(f"/api/v1/sermons/versions/{version_id}/export/srt/en")
        
        assert response.status_code == 403


def test_export_requires_auth(app):
    """BUG-006: export without operator credentials is rejected."""
    from fastapi.testclient import TestClient as _TC
    bare = _TC(app)
    version_id = "00000000-0000-0000-0000-000000000000"
    response = bare.get(f"/api/v1/sermons/versions/{version_id}/export/srt/en")
    assert response.status_code in (401, 403)


def test_export_rejects_bad_format(app):
    """BUG-007: unsupported format is a 422, not a 500."""
    from fastapi.testclient import TestClient as _TC
    client = _TC(app, headers={"Authorization": "Basic YWRtaW46YWRtaW4="})
    version_id = "00000000-0000-0000-0000-000000000000"
    r = client.get(f"/api/v1/sermons/versions/{version_id}/export/docx/en")
    assert r.status_code == 422


def test_export_no_approved_drafts_409(app):
    """BUG-007: language with no approved drafts -> 409, not silent blanks."""
    from unittest.mock import MagicMock, patch
    from fastapi.testclient import TestClient as _TC
    from bayaan_server.db.repository import SermonStatus
    client = _TC(app, headers={"Authorization": "Basic YWRtaW46YWRtaW4="})
    with patch("bayaan_server.db.repository.SermonRepository.get_version") as mg:
        mv = MagicMock(); mv.status = SermonStatus.PUBLISHED; mg.return_value = mv
        with patch("bayaan_server.db.repository.SermonRepository.list_segments") as ms:
            seg = MagicMock(); seg.id = "s1"; seg.segment_order = 0; ms.return_value = [seg]
            with patch("bayaan_server.db.repository.SermonRepository.list_drafts") as md:
                md.return_value = []
                r = client.get("/api/v1/sermons/versions/00000000-0000-0000-0000-000000000000/export/srt/fr")
                assert r.status_code == 409
