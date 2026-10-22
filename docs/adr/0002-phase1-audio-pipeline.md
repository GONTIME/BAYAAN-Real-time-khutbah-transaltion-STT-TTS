# ADR-0002: File ingest endpoint as the Phase 1 audio integration surface

**Status:** Accepted

**Context:** §190 requires "ASR live integration" with acceptance "stable
transcript from mixer input". The WebSocket client path belongs to Phase 2
(§191), and real mixer hardware is not available in the development
environment.

**Decision:**
- `LiveIngestService` is the single integration point:
  frames -> EnergyVAD -> ASR -> ArabicNormalizerV1 -> RuleContentDetector
  -> persisted LiveSegment + metrics.
- Audio enters via `AudioSource` implementations: `FileAudioSource` (WAV
  upload, golden-dataset-ready) and `DeviceAudioSource` (real mixer, behind
  optional `sounddevice` extra). Phase 2 WebSocket streams will call the
  same service with frame chunks instead of a file.
- Content detection in Phase 1 is only the conservative rule detector (§65
  step 1): QURAN/HADITH/DUA require explicit surface markers; anything else
  is GENERAL. Reference matching and safety validators are Phase 4 (§193).
- Alignment confidence is stored as 0.0 until the alignment engine exists;
  the field is part of the stable contract now (§64, §166).

**Consequences:**
- End-to-end pipeline is testable without hardware or cloud services.
- Golden dataset clips (§99) can be replayed through `POST /ingest` for
  benchmarking once a real ASR adapter lands.
- No silent format conversion: input must match configured sample
  rate/channels or the request fails with 415 (§184).