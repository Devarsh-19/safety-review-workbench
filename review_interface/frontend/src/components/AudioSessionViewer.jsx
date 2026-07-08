import React, { useState, useEffect, useCallback, useRef } from 'react';
import Hls from 'hls.js';
import { C, MONO } from '../tokens';
import TopBar from './TopBar';
import Footer from './Footer';
import StatusBadge from './StatusBadge';
import VerdictBadge from './VerdictBadge';
import LoadingSpinner from './LoadingSpinner';
import HasVideoBadge, { getHasVideoState } from './HasVideoBadge';
import {
  getAudioSessionDetail,
  saveSpeakerRoles,
  submitAudioSession,
  lockAudioSession,
  unlockAudioSession,
  confirmAudioFlag,
  amendAudioFlag,
  dismissAudioFlag,
  undismissAudioFlag,
  confirmAllAudioFlags,
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
const SEVERITIES = ['RED', 'AMBER'];
const INTENT_TAXONOMY = [
  "NSFW",
  "NSFW_EXPLICIT",
  "NSFW_GROOMING",
  "NSFW_APPEARANCE",
  "CSAM_RISK",
  "FINANCIAL_SOLICITATION",
  "IDENTITY_FRAUD",
  "ABUSIVE_LANGUAGE",
  "HATE_SPEECH",
  "FAKE_REMEDIES",
  "UNAUTHORIZED_MEDICAL_ADVICE",
  "SELF_HARM",
  "VIOLENCE",
  "INSTIGATION",
  "OFF_PLATFORM_SOLICITATION",
  "PERSONAL_DATA_COLLECTION",
  "FEAR_MANIPULATION",
  "COMPETITOR_PROMOTION"
];

export default function AudioSessionViewer({ sId, sessionList, reviewerName, reviewerRole, onBack, onNavigate }) {
  const [detail, setDetail] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState('');
  const [playerError, setPlayerError] = useState('');
  const [editingFlag, setEditingFlag] = useState(null);   // flag_id being edited
  const [dismissingFlagId, setDismissingFlagId] = useState(null);
  const [dismissReason, setDismissReason] = useState('');
  const [editIntent, setEditIntent] = useState('');
  const [editSeverity, setEditSeverity] = useState('RED');
  const [editReasoning, setEditReasoning] = useState('');

  // Adjacent-session navigation, same as the chat viewer: no status/verdict
  // filtering — Previous/Next walk the queue list as displayed.
  const currentIndex = sessionList?.findIndex((r) => r.s_id === sId) ?? -1;
  const prevIndex = currentIndex > 0 ? currentIndex - 1 : -1;
  const nextIndex =
    currentIndex >= 0 && sessionList && currentIndex < sessionList.length - 1
      ? currentIndex + 1
      : -1;

  const hasPrev = prevIndex !== -1;
  const hasNext = nextIndex !== -1;

  const goToPrev = () => { if (hasPrev && onNavigate) onNavigate(sessionList[prevIndex].s_id); };
  const goToNext = () => { if (hasNext && onNavigate) onNavigate(sessionList[nextIndex].s_id); };
  const audioRef = useRef(null);
  const hlsRef = useRef(null);

  // Refreshes after actions keep the current content (and the playing audio
  // element!) mounted — the full-screen spinner only shows on first load.
  // Unmounting <audio> mid-session killed playback: the HLS attach effect
  // keys on audioUrl, which doesn't change across refreshes.
  const load = useCallback(() => {
    setLoading(true);
    setError('');
    getAudioSessionDetail(sId)
      .then(setDetail)
      .catch((e) => setError(String(e.message || e)))
      .finally(() => setLoading(false));
  }, [sId]);

  useEffect(() => { load(); }, [load]);

  const session = detail?.session;
  const segments = detail?.segments || [];
  const flags = detail?.flags || [];
  const locked = session?.review_status === 'LOCKED';
  const isSubmitted = session?.review_status === 'SUBMITTED_FOR_REVIEW';
  const isReviewed = session?.review_status === 'REVIEWED';
  const flagsEditable = !locked && (reviewerRole === 'L2' ? true : session?.review_status === 'PENDING');
  const readOnly = !flagsEditable;
  const audioUrl = session?.audio_url;

  // Attach the HLS (.m3u8) stream to the <audio> element. Safari plays HLS
  // natively; everywhere else hls.js does the demuxing via MediaSource.
  useEffect(() => {
    const audio = audioRef.current;
    setPlayerError('');
    if (!audio || !audioUrl) return undefined;

    if (audio.canPlayType('application/vnd.apple.mpegurl')) {
      audio.src = audioUrl;
    } else if (Hls.isSupported()) {
      const hls = new Hls();
      hls.loadSource(audioUrl);
      hls.attachMedia(audio);
      hls.on(Hls.Events.ERROR, (_evt, data) => {
        if (data.fatal) {
          setPlayerError(`Audio stream error (${data.type}) — check the URL is reachable and allows CORS.`);
          hls.destroy();
        }
      });
      hlsRef.current = hls;
    } else {
      setPlayerError('This browser cannot play HLS audio.');
    }

    return () => {
      if (hlsRef.current) { hlsRef.current.destroy(); hlsRef.current = null; }
      audio.removeAttribute('src');
      audio.load();
    };
  }, [audioUrl, session?.has_video]);

  const seekTo = (seconds) => {
    const audio = audioRef.current;
    if (!audio || !audioUrl) return;
    audio.currentTime = Math.max(0, Number(seconds) || 0);
    audio.play().catch(() => { });
  };

  // Distinct raw speaker labels, in order of appearance. Every speaker gets a
  // lane; the first two are role-assignable (the data model supports exactly
  // two roles), extra speakers render read-only-role lanes.
  const speakerLabels = [];
  segments.forEach((seg) => {
    if (seg.speaker && !speakerLabels.includes(seg.speaker)) speakerLabels.push(seg.speaker);
  });

  const segById = {};
  segments.forEach((seg) => { segById[seg.seg_id] = seg; });

  // Active flags = amendment rows + originals without an amendment
  // (same model as chat review; amended originals stay as audit history).
  const amendedParents = new Set(
    flags.filter((f) => f.parent_flag_id != null).map((f) => f.parent_flag_id)
  );
  const activeFlags = flags.filter(
    (f) => f.parent_flag_id != null || !amendedParents.has(f.flag_id)
  );
  const unactionedCount = activeFlags.filter((f) => f.status !== 'CONFIRMED' && f.status !== 'DISMISSED').length;

  const byStartTime = (a, b) => {
    const aStart = Number(a.ts_start ?? segById[a.seg_id]?.ts_start ?? Number.MAX_SAFE_INTEGER);
    const bStart = Number(b.ts_start ?? segById[b.seg_id]?.ts_start ?? Number.MAX_SAFE_INTEGER);
    return aStart - bStart;
  };

  const flagsForSpeaker = (label) =>
    activeFlags.filter((f) => segById[f.seg_id]?.speaker === label).sort(byStartTime);

  // Flags whose seg_id is NULL or points at a segment that no longer exists
  // (possible after re-ingest replaces segments while preserving reviewer
  // flags). They MUST stay visible and actionable — they count toward the
  // submit gate, so hiding them would block submission with no way out.
  const orphanFlags = activeFlags
    .filter((f) => !segById[f.seg_id]?.speaker)
    .sort(byStartTime);

  const startEdit = (f) => {
    setEditingFlag(f.flag_id);
    setEditIntent(f.intent || '');
    // Normalize legacy severity values (HIGH/SEVERE→RED, MEDIUM/FLAGGED→AMBER)
    const sev = (f.severity || '').toUpperCase();
    const normalizedSev = ['RED', 'SEVERE', 'HIGH'].includes(sev) ? 'RED' : 'AMBER';
    setEditSeverity(normalizedSev);
    setEditReasoning('');
  };

  const saveEdit = () => {
    if (!editIntent.trim()) return;
    doAction(() => amendAudioFlag(editingFlag, {
      intent: editIntent.trim().toUpperCase().replace(/\s+/g, '_'),
      severity: editSeverity,
      reasoning: editReasoning,
      reviewer_id: reviewerName,
    }).then(() => setEditingFlag(null)));
  };

  const confirmDismiss = (f) => {
    const note = dismissReason.trim() || 'Dismissed as false detection';
    doAction(() => dismissAudioFlag(f.flag_id, reviewerName, note).then(() => {
      setDismissingFlagId(null);
      setDismissReason('');
    }));
  };

  const undoDismiss = (f) => {
    doAction(() => undismissAudioFlag(f.flag_id, reviewerName));
  };

  const roleForLane = (laneIdx) =>
    laneIdx === 0 ? session?.speaker1_role : session?.speaker2_role;

  const assignRole = (laneIdx, role) => {
    if (readOnly || busy) return;
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

  // Sentinel lane for flags that can't be attributed to a rendered speaker.
  const UNASSIGNED_LANE = '__unassigned__';

  const renderLane = (label, laneIdx) => {
    if (!label) return null;
    const isOrphanLane = label === UNASSIGNED_LANE;
    const laneFlags = isOrphanLane ? orphanFlags : flagsForSpeaker(label);
    if (isOrphanLane && laneFlags.length === 0) return null;
    const roleAssignable = !isOrphanLane && laneIdx < 2;
    const role = roleAssignable ? roleForLane(laneIdx) : null;
    return (
      <div key={label} style={{
        flex: 1,
        background: C.bgSurface,
        border: `1px solid ${isOrphanLane ? C.flaggedBorder : C.border}`,
        borderRadius: 6,
        overflow: 'hidden',
        minWidth: 320,
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
            {isOrphanLane ? 'Unassigned flags' : role ? `${role} (${label})` : label}
          </div>
          {isOrphanLane ? (
            <div style={{ fontSize: 11, color: C.textSecondary, marginTop: 6 }}>
              These flags reference no (or a removed) speaker segment. They still
              require action before the session can be submitted.
            </div>
          ) : roleAssignable ? (
            <div style={{ display: 'flex', gap: 6, marginTop: 8, alignItems: 'center' }}>
              <span style={{ fontSize: 11, color: C.textSecondary }}>Speaker is:</span>
              {ROLES.map((r) => {
                const active = role === r;
                return (
                  <button
                    key={r}
                    disabled={readOnly || busy}
                    onClick={() => assignRole(laneIdx, r)}
                    style={{
                      padding: '3px 10px', fontSize: 11, fontFamily: MONO,
                      borderRadius: 3,
                      border: `1px solid ${active ? C.accent : C.border}`,
                      background: active ? C.accentLight : C.bgSurface,
                      color: active ? C.accentDark : C.textSecondary,
                      cursor: readOnly || busy ? 'not-allowed' : 'pointer',
                    }}
                  >
                    {r}
                  </button>
                );
              })}
            </div>
          ) : (
            <div style={{ fontSize: 11, color: C.textSecondary, marginTop: 6 }}>
              Additional speaker — roles can only be assigned to the first two speakers.
            </div>
          )}
        </div>

        {/* Flag list */}
        <div style={{ padding: 12, maxHeight: 480, overflow: 'auto' }}>
          {laneFlags.length === 0 ? (
            <div style={{ fontSize: 12, color: C.textSecondary, padding: 8 }}>
              No flagged timestamps for this speaker.
            </div>
          ) : laneFlags.map((f) => {
            const seg = segById[f.seg_id];
            const confirmed = f.status === 'CONFIRMED';
            const dismissed = f.status === 'DISMISSED';
            const isEditing = editingFlag === f.flag_id;
            const isDismissing = dismissingFlagId === f.flag_id;
            const isSevere = f.severity === 'SEVERE' || f.severity === 'HIGH' || f.severity === 'RED';
            const isFlagged = f.severity === 'FLAGGED' || f.severity === 'MEDIUM' || f.severity === 'AMBER';
            const tsStart = f.ts_start ?? seg?.ts_start;
            const tsEnd = f.ts_end ?? seg?.ts_end;
            const seekable = !!audioUrl && tsStart != null;
            return (
              <div key={f.flag_id} style={{
                border: dismissed ? `1px dashed ${C.border}`
                  : `1px solid ${confirmed ? (isSevere ? C.severeBorder : isFlagged ? C.flaggedBorder : C.cleanBorder) : C.flaggedBorder}`,
                background: dismissed ? C.bgMuted
                  : (confirmed ? (isSevere ? C.severeBg : isFlagged ? C.flaggedBg : C.cleanBg) : C.flaggedBg),
                borderRadius: 5,
                padding: '10px 12px',
                marginBottom: 8,
                opacity: dismissed ? 0.75 : 1,
              }}>
                <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
                  <button
                    onClick={() => seekable && seekTo(tsStart)}
                    disabled={!seekable}
                    title={!audioUrl ? 'No recording attached'
                      : tsStart == null ? 'No timestamp on this flag'
                        : 'Play from this timestamp'}
                    style={{
                      fontFamily: MONO, fontSize: 12, fontWeight: 600,
                      color: seekable ? C.accentDark : C.textPrimary,
                      background: 'none', border: 'none', padding: 0,
                      cursor: seekable ? 'pointer' : 'default',
                      textDecoration: seekable ? 'underline' : 'none',
                    }}
                  >
                    {seekable ? '▶ ' : ''}
                    {tsStart == null ? 'no timestamp' : `${formatTime(tsStart)} – ${formatTime(tsEnd)}`}
                  </button>
                  <VerdictBadge verdict={f.severity} />
                  <span style={{
                    fontSize: 11, fontFamily: MONO, textTransform: 'uppercase',
                    letterSpacing: '0.04em', color: confirmed ? C.cleanText : C.flaggedText,
                  }}>
                    {f.intent}
                  </span>
                  <span style={{ fontSize: 11, fontFamily: MONO, color: C.textSecondary }}>
                    conf {f.conf != null ? Number(f.conf).toFixed(2) : '—'}
                  </span>
                  {seg?.tone && (
                    <span style={{ fontSize: 11, color: C.textSecondary }}>tone: {seg.tone}</span>
                  )}
                  {f.source === 'MANUAL' && (
                    <span
                      title={f.created_by ? `Edited by ${f.created_by}` : 'Edited'}
                      style={{
                        fontSize: 10, fontFamily: MONO, padding: '1px 6px', borderRadius: 3,
                        background: C.manualBg, border: `1px solid ${C.manualBorder}`, color: C.manualText,
                      }}
                    >
                      EDITED{f.created_by ? ` · ${f.created_by}` : ''}
                    </span>
                  )}
                  {confirmed && (
                    <span
                      title={f.confirmed_at ? `Confirmed at ${f.confirmed_at}` : 'Confirmed'}
                      style={{
                        fontSize: 10, fontFamily: MONO, padding: '1px 6px', borderRadius: 3,
                        background: C.accentLight, border: '1px solid #9FE1CB', color: C.accentDark,
                      }}
                    >
                      CONFIRMED{f.confirmed_by ? ` · ${f.confirmed_by}` : ''}
                    </span>
                  )}
                  {dismissed && (
                    <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
                      <span
                        style={{
                          fontSize: 10, fontFamily: MONO, padding: '1px 6px', borderRadius: 3,
                          background: '#EEEEEE', border: `1px solid ${C.border}`, color: C.textSecondary,
                        }}
                      >
                        DISMISSED
                      </span>
                      {!readOnly && (
                        <button
                          disabled={busy}
                          onClick={() => undoDismiss(f)}
                          style={{
                            fontSize: 10, fontFamily: MONO, padding: '1px 6px', borderRadius: 3,
                            background: C.bgSurface, border: `1px solid ${C.border}`, color: C.textPrimary,
                            cursor: busy ? 'not-allowed' : 'pointer',
                          }}
                        >
                          Undo
                        </button>
                      )}
                    </div>
                  )}
                </div>
                {f.transcript && (
                  <div style={{ fontSize: 13, color: C.textPrimary, marginTop: 6, lineHeight: 1.5 }}>
                    “{f.transcript}”
                  </div>
                )}
                {f.reasoning && (
                  <div style={{ fontSize: 11, color: C.textSecondary, marginTop: 4 }}>
                    Note: {f.reasoning}
                  </div>
                )}

                {/* Flag actions — confirm / edit / dismiss (hidden when readOnly) */}
                {!readOnly && !isEditing && !dismissed && !isDismissing && (
                  <div style={{ display: 'flex', gap: 6, marginTop: 8 }}>
                    {!confirmed && (
                      <button
                        disabled={busy}
                        onClick={() => doAction(() => confirmAudioFlag(f.flag_id, reviewerName))}
                        style={{
                          padding: '4px 10px', fontSize: 11, fontFamily: MONO, borderRadius: 3,
                          border: `1px solid ${C.accent}`, background: C.accent, color: '#FFFFFF',
                          cursor: busy ? 'not-allowed' : 'pointer',
                        }}
                      >
                        ✓ Confirm
                      </button>
                    )}
                    <button
                      disabled={busy}
                      onClick={() => startEdit(f)}
                      style={{
                        padding: '4px 10px', fontSize: 11, fontFamily: MONO, borderRadius: 3,
                        border: `1px solid ${C.border}`, background: C.bgSurface, color: C.textPrimary,
                        cursor: busy ? 'not-allowed' : 'pointer',
                      }}
                    >
                      Edit
                    </button>
                    <button
                      disabled={busy}
                      onClick={() => { setDismissingFlagId(f.flag_id); setDismissReason(''); }}
                      style={{
                        padding: '4px 10px', fontSize: 11, fontFamily: MONO, borderRadius: 3,
                        border: `1px solid ${C.severeBorder}`, background: C.bgSurface, color: C.severeText,
                        cursor: busy ? 'not-allowed' : 'pointer',
                      }}
                    >
                      Dismiss
                    </button>
                  </div>
                )}

                {/* Inline dismiss confirmation */}
                {!readOnly && isDismissing && (
                  <div style={{
                    marginTop: 8, padding: 10, borderRadius: 4,
                    background: '#FEF2F2', border: `1px solid ${C.severeBorder}`,
                  }}>
                    <div style={{ fontSize: 13, color: '#A32D2D', marginBottom: 8 }}>
                      Dismiss this flag? This marks it as a false detection.
                    </div>
                    <input
                      value={dismissReason}
                      onChange={(e) => setDismissReason(e.target.value)}
                      placeholder="Reason (optional — logged to the audit trail)…"
                      style={{
                        width: '100%', boxSizing: 'border-box', padding: '6px 8px',
                        fontSize: 12, borderRadius: 4, border: `1px solid ${C.border}`,
                        marginBottom: 8, background: '#FFFFFF',
                      }}
                    />
                    <div style={{ display: 'flex', gap: 6 }}>
                      <button
                        disabled={busy}
                        onClick={() => confirmDismiss(f)}
                        style={{
                          padding: '6px 12px', fontSize: 12, fontWeight: 500, borderRadius: 3,
                          border: 'none', background: busy ? '#D4D0C9' : '#A32D2D', color: '#FFFFFF',
                          cursor: busy ? 'not-allowed' : 'pointer',
                        }}
                      >
                        {busy ? '…' : 'Confirm Dismiss'}
                      </button>
                      <button
                        disabled={busy}
                        onClick={() => setDismissingFlagId(null)}
                        style={{
                          padding: '6px 12px', fontSize: 12, fontWeight: 500, borderRadius: 3,
                          border: `1px solid ${C.border}`, background: '#FFFFFF', color: C.textPrimary,
                          cursor: busy ? 'not-allowed' : 'pointer',
                        }}
                      >
                        Cancel
                      </button>
                    </div>
                  </div>
                )}

                {/* Inline edit form — saves as an amendment, resets to unconfirmed */}
                {!readOnly && isEditing && (
                  <div style={{
                    marginTop: 8, padding: 10, borderRadius: 4,
                    background: C.bgSurface, border: `1px solid ${C.border}`,
                  }}>
                    <div style={{ display: 'flex', gap: 6, marginBottom: 6 }}>
                      <select
                        value={editIntent}
                        onChange={(e) => setEditIntent(e.target.value)}
                        style={{
                          flex: 1, padding: '6px 8px', fontSize: 12, fontFamily: MONO,
                          borderRadius: 4, border: `1px solid ${C.border}`, cursor: 'pointer',
                        }}
                      >
                        <option value="" disabled>Select Intent</option>
                        {INTENT_TAXONOMY.map((i) => <option key={i} value={i}>{i}</option>)}
                      </select>
                      <select
                        value={editSeverity}
                        onChange={(e) => setEditSeverity(e.target.value)}
                        style={{
                          padding: '6px 8px', fontSize: 12, fontFamily: MONO,
                          borderRadius: 4, border: `1px solid ${C.border}`, cursor: 'pointer',
                        }}
                      >
                        {SEVERITIES.map((s) => <option key={s} value={s}>{s}</option>)}
                      </select>
                    </div>
                    <input
                      value={editReasoning}
                      onChange={(e) => setEditReasoning(e.target.value)}
                      placeholder="Reason for the edit (optional)…"
                      style={{
                        width: '100%', boxSizing: 'border-box', padding: '6px 8px',
                        fontSize: 12, borderRadius: 4, border: `1px solid ${C.border}`,
                        marginBottom: 6,
                      }}
                    />
                    <div style={{ display: 'flex', gap: 6 }}>
                      <button
                        disabled={busy || !editIntent.trim()}
                        onClick={saveEdit}
                        style={{
                          padding: '4px 12px', fontSize: 11, fontFamily: MONO, borderRadius: 3,
                          border: 'none', background: C.accent, color: '#FFFFFF',
                          cursor: busy ? 'not-allowed' : 'pointer',
                        }}
                      >
                        Save
                      </button>
                      <button
                        onClick={() => setEditingFlag(null)}
                        style={{
                          padding: '4px 12px', fontSize: 11, fontFamily: MONO, borderRadius: 3,
                          border: `1px solid ${C.border}`, background: C.bgSurface, color: C.textSecondary,
                          cursor: 'pointer',
                        }}
                      >
                        Cancel
                      </button>
                    </div>
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
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
          <button
            onClick={onBack}
            style={{
              padding: '6px 12px', fontSize: 12,
              borderRadius: 4, border: `1px solid ${C.border}`,
              background: C.bgSurface, color: C.textPrimary, cursor: 'pointer',
            }}
          >
            ← Back to queue
          </button>
          <div style={{ display: 'flex', gap: 8 }}>
            <button
              onClick={goToPrev}
              disabled={!hasPrev}
              style={{
                padding: '6px 12px', fontSize: 12, borderRadius: 4,
                border: `1px solid ${C.border}`,
                background: C.bgSurface, color: hasPrev ? C.textPrimary : C.textMuted,
                cursor: hasPrev ? 'pointer' : 'not-allowed',
              }}
            >
              ← Previous
            </button>
            <button
              onClick={goToNext}
              disabled={!hasNext}
              style={{
                padding: '6px 12px', fontSize: 12, borderRadius: 4,
                border: `1px solid ${C.border}`,
                background: C.bgSurface, color: hasNext ? C.textPrimary : C.textMuted,
                cursor: hasNext ? 'pointer' : 'not-allowed',
              }}
            >
              Next →
            </button>
          </div>
        </div>

        {loading && !detail ? <LoadingSpinner /> : !session ? (
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
              <HasVideoBadge value={session.has_video} />
              <span style={{ fontSize: 12, color: C.textSecondary }}>
                {session.lang || 'unknown language'} · {segments.length} segments · {activeFlags.length} flags
                {unactionedCount > 0 ? ` (${unactionedCount} unactioned)` : ''}
                {pauses.length > 0 ? ` · ${pauses.length} pauses` : ''}
              </span>
              {/* Review trail — who did what, when (chat parity) */}
              {(session.submitted_by || session.reviewer_id || session.locked_by) && (
                <span style={{ fontSize: 12, color: C.textSecondary, fontFamily: MONO }}>
                  {session.submitted_by && `submitted by ${session.submitted_by}${session.submitted_at ? ` at ${session.submitted_at}` : ''}`}
                  {!session.submitted_by && session.reviewer_id && `reviewed by ${session.reviewer_id}${session.reviewed_at ? ` at ${session.reviewed_at}` : ''}`}
                  {session.locked_by && ` · locked by ${session.locked_by}${session.locked_at ? ` at ${session.locked_at}` : ''}`}
                </span>
              )}
              {session.reviewer_note && (
                <span style={{ fontSize: 12, color: C.textSecondary }}>
                  note: {session.reviewer_note}
                </span>
              )}
            </div>

            {/* Audio player — HLS stream; click a flag's timestamp to jump there */}
            {audioUrl ? (
              <div style={{
                background: C.bgSurface, border: `1px solid ${C.border}`,
                borderRadius: 6, padding: '12px 18px', marginBottom: 16,
              }}>
                <div style={{
                  fontSize: 11, fontFamily: MONO, textTransform: 'uppercase',
                  letterSpacing: '0.05em', color: C.textSecondary, marginBottom: 8,
                }}>
                  Recording — click any flagged timestamp below to jump to it
                </div>
                <div style={{ fontSize: 12, color: C.textSecondary, marginBottom: 10 }}>
                  Source media: {getHasVideoState(session.has_video) == null
                    ? 'video status unknown'
                    : getHasVideoState(session.has_video)
                      ? 'contains a video stream'
                      : 'audio only'}
                </div>
                {getHasVideoState(session.has_video) ? (
                  <video ref={audioRef} controls preload="metadata" style={{ width: '100%', maxHeight: 480, display: 'block' }} />
                ) : (
                  <audio ref={audioRef} controls preload="metadata" style={{ width: '100%', display: 'block' }} />
                )}
                {playerError && (
                  <div style={{ fontSize: 12, color: C.severeText, marginTop: 6 }}>
                    {playerError}
                  </div>
                )}
              </div>
            ) : (
              <div style={{
                fontSize: 12, color: C.textMuted, marginBottom: 16,
              }}>
                No recording attached to this session (no audio_url ingested).
              </div>
            )}

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
              {!readOnly && reviewerRole !== 'L2' && (
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
                  {unactionedCount > 0 && (
                    <button
                      disabled={busy}
                      onClick={() => doAction(() => confirmAllAudioFlags(sId, reviewerName))}
                      title="Confirm every unactioned flag at once"
                      style={{
                        padding: '9px 16px', fontSize: 13, fontWeight: 500,
                        borderRadius: 5, border: `1px solid ${C.accent}`,
                        background: C.accentLight, color: C.accentDark,
                        cursor: busy ? 'not-allowed' : 'pointer', whiteSpace: 'nowrap',
                      }}
                    >
                      ✓ Confirm All ({unactionedCount})
                    </button>
                  )}
                  <button
                    disabled={busy || unactionedCount > 0}
                    title={unactionedCount > 0
                      ? `${unactionedCount} flag(s) must be confirmed, edited or dismissed first`
                      : 'Submit this session for L2 review'}
                    onClick={() => doAction(() => submitAudioSession(sId, reviewerName, note))}
                    style={{
                      padding: '9px 16px', fontSize: 13, fontWeight: 500,
                      borderRadius: 5, border: 'none',
                      background: (busy || unactionedCount > 0) ? '#D4D0C9' : C.accent,
                      color: (busy || unactionedCount > 0) ? C.textMuted : '#FFFFFF',
                      cursor: (busy || unactionedCount > 0) ? 'not-allowed' : 'pointer',
                      whiteSpace: 'nowrap',
                    }}
                  >
                    Submit for L2 Review
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
              {readOnly && reviewerRole !== 'L2' && (
                <span style={{ fontSize: 13, color: C.textSecondary }}>
                  This session is submitted or locked — read only.
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
