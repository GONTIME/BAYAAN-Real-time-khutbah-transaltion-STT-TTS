# ADR-0007: Phase 6 provider resilience (failover, circuit breakers, degraded mode, watchdog, alerts)

**Status:** Accepted

**Context:** §195 Phase 6 hardens the provider-facing runtime so the live
sermon service survives a provider outage. Acceptance: **"simulated provider
failure does not break original service."** Because an Arabic mosque service
cannot simply restart in the middle of a khutba, every resilience mechanism is
bounded and in-process: the original audio path (§178) is never a dependency
of translation, and the control loop never reopens a session (§111).

**Decisions:**

1. **One failover runner per capability, port-preserving.** `providers/
   failover.py::FailoverRunner` implements §87 exactly: primary provider with
   the full `request_timeout_ms` budget, each fallback with the short
   `fallback_timeout_ms` budget, `PROVIDER_SWITCHED` alert when a fallback
   serves, `SAFE_UNAVAILABLE` (`ProviderUnavailableError`) only when every
   provider in the chain is exhausted. A `build_providers` registry returns a
   `ProviderSet` of failover-wrapped ports (`Failover*Provider`) that satisfy
   the same protocols (ADR-0006 decision 1 carried forward), so
   `LiveIngestService`, `TtsService` and `PreparationService` are unchanged in
   spirit - the app configures chains, never business logic.

2. **Circuit breakers are per provider, and never unlimited (§54).** Each
   route carries a pure, clock-injectable `CircuitBreaker` (domain/health.py):
   OPEN after `failure_threshold` consecutive failures, cool-down `cooldown_ms`,
   HALF_OPEN admits exactly one probe, a failed probe re-opens immediately.
   While a primary circuit is OPEN its provider is skipped, so request bursts
   cannot re-hammer a dying adapter; `failure_threshold=1` still tries the
   fallback in the *same* request (§87 - failover once, prove health with a
   probe later).

3. **Degraded mode is derived state with an explicit reason (§57).** `ops/
   health.py::ServiceHealth` tracks capability heartbeats (ASR success,
   translation success, TTS success) and a set of degradation reasons. The
   §57 mode is computed (any ASR/translation reason ⇒ `arabic_only`; any TTS
   reason ⇒ `text_only`; none ⇒ `full`) and `degraded_mode_reason` records the
   join. On `ProviderUnavailableError` the live pipeline degrades per language
   and keeps serving §134 fallbacks - it never crashes or fabricates output;
   on ASR-down (no fallback) phones still receive the public fallback caption
   and `/health` reports `degraded` + `mode=arabic_only`.

4. **Watchdog = heartbeat poll + soft provider reboot (§111).** `ops/
   watchdog.py` runs on `watchdog_interval_ms`; a capability that produced a
   heartbeat and then stayed silent longer than `heartbeat_timeout_ms` is
   tripped: degraded reason recorded, circuit breakers for that chain closed,
   operator alerted. An idle box (no heartbeats ever) is not watched (§110
   boot != live). The watchdog NEVER reopens a session and never broadcasts.

5. **Alerts are log + metrics with a pluggable sink (§195, §179).** `ops/
   alerts.py::AlertBus` emits severity-gated structured alerts
   (`event=alert_*`, `bayaan_alert_<code>_total` counter) and optional sink
   handlers; `AlertsSettings.webhook_url` remains a documented placeholder.
   Alerts report degraded state; they never gate captions.

6. **Every budget is configuration, and the failure is injectable (§185).**
   New `FailoverSettings` (timeouts, threshold, cool-down, heartbeat, interval)
   and `AlertsSettings`; `ProviderSettings` gains fallback names per capability
   plus the `mock-failing` provider (a config-selected adapter that always
   raises a typed `ProviderOutageError`), so the §195 acceptance runs over the
   real app, WebSocket and `/health` without any real outage.

**Consequences:**
- §195 acceptance verified end-to-end: with `asr=mock-failing, asr_fallback=mock`
  captions keep reaching the phone after the primary dies and `/health` shows
  the OPEN circuit; with no fallback the app degrades (arabic_only +
  `degraded_mode_reason`) yet keeps serving §134 fallbacks; a translation
  outage fails over per language.
- Metrics are capability-scoped (`bayaan_provider_failure_<cap>_<name>_total`,
  `bayaan_provider_success_<cap>_<name>_total`, state gauges) so repeated names
  across chains (e.g. `mock`) never collide; route names are made Prometheus-safe.
- Full suite grows to 175 tests (30 new: §54 state machine, §87 runner, §195
  alerts + §111 watchdog, and the live WS acceptance).
- `/health` now reports `mode`, `degraded_reason` and per-chain circuit states;
  preflight gained a `resilience` component.
- Still a pilot blocker (unchanged): admin auth (§116) - ADR-0004.