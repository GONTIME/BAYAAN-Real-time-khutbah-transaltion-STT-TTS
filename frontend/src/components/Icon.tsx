import type { ReactElement } from "react";

/**
 * Minimal stroke-based icon set (24x24 grid, currentColor).
 * Replaces the decorative emoji used across the cockpit so the UI matches
 * the design system (monochrome, theme-aware, consistent cross-platform).
 * Fill-style glyphs (stop / dot / play) switch fill=currentColor.
 */

export type IconName =
  | "mic"
  | "radio"
  | "stop"
  | "dot"
  | "sparkles"
  | "sun"
  | "moon"
  | "smartphone"
  | "monitor"
  | "download"
  | "logout"
  | "zap"
  | "book-open"
  | "scroll"
  | "alert"
  | "check-circle"
  | "lock"
  | "file-text"
  | "flask"
  | "timer"
  | "camera"
  | "video"
  | "bookmark"
  | "volume-2"
  | "volume-x"
  | "maximize"
  | "play"
  | "trash"
  | "mosque";

const PATHS: Record<IconName, ReactElement[]> = {
  mic: [
    <path key="a" d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z" />,
    <path key="b" d="M19 10v2a7 7 0 0 1-14 0v-2" />,
    <line key="c" x1="12" y1="19" x2="12" y2="22" />,
  ],
  radio: [
    <circle key="c" cx="12" cy="12" r="2" />,
    <path key="a" d="M16.24 7.76a6 6 0 0 1 0 8.49" />,
    <path key="b" d="M7.76 7.76a6 6 0 0 0 0 8.49" />,
    <path key="d" d="M19.07 4.93a10 10 0 0 1 0 14.14" />,
    <path key="e" d="M4.93 4.93a10 10 0 0 0 0 14.14" />,
  ],
  stop: [
    <rect key="s" x="5" y="5" width="14" height="14" rx="2" fill="currentColor" stroke="none" />,
  ],
  dot: [
    <circle key="d" cx="12" cy="12" r="5.5" fill="currentColor" stroke="none" />,
  ],
  sparkles: [
    <path key="a" d="M12 3l2.1 6.2 5.9 1.8-5.9 1.8L12 19l-2.1-6.2L4 11l5.9-1.8z" />,
    <path key="b" d="M19 15l.9 2.4 2.1.6-2.1.6-.9 2.4-.9-2.4-2.1-.6 2.1-.6z" fill="currentColor" stroke="none" />,
  ],
  sun: [
    <circle key="c" cx="12" cy="12" r="5" />,
    <path key="a" d="M12 1v2" />,
    <path key="b" d="M12 21v2" />,
    <path key="c2" d="M4.22 4.22l1.42 1.42" />,
    <path key="d2" d="M18.36 18.36l1.42 1.42" />,
    <path key="e2" d="M1 12h2" />,
    <path key="f2" d="M21 12h2" />,
    <path key="g2" d="M4.22 19.78l1.42-1.42" />,
    <path key="h2" d="M18.36 5.64l1.42-1.42" />,
  ],
  moon: [
    <path key="m" d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />,
  ],
  smartphone: [
    <rect key="r" x="5" y="2" width="14" height="20" rx="2" />,
    <line key="l" x1="12" y1="18" x2="12.01" y2="18" />,
  ],
  monitor: [
    <rect key="r" x="2" y="3" width="20" height="14" rx="2" />,
    <line key="a" x1="8" y1="21" x2="16" y2="21" />,
    <line key="b" x1="12" y1="17" x2="12" y2="21" />,
  ],
  download: [
    <path key="d" d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />,
    <polyline key="p" points="7 10 12 15 17 10" />,
    <line key="l" x1="12" y1="15" x2="12" y2="3" />,
  ],
  logout: [
    <path key="d" d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" />,
    <polyline key="p" points="16 17 21 12 16 7" />,
    <line key="l" x1="21" y1="12" x2="9" y2="12" />,
  ],
  zap: [<polygon key="z" points="13 2 3 14 12 14 11 22 21 10 12 10 13 2" />],
  "book-open": [
    <path key="a" d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z" />,
    <path key="b" d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z" />,
  ],
  scroll: [
    <path key="a" d="M19 17V5a2 2 0 0 0-2-2H4" />,
    <path key="b" d="M8 21h12a2 2 0 0 0 2-2v-1a1 1 0 0 0-1-1H11a1 1 0 0 1-1-1v-1a1 1 0 0 1 1-1h9" />,
    <path key="c" d="M6 3a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h2" />,
  ],
  alert: [
    <path key="a" d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />,
    <line key="l" x1="12" y1="9" x2="12" y2="13" />,
    <line key="m" x1="12" y1="17" x2="12.01" y2="17" />,
  ],
  "check-circle": [
    <path key="a" d="M22 11.08V12a10 10 0 1 1-5.93-9.14" />,
    <polyline key="p" points="22 4 12 14.01 9 11.01" />,
  ],
  lock: [
    <rect key="r" x="3" y="11" width="18" height="11" rx="2" />,
    <path key="p" d="M7 11V7a5 5 0 0 1 10 0v4" />,
  ],
  "file-text": [
    <path key="a" d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />,
    <polyline key="p" points="14 2 14 8 20 8" />,
    <line key="l" x1="16" y1="13" x2="8" y2="13" />,
    <line key="m" x1="16" y1="17" x2="8" y2="17" />,
  ],
  flask: [
    <path key="a" d="M10 2v7.5L4.7 19a2 2 0 0 0 1.8 3h11a2 2 0 0 0 1.8-3L14 9.5V2" />,
    <path key="b" d="M8.5 2h7" />,
    <path key="c" d="M7 16h10" />,
  ],
  timer: [
    <circle key="c" cx="12" cy="13" r="8" />,
    <path key="p" d="M12 9v4l2.5 2.5" />,
    <path key="s" d="M9 2h6" />,
  ],
  camera: [
    <path key="a" d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z" />,
    <circle key="c" cx="12" cy="13" r="4" />,
  ],
  video: [
    <polygon key="p" points="23 7 16 12 23 17 23 7" />,
    <rect key="r" x="1" y="5" width="15" height="14" rx="2" />,
  ],
  bookmark: [
    <path key="m" d="M19 21l-7-5-7 5V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z" />,
  ],
  "volume-2": [
    <polygon key="p" points="11 5 6 9 2 9 2 15 6 15 11 19 11 5" />,
    <path key="a" d="M15.54 8.46a5 5 0 0 1 0 7.07" />,
    <path key="b" d="M19.07 4.93a10 10 0 0 1 0 14.14" />,
  ],
  "volume-x": [
    <polygon key="p" points="11 5 6 9 2 9 2 15 6 15 11 19 11 5" />,
    <line key="a" x1="22" y1="9" x2="16" y2="15" />,
    <line key="b" x1="16" y1="9" x2="22" y2="15" />,
  ],
  maximize: [
    <path key="a" d="M8 3H5a2 2 0 0 0-2 2v3" />,
    <path key="b" d="M21 8V5a2 2 0 0 0-2-2h-3" />,
    <path key="c" d="M3 16v3a2 2 0 0 0 2 2h3" />,
    <path key="d" d="M16 21h3a2 2 0 0 0 2-2v-3" />,
  ],
  play: [
    <polygon key="p" points="6 4 20 12 6 20" fill="currentColor" stroke="none" />,
  ],
  trash: [
    <path key="a" d="M3 6h18v2H3z" />,
    <path key="b" d="M5 8h14v8a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V8z" />,
    <path key="c" d="M9 10h2v6H9z" fill="currentColor" stroke="none" />,
    <path key="d" d="M13 10h2v6h-2z" fill="currentColor" stroke="none" />,
  ],
  mosque: [
    <path key="a" d="M12 2l4.5 7h-9z" />,
    <rect key="b" x="4" y="9" width="16" height="7" rx="1" />,
    <path key="c" d="M9.5 9v7h5V9" />,
    <path key="d" d="M6.5 9V4" />,
    <path key="e" d="M5 4h3" />,
    <path key="f" d="M17.5 9V4" />,
    <path key="g" d="M16 4h3" />,
    <path key="h" d="M2 21h20" />,
  ],
};

export function Icon({
  name,
  size = 16,
  className,
}: {
  name: IconName;
  size?: number;
  className?: string;
}) {
  const solid = name === "stop" || name === "dot" || name === "play";
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill={solid ? "currentColor" : "none"}
      stroke={solid ? "none" : "currentColor"}
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      aria-hidden="true"
      focusable="false"
    >
      {PATHS[name]}
    </svg>
  );
}

export default Icon;