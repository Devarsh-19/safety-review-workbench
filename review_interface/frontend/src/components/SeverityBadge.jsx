import React from 'react';
import { C, MONO } from '../tokens';

// Session-level severity auto-derived from the Severity Criteria and stored in
// sessions.astrotalk_severity (chat) / audio_sessions.astrotalk_severity (audio).
// HIGH/MEDIUM reuse the severe/flagged palette; LOW is green; CLEAN (no active
// flags at all) is neutral grey so it reads distinctly from a graded LOW.
const MAP = {
  HIGH:   { bg: C.severeBg,  border: C.severeBorder,  text: C.severeText,  label: 'High' },
  MEDIUM: { bg: C.flaggedBg, border: C.flaggedBorder, text: C.flaggedText, label: 'Medium' },
  LOW:    { bg: C.cleanBg,   border: C.cleanBorder,   text: C.cleanText,   label: 'Low' },
  CLEAN:  { bg: '#F1EFE8',   border: '#D3D1C7',       text: '#444441',     label: 'Clean' },
};

export default function SeverityBadge({ severity, labeled = false }) {
  if (!severity) return <span style={{ color: C.textMuted }}>—</span>;
  const s = MAP[String(severity).toUpperCase()];
  if (!s) return <span style={{ color: C.textMuted }}>—</span>;
  return (
    <span
      title="Auto-derived session severity (Severity Criteria)"
      style={{
        display: 'inline-block',
        padding: '2px 8px',
        borderRadius: 3,
        border: `1px solid ${s.border}`,
        fontSize: 10,
        fontFamily: MONO,
        fontWeight: 500,
        textTransform: 'uppercase',
        letterSpacing: '0.04em',
        background: s.bg,
        color: s.text,
        whiteSpace: 'nowrap',
      }}
    >
      {labeled ? `Severity: ${s.label}` : s.label}
    </span>
  );
}
