"""PC camera relay package (ADR-0015). See relay.py for the design."""

from bayaan_server.camera.relay import CameraRelay, ViewerLimitError

__all__ = ["CameraRelay", "ViewerLimitError"]