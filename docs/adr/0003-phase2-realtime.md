# ADR-0003: Phase 2 realtime architecture decisions

**Status:** Accepted

**Context:** §191 requires WebSocket broadcast, room lifecycle, public PWA,
language selection, and live text output on phones.

**Decisions:**

1. **Bidirectional room<->session linking.** Rooms and sessions reference
   each other regardless of creation order; the broker publishes via
   `session.room_id`, so the graph must stay consistent. Sessions may still
   carry a bare (nonexistent) `room_id` for Phase 0 compatibility.
2. **Stateless HMAC visitor tokens** (§116): random, expiring, no identity,
   no server-side storage. Production requires a configured secret -
   preflight FAILs loudly with the dev default (§184).
3. **Disabled safety signals are excluded, never faked** (§166/§167):
   alignment/validation engines do not exist until Phase 4; their composite
   weights renormalize over active signals and their thresholds are not
   enforced. `BAYAAN_SAFETY__ALIGNMENT_ENABLED=true` activates the signal
   when the engine lands.
4. **In-memory rate limiter** (§115): sliding window per category per client
   address. A multi-worker deployment must move it to Redis; tracked for
   Phase 6 (§196 failover work).
5. **Caption fan-out is fire-and-broadcast:** dead WebSocket clients are
   dropped, never allowed to stall the live pipeline (§107).
6. **Backend serves the built SPA** (`frontend/dist`) when present; dev uses
   the Vite proxy so everything stays same-origin for CSP (§114).
7. **Mock provider confidences are configuration** so the full safety path
   (ALLOW and FALLBACK/BLOCK branches) is exercisable in dev/test.

**Consequences:**
- §191 acceptance verified: phone receives live Arabic/target text.
- Public clients only ever see "Translation unavailable" on unsafe output
  (§134); operator-facing reasons go to the audit log (§98/§168).
