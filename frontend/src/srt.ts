/**
 * Pure SRT Subtitle Generation and Export Utility.
 * Converts live captions collected during a khutbah into standard SRT format.
 */

export interface SubtitleCue {
  index: number;
  startTime: number; // in milliseconds
  endTime: number;   // in milliseconds
  text: string;
}

/**
 * Format milliseconds to standard SRT timestamp: HH:MM:SS,mmm
 */
export function formatSrtTimestamp(ms: number): string {
  if (ms < 0) ms = 0;
  const hours = Math.floor(ms / 3600000);
  const minutes = Math.floor((ms % 3600000) / 60000);
  const seconds = Math.floor((ms % 60000) / 1000);
  const millis = Math.floor(ms % 1000);

  const pad2 = (n: number) => n.toString().padStart(2, "0");
  const pad3 = (n: number) => n.toString().padStart(3, "0");

  return `${pad2(hours)}:${pad2(minutes)}:${pad2(seconds)},${pad3(millis)}`;
}

/**
 * Build standard SRT string from cue objects.
 */
export function buildSrt(cues: SubtitleCue[]): string {
  if (!cues.length) return "";

  return cues
    .map((cue, idx) => {
      const cueNumber = idx + 1;
      const startStr = formatSrtTimestamp(cue.startTime);
      const endStr = formatSrtTimestamp(cue.endTime);
      return `${cueNumber}\n${startStr} --> ${endStr}\n${cue.text.trim()}\n`;
    })
    .join("\n");
}

/**
 * Convert LiveFeed items to SubtitleCues for a specific language ('ar', 'it', 'en').
 */
export function captionsToCues(
  items: Array<{
    sequence: number;
    arabic: string;
    targets: Record<string, { text: string; state: string }>;
    time: string;
  }>,
  language: string,
  sessionStartTimeMs?: number
): SubtitleCue[] {
  if (!items.length) return [];

  const baseTime = sessionStartTimeMs || (items[0] ? new Date(items[0].time).getTime() : Date.now());
  const cues: SubtitleCue[] = [];

  for (let i = 0; i < items.length; i++) {
    const item = items[i];
    let cueText = "";

    if (language === "ar") {
      cueText = item.arabic;
    } else if (item.targets && item.targets[language]) {
      cueText = item.targets[language].text;
    }

    if (!cueText || cueText.trim() === "") continue;

    const currentItemTime = new Date(item.time).getTime();
    const startTime = Math.max(0, currentItemTime - baseTime);

    // End time is either start of next item, or 4 seconds duration
    let endTime = startTime + 4000;
    if (i < items.length - 1) {
      const nextItemTime = new Date(items[i + 1].time).getTime();
      const nextStart = Math.max(0, nextItemTime - baseTime);
      if (nextStart > startTime) {
        endTime = Math.min(startTime + 6000, nextStart);
      }
    }

    cues.push({
      index: cues.length + 1,
      startTime,
      endTime,
      text: cueText,
    });
  }

  return cues;
}

/**
 * Trigger client-side browser download of SRT file.
 */
export function downloadSrtFile(filename: string, content: string): void {
  const blob = new Blob([content], { type: "text/srt;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  URL.revokeObjectURL(url);
}
