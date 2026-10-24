/**
 * Curated target-language catalog (ADR-0017) — the frontend mirror of
 * `backend/bayaan_server/domain/languages.py`.
 *
 * Why a mirror at all: the room page must label a language the moment the
 * server names it, even before/without a diagnostics round-trip. The catalog is
 * tiny and stable, and the server is still the authority — `/api/v1/diagnostics/
 * system-summary` returns the configured targets plus this catalog, and the
 * pickers use that when it is available. An unknown code always falls back to
 * the raw code, never to a guess (§0 rule 8).
 */

export type LanguageInfo = {
  code: string;
  /** Endonym: what the reader calls their own language. */
  label: string;
  /** English name, for operator-facing pickers and tooltips. */
  english: string;
};

export const SOURCE_LANGUAGE = "ar";

/** Same order as the backend catalog: the default targets first. */
export const LANGUAGE_CATALOG: LanguageInfo[] = [
  { code: "ar", label: "العربية", english: "Arabic" },
  { code: "it", label: "Italiano", english: "Italian" },
  { code: "en", label: "English", english: "English" },
  { code: "fr", label: "Français", english: "French" },
  { code: "ur", label: "اردو", english: "Urdu" },
  { code: "id", label: "Bahasa Indonesia", english: "Indonesian" },
  { code: "bn", label: "বাংলা", english: "Bengali" },
  { code: "fa", label: "فارسی", english: "Persian" },
  { code: "ps", label: "پښتو", english: "Pashto" },
  { code: "tr", label: "Türkçe", english: "Turkish" },
  { code: "sw", label: "Kiswahili", english: "Swahili" },
  { code: "de", label: "Deutsch", english: "German" },
  { code: "es", label: "Español", english: "Spanish" },
];

/** Languages the admin can enable as targets (the source is never a target). */
export const TARGET_LANGUAGE_CATALOG: LanguageInfo[] = LANGUAGE_CATALOG.filter(
  (entry) => entry.code !== SOURCE_LANGUAGE
);

const BY_CODE = new Map(LANGUAGE_CATALOG.map((entry) => [entry.code, entry]));

/** Endonym for a code; an unknown code stays the code itself. */
export function languageLabel(code: string): string {
  return BY_CODE.get(code)?.label ?? code;
}

/** English name for a code; falls back to the code. */
export function languageEnglishName(code: string): string {
  return BY_CODE.get(code)?.english ?? code;
}
