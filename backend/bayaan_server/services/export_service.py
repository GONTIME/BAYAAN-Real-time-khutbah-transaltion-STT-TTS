from __future__ import annotations

import datetime
from typing import Protocol

class ExportFormat(Protocol):
    def format(self, index: int, start: datetime.datetime, end: datetime.datetime, text: str) -> str:
        ...

class SrtFormat:
    def format(self, index: int, start: datetime.datetime, end: datetime.datetime, text: str) -> str:
        # SRT format: Index \n HH:MM:SS,mmm --> HH:MM:SS,mmm \n Text \n\n
        def _fmt_time(dt: datetime.datetime) -> str:
            td = dt - datetime.datetime(1970, 1, 1, tzinfo=dt.tzinfo) if dt.tzinfo else dt - datetime.datetime(1970, 1, 1)
            # We just need the time part relative to start of session
            # but since SRT expects absolute time from start of video, 
            # we'll handle the offset in the service.
            return dt.strftime("%H:%M:%S,%f")[:-3]

        return f"{index}\n{_fmt_time(start)} --> {_fmt_time(end)}\n{text}\n\n"

class VttFormat:
    def format(self, index: int, start: datetime.datetime, end: datetime.datetime, text: str) -> str:
        # VTT format: WEBVTT \n\n Index \n HH:MM:SS.mmm --> HH:MM:SS.mmm \n Text \n\n
        def _fmt_time(dt: datetime.datetime) -> str:
            return dt.strftime("%H:%M:%S.%f")[:-3]
        return f"{index}\n{_fmt_time(start)} --> {_fmt_time(end)}\n{text}\n\n"

class ExportService:
    """Handles conversion of sermon segments to SRT/VTT formats (§191)."""
    
    def __init__(self) -> None:
        self._formats = {
            "srt": SrtFormat(),
            "vtt": VttFormat(),
        }

    def export_sermon(self, segments: list, language: str, fmt_type: str, header_note: str = "") -> str:
        """
        Generates the caption file content. 
        segments is expected to be a list of objects with (start, end, approved_draft).
        """
        formatter = self._formats.get(fmt_type.lower())
        if not formatter:
            raise ValueError(f"Unsupported export format: {fmt_type}")

        output = []
        if fmt_type.lower() == "vtt":
            output.append("WEBVTT\n")
            if header_note:
                output.append(f"NOTE {header_note}\n")
            output.append("\n")

        # Use the first segment's start time as the zero-point for SRT/VTT
        if not segments:
            return ""
        
        session_start = segments[0].start # This assumes segments are sorted

        for i, seg in enumerate(segments, 1):
            # Calculate offset relative to session start
            start_offset = seg.start - session_start
            end_offset = seg.end - session_start
            
            # Convert timedeltas to dummy datetimes for the formatter
            start_dt = datetime.datetime(2000, 1, 1) + start_offset
            end_dt = datetime.datetime(2000, 1, 1) + end_offset
            
            # Find the approved draft for the requested language
            draft = next((d for d in seg.drafts if d.language == language and d.approved), None)
            text = draft.text if draft else ""
            
            output.append(formatter.format(i, start_dt, end_dt, text))

        return "".join(output)
