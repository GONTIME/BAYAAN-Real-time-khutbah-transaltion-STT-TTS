"""Outbound operator notifications (ADR-0016 Telegram caption mirror).

Kept separate from ``providers`` (translation/ASR/TTS ports, §63) and from
``ops`` (health/alerts, §195): a mirror is neither a pipeline capability nor an
alert sink. It only reports what the room already published (§0 rule 5).
"""
