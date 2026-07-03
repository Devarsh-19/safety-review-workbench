import React, { useState, useEffect, useCallback } from 'react';
import { C, MONO } from '../tokens';
import TopBar from './TopBar';
import Footer from './Footer';
import StatusBadge from './StatusBadge';
import VerdictBadge from './VerdictBadge';
import LoadingSpinner from './LoadingSpinner';
import {
  getAudioSessionDetail,
  saveSpeakerRoles,
  submitAudioSession,
  lockAudioSession,
  unlockAudioSession,
} from '../api';

function formatTime(seconds) {
  const s = Math.round(Number(seconds) || 0);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  const pad = (n) => String(n).padStart(2, '0');
  return `${pad(h)}:${pad(m)}:${pad(sec)}`;
}

const ROLES = ['ASTROLOGER', 'USER'];
const opposite = (role) => (role === 'ASTROLOGER' ? 'USER' : 'ASTROLOGER');

export default function AudioSessionViewer({ sId, reviewerName, reviewerRole, onBack }) {
  const [detail,  setDetail]  = useState(null);
  const [loading, setLoading] = useState(true);
  const [error,   setError]   = useState('');
  const [busy,    setBusy]    = useState(false);
  const [note,    setNote]    = useState('');

  const load = useCallback(() => {
    setLoading(true);
    setError('');
    getAudioSessionDetail(sId)
      .then(setDetail)
      .catch((e) => setError(String(e.message || e)))
      .finally(() => setLoading(false));
  }, [sId]);

  useEffect(() => { load(); }, [load]);

  const session  = detail?.session;
  const segments = detail?.segments || [];
  const flags    = detail?.flags || [];
  const locked   = session?.review_status === 'LOCKED';

  // Distinct raw speaker labels, in order of appearance -> lane 1 and lane 2.
  const speakerLabels = [];
  segments.forEach((seg) => {
    if (seg.speaker && !speakerLabels.includes(seg.speaker)) speakerLabels.push(seg.speaker);
  });
  const [label1, label2] = [speakerLabels[0], speakerLabels[1]];

  const segById = {};
  segments.forEach((seg) => { segById[seg.seg_id] = seg; });

  const flagsForSpeaker = (label) =>
    flags.filter((f) => segById[f.seg_id]?.speaker === label);

  const roleForLane = (laneIdx) =>
    laneIdx === 0 ? session?.speaker1_role : session?.speaker2_role;

  const assignRole = (laneIdx, role) => {
    if (locked || busy) return;
    const s1 = laneIdx === 0 ? role : opposite(role);
    const s2 = laneIdx === 0 ? opposite(role) : role;
    setBusy(true);
    saveSpeakerRoles(sId, s1, s2, reviewerName)
      .then(load)
      .catch((e) => setError(String(e.message || e)))
      .finally(() => setBusy(false));
  };

  const doAction = (fn) => {
    setBusy(true);
    setError('');
    fn()
      .then(load)
      .catch((e) => setError(String(e.message || e)))
      .finally(() => setBusy(false));
  };

  const pauses = (() => {
    try { return JSON.parse(session?.pauses || '[]'); } catch { return []; }
  })();

  const renderLane = (label, laneIdx) => {
    if (!label) return null;
    const laneFlags = flagsForSpeaker(label);
    const role = roleForLane(laneIdx);
    return (
      <div key={label} style={{
        flex: 1,
        background: C.bgSurface,
        border: `1px solid ${C.border}`,
        borderRadius: 6,
        overflow: 'hidden',
        minWidth: 0,
      }}>
        {/* Lane header + role assignment */}
        <div style={{
          padding: '12px 16px',
          borderBottom: `1px solid ${C.border}`,
          background: C.bgMuted,
        }}>
          <div style={{
            fontSize: 12, fontFamily: MONO, textTransform: 'uppercase',
            letterSpacing: '0.05em', color: C.textPrimary, fontWeight: 600,
          }}>
            {role ? `${role} (${label})` : label}
          </div>
          <div style={{ display: 'flex', gap: 6, marginTop: 8, alignItems: 'center' }}>
            <span style={{ fontSize: 11, color: C.textSecondary }}>Speaker is:</span>
            {ROLES.map((r) => {
              const active = role === r;
              return (
                <button
                  key={r}
                  disabled={locked || busy}
                  onClick={() => assignRole(laneIdx, r)}
                  style={{
                    padding: '3px 10px', fontSize: 11, fontFamily: MONO,
                    borderRadius: 3,
                    border: `1px solid ${active ? C.accent : C.border}`,
                    background: active ? C.accentLight : C.bgSurface,
                    color: active ? C.accentDark : C.textSecondary,
                    cursor: locked || busy ? 'not-allowed' : 'pointer',
                  }}
                >
                  {r}
                </button>
              );
            })}
          </div>
        </div>

        {/* Flag list */}
        <div style={{ padding: 12, maxHeight: 480, overflow: 'auto' }}>
          {laneFlags.length === 0 ? (
            <div style={{ fontSize: 12, color: C.textSecondary, padding: 8 }}>
              No flagged timestamps for this speaker.
            </div>
          ) : laneFlags.map((f) => {
            const seg = segById[f.seg_id];
            return (
              <div key={f.flag_id} style={{
                border: `1px solid ${C.flaggedBorder}`,
                background: C.flaggedBg,
                borderRadius: 5,
                padding: '10px 12px',
                marginBottom: 8,
              }}>
                <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
                  <span style={{ fontFamily: MONO, fontSize: 12, fontWeight: 600, color: C.textPrimary }}>
                    {formatTime(seg?.ts_start)} – {formatTime(seg?.ts_end)}
                  </span>
                  <VerdictBadge verdict={f.severity} />
                  <span style={{
                    fontSize: 11, fontFamily: MONO, textTransform: 'uppercase',
                    letterSpacing: '0.04em', color: C.flaggedText,
                  }}>
                    {f.intent}
                  </span>
                  <span style={{ fontSize: 11, fontFamily: MONO, color: C.textSecondary }}>
                    conf {f.conf != null ? Number(f.conf).toFixed(2) : '—'}
                  </span>
                  {seg?.tone && (
                    <span style={{ fontSize: 11, color: C.textSecondary }}>tone: {seg.tone}</span>
                  )}
                </div>
                {f.transcript && (
                  <div style={{ fontSize: 13, color: C.textPrimary, marginTop: 6, lineHeight: 1.5 }}>
                    “{f.transcript}”
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </div>
    );
  };

  return (
    <div style={{ height: '100%', display: 'flex', flexDirection: 'column' }}>
      <TopBar reviewerName={`${reviewerName} · Audio Review`} />

      <div style={{ flex: 1, overflow: 'auto', padding: 24, background: C.bgPage }}>
        <button
          onClick={onBack}
          style={{
            marginBottom: 16, padding: '6px 12px', fontSize: 12,
            borderRadius: 4, border: `1px solid ${C.border}`,
            background: C.bgSurface, color: C.textPrimary, cursor: 'pointer',
          }}
        >
          ← Back to queue
        </button>

        {loading ? <LoadingSpinner /> : !session ? (
          <div style={{ color: C.textSecondary }}>Session not found.</div>
        ) : (
          <>
            {/* Session header */}
            <div style={{
              background: C.bgSurface, border: `1px solid ${C.border}`,
              borderRadius: 6, padding: '14px 18px', marginBottom: 16,
              display: 'flex', alignItems: 'center', gap: 14, flexWrap: 'wrap',
            }}>
              <span style={{ fontSize: 16, fontFamily: MONO, fontWeight: 600 }}>
                Session {session.s_id}
              </span>
              <VerdictBadge verdict={session.overall_verdict} />
              <StatusBadge status={session.review_status} />
              <span style={{ fontSize: 12, color: C.textSecondary }}>
                {session.lang || 'unknown language'} · {segments.length} segments · {flags.length} flags
                {pauses.length > 0 ? ` · ${pauses.length} pauses` : ''}
              </span>
              {session.locked_by && (
                <span style={{ fontSize: 12, color: C.textSecondary }}>
                  locked by {session.locked_by}
                </span>
              )}
            </div>

            {error && (
              <div style={{
                padding: 12, marginBottom: 16, borderRadius: 5,
                background: C.severeBg, border: `1px solid ${C.severeBorder}`,
                color: C.severeText, fontSize: 13,
              }}>
                {error}
              </div>
            )}

            {/* Two speaker lanes */}
            {speakerLabels.length === 0 ? (
              <div style={{ color: C.textSecondary, fontSize: 13 }}>No segments in this session.</div>
            ) : (
              <div style={{ display: 'flex', gap: 16, alignItems: 'flex-start' }}>
                {renderLane(label1, 0)}
                {renderLane(label2, 1)}
              </div>
            )}

            {/* Action bar */}
            <div style={{
              marginTop: 16, background: C.bgSurface,
              border: `1px solid ${C.border}`, borderRadius: 6,
              padding: '14px 18px', display: 'flex', gap: 10, alignItems: 'center',
            }}>
              {!locked && (
                <>
                  <input
                    value={note}
                    onChange={(e) => setNote(e.target.value)}
                    placeholder="Review note (optional)…"
                    style={{
                      flex: 1, padding: '8px 12px', fontSize: 13, borderRadius: 5,
                      border: `1px solid ${C.border}`, background: C.bgMuted,
                      color: C.textPrimary,
                    }}
                  />
                  <button
                    disabled={busy}
                    onClick={() => doAction(() => submitAudioSession(sId, reviewerName, note))}
                    style={{
                      padding: '9px 16px', fontSize: 13, fontWeight: 500,
                      borderRadius: 5, border: 'none',
                      background: busy ? '#D4D0C9' : C.accent, color: '#FFFFFF',
                      cursor: busy ? 'not-allowed' : 'pointer',
                    }}
                  >
                    Submit for Review
                  </button>
                </>
              )}
              {reviewerRole === 'L2' && !locked && (
                <button
                  disabled={busy}
                  onClick={() => doAction(() => lockAudioSession(sId, reviewerName))}
                  style={{
                    padding: '9px 16px', fontSize: 13, fontWeight: 500,
                    borderRadius: 5, border: `1px solid ${C.border}`,
                    background: C.topbar, color: C.topbarText,
                    cursor: busy ? 'not-allowed' : 'pointer',
                  }}
                >
                  Lock Session
                </button>
              )}
              {reviewerRole === 'L2' && locked && (
                <button
                  disabled={busy}
                  onClick={() => doAction(() => unlockAudioSession(sId, reviewerName))}
                  style={{
                    padding: '9px 16px', fontSize: 13, fontWeight: 500,
                    borderRadius: 5, border: `1px solid ${C.border}`,
                    background: C.bgSurface, color: C.textPrimary,
                    cursor: busy ? 'not-allowed' : 'pointer',
                  }}
                >
                  Unlock Session
                </button>
              )}
              {locked && reviewerRole !== 'L2' && (
                <span style={{ fontSize: 13, color: C.textSecondary }}>
                  This session is locked — read only.
                </span>
              )}
            </div>
          </>
        )}
      </div>

      <Footer />
    </div>
  );
}
