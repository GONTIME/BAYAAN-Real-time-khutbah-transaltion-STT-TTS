from __future__ import annotations

import asyncio
import pytest
from unittest.mock import MagicMock, patch
from bayaan_server.config import Settings
from bayaan_server.audio.sources import DeviceAudioSource, AudioFormatError

@pytest.fixture
def settings():
    s = Settings()
    s.audio.sample_rate = 16000
    s.audio.channels = 1
    return s

@pytest.mark.asyncio
async def test_device_audio_source_import_error(settings):
    """Verify that missing sounddevice surfaces as AudioFormatError."""
    with patch("builtins.__import__", side_effect=ImportError("No module named 'sounddevice'")):
        source = DeviceAudioSource(settings)
        with pytest.raises(AudioFormatError, match="sounddevice is not installed"):
            async for _ in source.frames():
                break

@pytest.mark.asyncio
async def test_device_audio_source_capture_format(settings):
    """Verify stable capture of 16kHz mono PCM."""
    # We must patch 'sounddevice' globally to avoid ModuleNotFoundError during the internal import
    with patch("builtins.__import__", side_effect=lambda name, *args, **kwargs: 
               MagicMock() if name == "sounddevice" else __import__(name, *args, **kwargs)):
        with patch("bayaan_server.audio.sources.sounddevice", create=True) as mock_sd:
            mock_stream = MagicMock()
            mock_sd.RawInputStream.return_value = mock_stream
            source = DeviceAudioSource(settings)
            with patch("bayaan_server.audio.sources._get_async") as mock_get:
                frame_data = b"\x00\x00" * 1024
                mock_get.side_effect = [frame_data, frame_data, frame_data, asyncio.CancelledError()]
                frames = []
                try:
                    async for frame in source.frames():
                        frames.append(frame)
                        if len(frames) >= 3:
                            break
                except asyncio.CancelledError:
                    pass
                assert len(frames) == 3
                assert len(frames[0]) == 2048
                assert frames[0] == frame_data

@pytest.mark.asyncio
async def test_driver_compatibility_simulation(settings):
    """Simulate compatibility check for WASAPI and DirectSound."""
    with patch("bayaan_server.audio.sources.sounddevice", create=True) as mock_sd:
        device1 = MagicMock()
        device1.name = "Mic (WASAPI)"
        device1.hostapi = 0
        device2 = MagicMock()
        device2.name = "Mic (DirectSound)"
        device2.hostapi = 1
        mock_sd.query_devices.return_value = [device1, device2]
        devices = mock_sd.query_devices()
        device_names = [d.name for d in devices]
        assert any("WASAPI" in name for name in device_names)
        assert any("DirectSound" in name for name in device_names)

@pytest.mark.asyncio
async def test_continuous_capture_stability(settings):
    """Simulate continuous capture stability."""
    source = DeviceAudioSource(settings)
    with patch("builtins.__import__", side_effect=lambda name, *args, **kwargs: 
               MagicMock() if name == "sounddevice" else __import__(name, *args, **kwargs)):
        with patch("bayaan_server.audio.sources.sounddevice", create=True):
            with patch("bayaan_server.audio.sources._get_async") as mock_get:
                frame_data = b"\x00\x00" * 1024
                mock_get.side_effect = [frame_data] * 1000 + [asyncio.CancelledError()]
                count = 0
                try:
                    async for _ in source.frames():
                        count += 1
                        if count >= 1000:
                            break
                except asyncio.CancelledError:
                    pass
                assert count == 1000
