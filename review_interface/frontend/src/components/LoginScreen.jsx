import React, { useState, useEffect } from 'react';
import { C, MONO } from '../tokens';
import TopBar from './TopBar';
import Footer from './Footer';
import { getStats } from '../api';

const REVIEWERS = [
  { name: 'Gaurav',   role: 'L1' },
  { name: 'Yusuf',    role: 'L1' },
  { name: 'Nikhil',   role: 'L1' },
  { name: 'Divyansh', role: 'L1' },
  { name: 'Vineet',   role: 'L1' },
  { name: 'Devarsh',  role: 'L1' },
  { name: 'Amogh',    role: 'L2' },
  { name: 'Locked',   role: 'L2' },   // read-only view of LOCKED sessions only
];

export default function LoginScreen({ onLogin }) {
  const [name,     setName]     = useState('');
  const [focused,  setFocused]  = useState(false);
  const [stats,    setStats]    = useState(null);
  const [btnHover, setBtnHover] = useState(false);

  useEffect(() => {
    getStats().then(setStats).catch(() => {});
  }, []);

  const disabled = !name;

  const handleSubmit = (e) => {
    e.preventDefault();
    if (!disabled) {
      const reviewer = REVIEWERS.find((r) => r.name === name);
      onLogin(name, reviewer?.role || 'L1');
    }
  };

  // Explicit numeric defaults so cells always show 0 rather than "—"
  const pending  = Number(stats?.total_pending  ?? 0);
  const reviewed = Number(stats?.total_reviewed ?? 0);
  const total    = Number(stats?.total_sessions ?? 0);

  const statsCells = [
    {
      label: 'Pending',
      value: pending,
      color: pending > 0 ? C.severeText : C.textPrimary,
    },
    {
      label: 'Reviewed',
      value: reviewed,
      color: reviewed > 0 ? C.accent : C.textPrimary,
    },
    {
      label: 'Total',
      value: total,
      color: C.textPrimary,
    },
  ];

  return (
    <div style={{ height: '100%', display: 'flex', flexDirection: 'column' }}>
      {/* showBadge signals TopBar to display the GT Bharat badge on the login screen;
          TopBar needs to be updated to consume this prop. */}
      <TopBar reviewerName="" showBadge={true} />

      <div style={{
        flex: 1,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        padding: 48,
        background: C.bgPage,
      }}>
        <div style={{
          background: C.bgSurface,
          border: `1px solid ${C.border}`,
          borderRadius: 6,
          padding: '40px 44px',
          width: 400,
          maxWidth: '100%',
        }}>
          {/* Eyebrow */}
          <div style={{
            fontSize: 11,
            fontFamily: MONO,
            textTransform: 'uppercase',
            letterSpacing: '0.08em',
            color: C.accent,
            marginBottom: 8,
          }}>
            AstroTalk · Independent Review
          </div>

          {/* Title */}
          <div style={{
            fontSize: 22,
            fontWeight: 600,
            color: C.textPrimary,
            marginBottom: 6,
          }}>
            Sign in to begin
          </div>

          {/* Subtitle */}
          <div style={{
            fontSize: 13,
            color: C.textSecondary,
            lineHeight: 1.5,
            marginBottom: 28,
          }}>
            Your name will be recorded against each review decision.
          </div>

          {/* Divider */}
          <div style={{ borderTop: `1px solid ${C.border}`, marginBottom: 24 }} />

          <form onSubmit={handleSubmit}>
            {/* Label */}
            <div style={{
              fontSize: 11,
              fontFamily: MONO,
              textTransform: 'uppercase',
              letterSpacing: '0.05em',
              color: C.textSecondary,
              marginBottom: 8,
            }}>
              Reviewer Name
            </div>

            {/* Dropdown */}
            <select
              autoFocus
              value={name}
              onChange={(e) => setName(e.target.value)}
              onFocus={() => setFocused(true)}
              onBlur={() => setFocused(false)}
              style={{
                width: '100%',
                padding: '10px 14px',
                fontSize: 14,
                borderRadius: 5,
                border: `1px solid ${focused ? C.accent : C.border}`,
                background: focused ? C.bgSurface : C.bgMuted,
                color: name ? C.textPrimary : C.textSecondary,
                transition: 'border-color 150ms, background 150ms',
                cursor: 'pointer',
              }}
            >
              <option value="" disabled>Select your name…</option>
              {REVIEWERS.map((r) => (
                <option key={r.name} value={r.name}>{r.name}</option>
              ))}
            </select>

            {/* Stats strip */}
            <div style={{
              display: 'flex',
              border: `1px solid ${C.border}`,
              borderRadius: 5,
              overflow: 'hidden',
              marginTop: 16,
            }}>
              {statsCells.map((cell, i) => (
                <div key={cell.label} style={{
                  flex: 1,
                  padding: '12px 8px',
                  textAlign: 'center',
                  borderRight: i < statsCells.length - 1 ? `1px solid ${C.border}` : 'none',
                  background: C.bgMuted,
                }}>
                  <div style={{
                    fontSize: 20,
                    fontFamily: MONO,
                    fontWeight: 500,
                    color: cell.color,
                  }}>
                    {cell.value}
                  </div>
                  <div style={{ fontSize: 11, color: C.textSecondary, marginTop: 2 }}>
                    {cell.label}
                  </div>
                </div>
              ))}
            </div>

            {/* Submit button */}
            <button
              type="submit"
              disabled={disabled}
              onMouseEnter={() => setBtnHover(true)}
              onMouseLeave={() => setBtnHover(false)}
              style={{
                marginTop: 16,
                width: '100%',
                padding: 12,
                fontSize: 14,
                fontWeight: 500,
                borderRadius: 5,
                border: 'none',
                background: disabled ? '#D4D0C9' : btnHover ? C.accentDark : C.accent,
                color: disabled ? C.textMuted : '#FFFFFF',
                cursor: disabled ? 'not-allowed' : 'pointer',
                transition: 'background 150ms',
              }}
            >
              Start Reviewing →
            </button>
          </form>
        </div>
      </div>

      <Footer />
    </div>
  );
}
