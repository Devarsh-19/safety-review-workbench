import React from 'react';
import { C, MONO } from '../tokens';

export default function TopBar({ reviewerName, reviewerRole }) {
  return (
    <div style={{
      height: 44,
      flexShrink: 0,
      background: C.topbar,
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'space-between',
      padding: '0 20px',
    }}>
      <span style={{
        fontSize: 12,
        fontWeight: 500,
        textTransform: 'uppercase',
        letterSpacing: '0.07em',
        color: C.topbarText,
      }}>
        Content Safety Review Workbench
      </span>

      {reviewerName && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <span style={{ fontSize: 12, fontWeight: 600, fontFamily: MONO, color: C.topbarText }}>
            {reviewerName}{reviewerRole ? ` · ${reviewerRole}` : ''}
          </span>
          <span style={{
            fontSize: 10,
            fontFamily: MONO,
            letterSpacing: '0.03em',
            background: C.accent,
            color: C.accentLight,
            padding: '3px 9px',
            borderRadius: 4,
          }}>
            GT Bharat
          </span>
        </div>
      )}
    </div>
  );
}
