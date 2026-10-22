"""Provider registry and factory (§63, §87 failover chains).

Centralizes provider registration and failover assembly. Keeps mocks pure
and isolated in ``mock.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from bayaan_server.config import Settings
from bayaan_server.metrics import MetricsRegistry
from bayaan_server.ops.alerts import AlertBus
from bayaan_server.providers.failover import (
    FailoverASRProvider,
    FailoverTTSProvider,
    FailoverTranslationProvider,
    ProviderSet,
    build_failover_runners,
)
from bayaan_server.providers.libre import LibreTranslateProvider
from bayaan_server.providers.mock import (
    MockASRProvider,
    MockTTSProvider,
    MockTranslationProvider,
)
from bayaan_server.providers.ollama import OllamaProvider
from bayaan_server.providers.ollama_cloud import OllamaCloudProvider
from bayaan_server.providers.piper import PiperTTSProvider
from bayaan_server.providers.whisper import WhisperASRProvider

if TYPE_CHECKING:
    pass


def build_providers(
    settings: Settings,
    alerts: AlertBus | None = None,
    metrics: MetricsRegistry | None = None,
    now_ms=None,
) -> ProviderSet:
    """§63 provider registry -> §87 failover-wrapped ports.

    Each capability is a chain (primary + optional fallback) guarded by §54
    circuit breakers; the returned ports satisfy the same protocols as every
    adapter, so business logic is unchanged. Alerts/metrics are optional so
    unit tests can build a registry without an alerting stack.
    """
    metrics = metrics or MetricsRegistry()
    alerts = alerts or AlertBus(settings.alerts, metrics)

    factories = {
        "asr": {
            "mock": lambda s: MockASRProvider(s),
            "mock-failing": lambda s: MockASRProvider(s, fail_always=True),
            "whisper": lambda s: WhisperASRProvider(s),
        },
        "translation": {
            "mock": lambda s: MockTranslationProvider(s),
            "mock-failing": lambda s: MockTranslationProvider(s, fail_always=True),
            "libre": lambda s: LibreTranslateProvider(s),
            "ollama": lambda s: OllamaProvider(s),
            "ollama_cloud": lambda s: OllamaCloudProvider(s),
        },
        "tts": {
            "mock": lambda s: MockTTSProvider(s),
            "piper": lambda s: PiperTTSProvider(s),
        },
    }
    runners = build_failover_runners(
        settings, alerts, metrics, factories, now_ms=now_ms
    )
    return ProviderSet(
        asr=FailoverASRProvider(runners["asr"]),
        translation=FailoverTranslationProvider(runners["translation"]),
        tts=FailoverTTSProvider(runners["tts"]),
        runners=runners,
    )
