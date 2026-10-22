# ADR-0004: Phase 3 sermon preparation decisions

**Status:** Accepted

**Context:** §192 requires sermon document ingestion (DOCX/PDF/TXT/MD),
semantic segmentation, translation drafts, glossary, review UI, and an
immutable published version. Acceptance: "prepared sermon can be published
immutably."

**Decisions:**

1. **DOCX via stdlib zip+XML, no python-docx** (§158 caveat). `word/document.xml`
   is walked in order; tracked deletions (`<w:del>`, `<w:delText>`),
   paragraph-level deletion marks, hidden (`w:vanish`) runs, and comment
   ranges (`comments.xml` is never opened) are excluded. Hyperlink display
   text is kept, targets dropped. Table rows become separate paragraphs
   (§156: tables are not prose).
2. **PDF via pypdf; image-only PDF is a loud 422** (§157). OCR is never
   assumed. If OCR is ever added it requires a sandbox, an Arabic model and
   human review — out of MVP scope.
3. **Immutable publish enforced in the repository** (§41). `PUBLISHED`
   versions reject segment/draft writes and re-publish (HTTP 409). The
   guard protects *content*; the `PUBLISHED → ARCHIVED` lifecycle transition
   remains reachable. Approval provenance set at the APPROVE step persists
   through publish and is only overwritten when a new approver is given —
   no silent edits (§96).
4. **Lifecycle is DRAFT → REVIEW → APPROVED → PUBLISHED (+ARCHIVED)** (§119).
   Publish requires every draft approved first (no unreviewed text goes live).
   The parent sermon rolls up via a dedicated transition, not by reusing the
   version path (the sermon state machine is distinct).
5. **Prepared retrieval is exact normalized match** (§78). Live ASR text
   (`ar_norm_v1`) is matched against the published version's normalized
   segments; an exact hit reuses the approved draft instead of calling the
   translation provider, still passing through the safety gate. Fuzzy/semantic
   matching and the alignment engine stay Phase 4 (§193).
6. **Glossary approval bumps an integer revision** (`g{N}`) stamped as
   `glossary_version` on every draft (§69 provenance, §180 governance). Only
   approved terms influence the review panel's glossary hits.
7. **`mock_asr_text` provider setting** exercises the prepared-retrieval path
   end-to-end without a real ASR backend. Configuration, not business logic
   (§0 rule 4); empty in production.

**Known gap / pilot blocker:** the reviewer console at `/admin` has **no
authentication** yet. §116 wants secure operator sessions; until that lands
the console must only be reachable on the trusted operator network. Tracked
before pilot.

**Consequences:**
- §192 acceptance verified live: upload → segment → drafts → approve →
  REVIEW → APPROVED → PUBLISHED (200); re-publish rejected (409).
- SPA fallback serves `/`, `/admin`, `/r/<CODE>` from the built frontend.
