import React from 'react';
import { C, MONO } from '../tokens';

/**
 * Parse a has_video value (from the DB — may be boolean, string, or integer)
 * into a tri-state: true / false / null (unknown).
 */
export function getHasVideoState(value) {
  if (value === null || value === undefined || value === '') return null;
  if (typeof value === 'string') {
    const normalized = value.trim().toLowerCase();
    if (!normalized) return null;
    if (['true', '1', 'yes'].includes(normalized)) return true;
    if (['false', '0', 'no'].includes(normalized)) return false;
  }
  return Boolean(value);
}

/**
 * Badge showing whether the source media contains a video stream.
 * @param {{ value: any, compact?: boolean }} props
 *   compact=true uses short labels (Yes/No/Unknown) for table cells;
 *   compact=false (default) uses full labels (Video Present/Audio Only/Video Unknown).
 */
export default function HasVideoBadge({ value, compact = false }) {
  const hasVideo = getHasVideoState(value);
  const label = compact
    ? (hasVideo == null ? 'Unknown' : hasVideo ? 'Yes' : 'No')
    : (hasVideo == null ? 'Video Unknown' : hasVideo ? 'Video Present' : 'Audio Only');
  const palette = hasVideo == null
    ? { bg: C.bgMuted, border: C.border, text: C.textSecondary }
    : hasVideo
      ? { bg: C.flaggedBg, border: C.flaggedBorder, text: C.flaggedText }
      : { bg: C.cleanBg, border: C.cleanBorder, text: C.cleanText };

  return (
    <span style={{
      display: 'inline-flex',
      alignItems: 'center',
      justifyContent: 'center',
      minWidth: compact ? 44 : undefined,
      padding: compact ? '2px 8px' : '3px 10px',
      borderRadius: 999,
      border: `1px solid ${palette.border}`,
      background: palette.bg,
      color: palette.text,
      fontSize: 11,
      fontFamily: MONO,
      textTransform: 'uppercase',
      letterSpacing: '0.04em',
    }}>
      {label}
    </span>
  );
}
