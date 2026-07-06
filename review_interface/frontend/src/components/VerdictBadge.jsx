import React from 'react';
import { C, MONO } from '../tokens';

// Maps both verdict values (SEVERE/FLAGGED/CLEAN/UNPROCESSED) and
// flag severity values (HIGH/MEDIUM/LOW) to the same colour system.
const MAP = {
  SEVERE:       { bg: C.severeBg,  border: C.severeBorder,  text: C.severeText  },
  HIGH:         { bg: C.severeBg,  border: C.severeBorder,  text: C.severeText  },
  FLAGGED:      { bg: C.flaggedBg, border: C.flaggedBorder, text: C.flaggedText },
  MEDIUM:       { bg: C.flaggedBg, border: C.flaggedBorder, text: C.flaggedText },
  CLEAN:        { bg: C.cleanBg,   border: C.cleanBorder,   text: C.cleanText   },
  LOW:          { bg: C.cleanBg,   border: C.cleanBorder,   text: C.cleanText   },
  UNPROCESSED:  { bg: '#F1EFE8',   border: '#D3D1C7',       text: '#444441'     },
};

const LABELS = {
  UNPROCESSED: 'Unprocessed',
};

export default function VerdictBadge({ verdict }) {
  if (!verdict) return <span style={{ color: C.textMuted }}>—</span>;
  const s = MAP[verdict] || { bg: C.bgStatsrow, border: C.border, text: C.textMuted };
  const label = LABELS[verdict] || verdict;
  return (
    <span style={{
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
    }}>
      {label}
    </span>
  );
}
