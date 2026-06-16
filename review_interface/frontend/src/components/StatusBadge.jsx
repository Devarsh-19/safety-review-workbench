import React from 'react';
import { C, MONO } from '../tokens';

const STYLES = {
  PENDING:              { bg: C.bgStatsrow,  border: '#D4D0C9', text: C.textSecondary, label: 'PENDING'              },
  REVIEWED:             { bg: C.accentLight, border: '#9FE1CB', text: C.accentDark,    label: 'REVIEWED'             },
  CONFIRMED:            { bg: C.accentLight, border: '#9FE1CB', text: C.accentDark,    label: 'CONFIRMED'            },
  OVERRIDDEN:           { bg: C.accentLight, border: '#9FE1CB', text: C.accentDark,    label: 'OVERRIDDEN'           },
  SUBMITTED_FOR_REVIEW: { bg: '#EFF6FF',     border: '#B5D4F4', text: '#0C447C',       label: 'SUBMITTED'            },
  NEEDS_FINAL_REVIEW:   { bg: '#FFFBEB',     border: '#FAC775', text: '#854F0B',       label: 'Needs Final Review'   },
  LOCKED:               { bg: '#F1EFE8',     border: '#D3D1C7', text: '#444441',       label: 'LOCKED'               },
};

const DEFAULT = { bg: C.accentLight, border: '#9FE1CB', text: C.accentDark };

export default function StatusBadge({ status }) {
  const s = STYLES[status] ?? { ...DEFAULT, label: status };
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
      {s.label}
    </span>
  );
}
