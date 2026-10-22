"""Domain enums (BAYAAN_MASTER_SPEC.md §64-§65, §98, §120)."""

from __future__ import annotations

from enum import StrEnum


class SessionStatus(StrEnum):
    """Live session lifecycle (§110: boot != live; §200 start/stop sequence)."""

    CREATED = "CREATED"
    READY = "READY"
    LIVE = "LIVE"
    ENDED = "ENDED"
    FAILED = "FAILED"


class SermonStatus(StrEnum):
    """Sermon data lifecycle (§119: UPLOAD -> ... -> PUBLISHED -> ARCHIVED).

    UPLOAD/PROCESS are transient (a created sermon with no prepared
    version is DRAFT); DELETED happens only under a retention policy.
    """

    DRAFT = "DRAFT"
    REVIEW = "REVIEW"
    APPROVED = "APPROVED"
    PUBLISHED = "PUBLISHED"
    ARCHIVED = "ARCHIVED"


class ContentType(StrEnum):
    """Content classification types (§65)."""

    GENERAL = "GENERAL"
    QURAN = "QURAN"
    HADITH = "HADITH"
    DUA = "DUA"
    QUOTE = "QUOTE"
    PROPER_NAME = "PROPER_NAME"
    NUMBER_SENSITIVE = "NUMBER_SENSITIVE"
    UNKNOWN = "UNKNOWN"


class RiskLevel(StrEnum):
    """Safety risk levels (§101 error classification, §95 review risk)."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class SafetyDecision(StrEnum):
    """Safety engine decision (§0 rule 5: no unsafe auto-broadcast).

    §79 validator outcomes mapped onto the display policy:
    ALLOW        -> normal broadcast.
    DISPLAY_ONLY -> shown but never spoken (TTS blocked) or auto-published
                    (a review warning without a critical breach).
    WARNING      -> a reviewable warning; safe to broadcast but surfaced to
                    the operator (§168).
    FALLBACK      -> below the composite floor: "Translation unavailable"
                    (§92/§134), never a guess.
    BLOCK         -> a critical breach (number/negation/scripture): no
                    broadcast at all (§101, §167).
    """

    ALLOW = "ALLOW"
    DISPLAY_ONLY = "DISPLAY_ONLY"
    WARNING = "WARNING"
    FALLBACK = "FALLBACK"
    BLOCK = "BLOCK"


class RiskLevelName(StrEnum):
    """§23 risk scoring buckets."""

    NONE = "NONE"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class ValidationSeverity(StrEnum):
    """§101 error classification for one validator warning."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    CRITICAL = "CRITICAL"


class VerificationLevel(StrEnum):
    """§42.3 / §76 religious-reference verification tiers.

    Never claim a citation we cannot verify (§0 rule 8): only VERIFIED and
    VERIFIED_NEAR carry an approved reference; REVIEW/UNKNOWN never do.
    """

    VERIFIED = "VERIFIED"
    VERIFIED_NEAR = "VERIFIED_NEAR"
    MANUAL_REVIEW_REQUIRED = "MANUAL_REVIEW_REQUIRED"
    UNKNOWN = "UNKNOWN"


class MatchConfidence(StrEnum):
    """§76/§77 match confidence for scripture/alignment lookup."""

    VERIFIED = "VERIFIED"
    NEAR = "NEAR"
    WEAK = "WEAK"
    NONE = "NONE"


class AlignmentDecision(StrEnum):
    """§77 sermon_aligner outcome."""

    CONFIRMED = "CONFIRMED"
    WAIT = "WAIT"
    FALLBACK = "FALLBACK"


class ErrorCategory(StrEnum):
    """Post-sermon review error categories (§120)."""

    ASR = "ASR"
    ALIGNMENT = "ALIGNMENT"
    TRANSLATION = "TRANSLATION"
    GLOSSARY = "GLOSSARY"
    TTS = "TTS"
    NETWORK = "NETWORK"
    OTHER = "OTHER"


class AuditAction(StrEnum):
    """Audit log events (§98 - the mandated minimum set)."""

    ADMIN_LOGIN = "ADMIN_LOGIN"
    FILE_UPLOADED = "FILE_UPLOADED"
    SERMON_UPLOADED = "SERMON_UPLOADED"
    SERMON_APPROVED = "SERMON_APPROVED"
    SERMON_PUBLISHED = "SERMON_PUBLISHED"
    SERMON_DELETED = "SERMON_DELETED"
    GLOSSARY_UPDATED = "GLOSSARY_UPDATED"
    GLOSSARY_APPROVED = "GLOSSARY_APPROVED"
    PROVIDER_CHANGED = "PROVIDER_CHANGED"
    MODEL_CHANGED = "MODEL_CHANGED"
    SESSION_STARTED = "SESSION_STARTED"
    SESSION_ENDED = "SESSION_ENDED"
    SAFETY_BLOCK = "SAFETY_BLOCK"
    TTS_BLOCK = "TTS_BLOCK"
    DEGRADED = "DEGRADED"
    OPERATOR_OVERRIDE = "OPERATOR_OVERRIDE"