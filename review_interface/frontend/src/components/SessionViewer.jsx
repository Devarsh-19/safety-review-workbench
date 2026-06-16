import React, { useState, useEffect, useRef, useCallback } from 'react';
import { C, MONO } from '../tokens';
import VerdictBadge from './VerdictBadge';
import {
  getSessionDetail, getSessionFlags, submitReview,
  manualFlag, saveSessionNote,
  confirmFlag, submitSession, markNeedsFinalReview,
} from '../api';

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const ACTIONS = [
  { key: 'CONFIRM',             label: 'Confirm Flag',        active: { bg: C.accent,     color: '#FFFFFF',       border: C.accent       } },
  { key: 'FALSE_POSITIVE',      label: 'Mark False Positive', active: { bg: C.flaggedBg,  color: C.flaggedText,   border: C.flaggedBorder } },
  { key: 'NEEDS_FINAL_REVIEW',  label: 'Needs Final Review',  active: { bg: C.severeBg,   color: C.severeText,    border: C.severeBorder  } },
  { key: 'CLEAR',               label: 'Clear',               active: { bg: C.bgStatsrow, color: C.textSecondary, border: C.border        } },
];

const STATUS_LABEL = {
  CONFIRMED:          'Confirmed Flag',
  OVERRIDDEN:         'Marked False Positive',
  NEEDS_FINAL_REVIEW: 'Needs Final Review',
  REVIEWED:           'Cleared',
  LOCKED:             'Locked',
};

const INTENT_CATEGORIES = [
  'OFF_PLATFORM_SOLICITATION',
  'NSFW',
  'NSFW_EXPLICIT',
  'NSFW_GROOMING',
  'NSFW_APPEARANCE',
  'CSAM_RISK',
  'FEAR_MANIPULATION',
  'FINANCIAL_SOLICITATION',
  'PERSONAL_DATA_COLLECTION',
  'ABUSIVE_LANGUAGE',
  'HATE_SPEECH',
  'IDENTITY_FRAUD',
  'FAKE_REMEDIES',
  'UNAUTHORIZED_MEDICAL_ADVICE',
  'COMPETITOR_PROMOTION',
  'RE_ENGAGEMENT_SOLICITATION',
  'EXTERNAL_MEDIA_CONTENT',
  'OTHER',
];

// ---------------------------------------------------------------------------
// Flag → turn association
// Priority 1: direct turn_id match (value equality, not index arithmetic)
// Priority 2: pattern_matched exact substring containment (for null turn_id)
// Priority 3: fuzzy word-overlap on reasoning (non-MANUAL, null turn_id only)
// DISMISSED flags are excluded — they should not generate transcript badges.
// ---------------------------------------------------------------------------
function buildFlagsByTurnIdx(turns, flags) {
  const result = {};
  flags.forEach((flag) => {
    // Only parent flags generate transcript badges; children are rendered through getFlagState
    if (flag.parent_flag_id != null) return;

    // Priority 1 — direct turn_id value match
    if (flag.turn_id != null) {
      const idx = turns.findIndex((t) => String(t.turn_id) === String(flag.turn_id));
      if (idx >= 0) {
        if (!result[idx]) result[idx] = [];
        result[idx].push(flag);
      }
      return; // do not fall through — turn_id was explicitly set
    }

    // Priority 2 — pattern_matched substring containment
    // pattern_matched for MANUAL flags is the first 200 chars of message_text,
    // so either the turn contains pm (long message) or pm equals tm (short message).
    const pm = (flag.pattern_matched || '').trim().toLowerCase();
    if (pm.length >= 4) {
      let matched = false;
      for (let i = 0; i < turns.length; i++) {
        const tm = (turns[i].message_text || '').trim().toLowerCase();
        if (tm.includes(pm) || pm.includes(tm)) {
          if (!result[i]) result[i] = [];
          result[i].push(flag);
          matched = true;
          break;
        }
      }
      if (!matched && flag.detection_layer === 'MANUAL') {
        console.warn('No turn match for flag', flag.flag_id, pm);
        return; // MANUAL flags don't fall through to word-overlap
      }
      if (matched) return;
    }

    // Priority 3 — reasoning word-overlap (LLM / REGEX flags without a pattern_matched match)
    if (!flag.reasoning) return;
    const reasonWords = new Set((flag.reasoning.toLowerCase().match(/\b[a-z]{4,}\b/g) || []));
    for (let i = 0; i < turns.length; i++) {
      const tw = (turns[i].message_text || '').toLowerCase().match(/\b[a-z]{4,}\b/g) || [];
      if (tw.some((w) => reasonWords.has(w))) {
        if (!result[i]) result[i] = [];
        result[i].push(flag);
        break;
      }
    }
  });
  return result;
}

// ---------------------------------------------------------------------------
// Skeleton loader
// ---------------------------------------------------------------------------
function Skeleton({ width = '100%', height = 14, mt = 0, mb = 0 }) {
  return (
    <div style={{ width, height, marginTop: mt, marginBottom: mb,
      background: C.border, borderRadius: 3, animation: 'pulse 1.5s ease-in-out infinite' }} />
  );
}
function SkeletonPane() {
  return (
    <div style={{ padding: 20 }}>
      <Skeleton width={80} height={10} mb={20} />
      {[75, 60, 85, 50, 70].map((w, i) => (
        <div key={i} style={{ marginBottom: 16 }}><Skeleton width={`${w}%`} height={40} /></div>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Toast
// ---------------------------------------------------------------------------
function Toast({ message }) {
  return (
    <div style={{
      position: 'fixed', bottom: 28, right: 28, background: C.topbar, color: '#FFFFFF',
      borderRadius: 6, padding: '12px 20px', fontSize: 13, display: 'flex',
      alignItems: 'center', gap: 8, zIndex: 1000, animation: 'slideUp 0.25s ease-out',
    }}>
      <span style={{ color: C.accentLight, fontWeight: 600 }}>✓</span>
      {message}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Detection-layer badge colours
// ---------------------------------------------------------------------------
function layerStyle(layer) {
  if (layer === 'LLM')       return { bg: C.llmBg,    text: C.llmText,    border: C.llmBorder    };
  if (layer === 'MANUAL')    return { bg: C.manualBg, text: C.manualText, border: C.manualBorder };
  if (layer === 'AMENDED')   return { bg: '#E1F5EE',  text: '#085041',    border: '#9FE1CB'      };
  if (layer === 'DISMISSED') return { bg: '#F5F4F0',  text: '#9B9890',    border: '#D4D0C9'      };
  return                              { bg: C.regexBg, text: C.regexText,  border: C.regexBorder  };
}

// ---------------------------------------------------------------------------
// Flag card Edit / Dismiss action button (manages its own hover state)
// ---------------------------------------------------------------------------
function FlagActionButton({ label, onClick, hoverColor, hoverBorder }) {
  const [hovered, setHovered] = React.useState(false);
  return (
    <button
      onClick={onClick}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      style={{
        fontSize: 10, fontFamily: MONO,
        background: '#FFFFFF',
        border: `1px solid ${hovered ? hoverBorder : '#E2DED8'}`,
        borderRadius: 3, padding: '2px 7px',
        color: hovered ? hoverColor : '#6B6860',
        cursor: 'pointer',
        transition: 'border-color 120ms, color 120ms',
      }}
    >
      {label}
    </button>
  );
}

// ---------------------------------------------------------------------------
// Detection-layer badge — used individually or paired (parent + child layers)
// ---------------------------------------------------------------------------
function DetectionBadge({ layer }) {
  const s = layerStyle(layer);
  return (
    <span style={{
      fontSize: 10, fontFamily: MONO, fontWeight: 500,
      padding: '2px 7px', borderRadius: 3,
      background: s.bg, color: s.text, border: `1px solid ${s.border}`,
    }}>
      {layer}
    </span>
  );
}

// ---------------------------------------------------------------------------
// Canonical flag state — single source of truth for rendering and counting
// ---------------------------------------------------------------------------
function getFlagState(parentFlag, allFlags) {
  // Match children by explicit FK — correct even when an amendment changes the category_code
  const children = allFlags.filter((f) => f.parent_flag_id === parentFlag.flag_id);
  const dismissedChild = children.find((f) => f.detection_layer === 'DISMISSED');
  if (dismissedChild) return { state: 'DISMISSED', child: dismissedChild };
  const amendedChild = children.find((f) => f.detection_layer === 'AMENDED');
  if (amendedChild) return { state: 'AMENDED', child: amendedChild };
  if (parentFlag.is_confirmed === 1) return { state: 'CONFIRMED', child: null };
  return { state: 'UNACTIONED', child: null };
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export default function SessionViewer({ sessionId, sessionList, reviewerName, reviewerRole, onBack, onNavigate }) {
  // ── Core state ────────────────────────────────────────────────────────────
  const [data,           setData]           = useState(null);
  const [flags,          setFlags]          = useState([]);   // refreshable separately
  const [loading,        setLoading]        = useState(true);
  const [error,          setError]          = useState(null);

  // Review panel state
  const [selectedAction, setSelectedAction] = useState(null);
  const [note,           setNote]           = useState('');
  const [noteFocused,    setNoteFocused]    = useState(false);
  const [noteValidation, setNoteValidation] = useState(false);
  const [submitting,     setSubmitting]     = useState(false);
  const [toast,          setToast]          = useState(null);
  const [showUpdateForm, setShowUpdateForm] = useState(false);

  // Feature 1 — manual flagging
  const [hoveredTurnIdx,    setHoveredTurnIdx]    = useState(null);
  const [openFlagPopover,   setOpenFlagPopover]   = useState(null);
  const [popoverCategory,   setPopoverCategory]   = useState(INTENT_CATEGORIES[0]);
  const [popoverNote,       setPopoverNote]       = useState('');
  const [popoverNoteFocused,setPopoverNoteFocused]= useState(false);
  const [flaggingProgress,  setFlaggingProgress]  = useState(false);
  const [confirmedTurns,    setConfirmedTurns]    = useState(new Set());

  // Feature 2 — session note
  const [sessionNote,       setSessionNote]       = useState('');
  const [sessionNoteFocused,setSessionNoteFocused]= useState(false);
  const [noteSaved,         setNoteSaved]         = useState(false);

  // ── Feature A/B state ─────────────────────────────────────────────────────
  const [flagScrollMsg,      setFlagScrollMsg]      = useState({});
  const [highlightedTurnIdx, setHighlightedTurnIdx] = useState(null);
  const [editingFlagId,      setEditingFlagId]      = useState(null);
  const [editForm,           setEditForm]           = useState({});
  const [editSaving,         setEditSaving]         = useState(false);
  const [dismissingFlagId,   setDismissingFlagId]   = useState(null);
  const [dismissNote,        setDismissNote]        = useState('');
  const [dismissSaving,      setDismissSaving]      = useState(false);
  const [flagCardHoverId,    setFlagCardHoverId]    = useState(null);

  // Workflow state
  const [l2Note,             setL2Note]             = useState('');
  const [l2NoteFocused,      setL2NoteFocused]      = useState(false);
  const [submitSuccess,      setSubmitSuccess]      = useState(false);
  const [confirmingFlagId,   setConfirmingFlagId]   = useState(null);
  const [lockBtnHover,       setLockBtnHover]       = useState(false);

  // Refs
  const firstFlaggedRef  = useRef(null);
  const popoverRef       = useRef(null);
  const noteDebounceRef  = useRef(null);
  const turnRefs         = useRef([]);

  // ── Data loading ──────────────────────────────────────────────────────────
  useEffect(() => {
    setLoading(true);
    setError(null);
    setData(null);
    setFlags([]);
    setNote('');
    setNoteValidation(false);
    setSelectedAction(null);
    setShowUpdateForm(false);
    setSessionNote('');
    setOpenFlagPopover(null);
    setConfirmedTurns(new Set());
    setFlagScrollMsg({});
    setHighlightedTurnIdx(null);
    setEditingFlagId(null);
    setEditForm({});
    setEditSaving(false);
    setDismissingFlagId(null);
    setDismissNote('');
    setDismissSaving(false);
    setFlagCardHoverId(null);
    setL2Note('');
    setL2NoteFocused(false);
    setSubmitSuccess(false);
    setConfirmingFlagId(null);
    setLockBtnHover(false);

    getSessionDetail(sessionId)
      .then((d) => {
        setData(d);
        setFlags(d.flags || []);
        setSessionNote(d.session?.session_note || '');
        setLoading(false);
      })
      .catch((err) => { setError(err.message); setLoading(false); });
  }, [sessionId]);

  // Auto-scroll to first flagged turn
  useEffect(() => {
    if (data && firstFlaggedRef.current) {
      firstFlaggedRef.current.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
  }, [data]);

  // Close popover on outside click or Escape
  useEffect(() => {
    if (openFlagPopover === null) return;
    const onMouseDown = (e) => {
      if (popoverRef.current && !popoverRef.current.contains(e.target)) {
        setOpenFlagPopover(null);
      }
    };
    const onKeyDown = (e) => { if (e.key === 'Escape') setOpenFlagPopover(null); };
    document.addEventListener('mousedown', onMouseDown);
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('mousedown', onMouseDown);
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [openFlagPopover]);

  // ── Navigation ────────────────────────────────────────────────────────────
  const currentIdx = sessionList ? sessionList.findIndex((s) => s.session_id === sessionId) : -1;
  const prevId     = currentIdx > 0                      ? sessionList[currentIdx - 1]?.session_id : null;
  const nextId     = currentIdx < sessionList.length - 1 ? sessionList[currentIdx + 1]?.session_id : null;
  const noteRequired = selectedAction === 'FALSE_POSITIVE' || selectedAction === 'NEEDS_FINAL_REVIEW';
  const noteValid = !noteRequired || note.trim().length >= 10;
  const submitDisabled = !selectedAction || !noteValid || submitting;

  // ── Review submit ─────────────────────────────────────────────────────────
  const handleSubmit = useCallback(async () => {
    if (submitting || !selectedAction) return;
    if (!noteValid) {
      setNoteValidation(true);
      return;
    }
    setSubmitting(true);
    try {
      await submitReview(sessionId, selectedAction, reviewerName, note, null);
      setToast('Review saved');
      setTimeout(() => { setToast(null); onBack(); }, 2500);
    } catch (err) {
      setToast(`Error: ${err.message}`);
      setSubmitting(false);
    }
  }, [submitting, selectedAction, noteValid, sessionId, reviewerName, note, onBack]);

  // ── Manual flag submit ────────────────────────────────────────────────────
  const handleManualFlag = async (turn, turnIdx) => {
    if (flaggingProgress) return;
    setFlaggingProgress(true);
    try {
      await manualFlag(sessionId, {
        turn_id:       null,
        category_code: popoverCategory,
        note:          popoverNote,
        reviewer_id:   reviewerName,
        message_text:  (turn.message_text || '').slice(0, 200),
      });
      const updated = await getSessionFlags(sessionId);
      setFlags(updated);
      setOpenFlagPopover(null);
      setPopoverNote('');
      setConfirmedTurns((prev) => new Set([...prev, turnIdx]));
      setTimeout(() => {
        setConfirmedTurns((prev) => { const n = new Set(prev); n.delete(turnIdx); return n; });
      }, 2000);
    } catch (_) {
    } finally {
      setFlaggingProgress(false);
    }
  };

  // ── Session note auto-save (debounced 1.5 s) ──────────────────────────────
  const handleNoteChange = (value) => {
    setSessionNote(value);
    if (noteDebounceRef.current) clearTimeout(noteDebounceRef.current);
    noteDebounceRef.current = setTimeout(async () => {
      try {
        await saveSessionNote(sessionId, value, reviewerName);
        setNoteSaved(true);
        setTimeout(() => setNoteSaved(false), 2000);
      } catch (_) {}
    }, 1500);
  };

  // ── Feature B — Flag card edit / dismiss ─────────────────────────────────
  const openEditForm = (flag) => {
    setEditingFlagId(flag.flag_id);
    setEditForm({
      category_code: flag.category_code,
      severity:      flag.severity || 'MEDIUM',
      reasoning:     flag.reasoning || '',
    });
  };

  const handleSaveAmend = async (flag) => {
    if (editSaving) return;
    setEditSaving(true);
    try {
      // Always POST to /amend on the PARENT's flag_id.
      // For AMENDED children, parent_flag_id holds the original parent's id.
      // The backend is idempotent: if an AMENDED child already exists it updates
      // it in place rather than creating a second one.
      const targetId = flag.detection_layer === 'AMENDED' ? flag.parent_flag_id : flag.flag_id;
      const res = await fetch(`/flags/${targetId}/amend`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...editForm, reviewer_id: reviewerName }),
      });
      if (!res.ok) throw new Error('amend failed');
      const updated = await getSessionFlags(sessionId);
      setFlags(updated);
      setEditingFlagId(null);
    } catch (_) {
    } finally {
      setEditSaving(false);
    }
  };

  const handleConfirmDismiss = async (flag) => {
    if (dismissSaving) return;
    setDismissSaving(true);
    try {
      const res = await fetch(`/flags/${flag.flag_id}/dismiss`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ reviewer_id: reviewerName, note: dismissNote }),
      });
      if (!res.ok) throw new Error('dismiss failed');
      const updated = await getSessionFlags(sessionId);
      setFlags(updated);
      setDismissingFlagId(null);
    } catch (_) {
    } finally {
      setDismissSaving(false);
    }
  };

  // ── Workflow handlers ─────────────────────────────────────────────────────
  const handleConfirmFlag = async (flag) => {
    if (confirmingFlagId === flag.flag_id) return;
    setConfirmingFlagId(flag.flag_id);
    try {
      await confirmFlag(flag.flag_id, reviewerName);
      const updated = await getSessionFlags(sessionId);
      setFlags(updated);
    } catch (_) {}
    finally {
      setConfirmingFlagId(null);
    }
  };

  const handleSessionSubmit = async () => {
    if (submitting) return;
    setSubmitting(true);
    try {
      await submitSession(sessionId, reviewerName, l2Note || null);
      setSubmitSuccess(true);
      setTimeout(() => { setSubmitSuccess(false); onBack(); }, 2000);
    } catch (err) {
      setToast(`Error: ${err.message}`);
      setSubmitting(false);
    }
  };

  const handleMarkNeedsFinalReview = async () => {
    setSubmitting(true);
    try {
      await markNeedsFinalReview(sessionId, 'Amogh');
      const updated = await getSessionDetail(sessionId);
      setData(updated);
      setFlags(updated.flags || []);
    } catch (err) {
      setToast(`Error: ${err.message}`);
    } finally {
      setSubmitting(false);
    }
  };

  const handleLockSession = async () => {
    if (!window.confirm(`Lock session ${sessionId}? This is final and cannot be undone.`)) return;
    setSubmitting(true);
    try {
      await fetch(`/sessions/${encodeURIComponent(sessionId)}/lock`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ reviewer_id: 'Amogh' }),
      });
      const updated = await getSessionDetail(sessionId);
      setData(updated);
      setFlags(updated.flags || []);
    } catch (err) {
      setToast(`Error: ${err.message}`);
    } finally {
      setSubmitting(false);
    }
  };

  // ── Error state ───────────────────────────────────────────────────────────
  if (error) {
    return (
      <div style={{ height: '100%', display: 'flex', flexDirection: 'column',
        alignItems: 'center', justifyContent: 'center', gap: 12, background: C.bgPage }}>
        <div style={{ fontSize: 14, color: C.severeText }}>Failed to load session.</div>
        <div style={{ fontSize: 12, color: C.textSecondary }}>{error}</div>
        <button onClick={onBack} className="btn-link-accent"
          style={{ fontSize: 13, color: C.accent, background: 'none', border: 'none', cursor: 'pointer' }}>
          ← Back to Queue
        </button>
      </div>
    );
  }

  const { session = {}, turns = [] } = data || {};
  const flagsByTurnIdx  = data ? buildFlagsByTurnIdx(turns, flags) : {};
  const firstFlaggedIdx = turns.findIndex((_, i) => flagsByTurnIdx[i]?.length > 0);
  const status             = session?.review_status;
  const isLocked           = status === 'LOCKED';
  const isSubmitted        = status === 'SUBMITTED_FOR_REVIEW';
  const isNeedsFinalReview = status === 'NEEDS_FINAL_REVIEW';
  const isReviewed         = status && status !== 'PENDING' && session.reviewer_id;

  // ── Flag state model — single source of truth ───────────────────────────
  // parent_flag_id === null identifies original flags; detection_layer guard handles
  // pre-migration orphaned AMENDED/DISMISSED rows that have parent_flag_id = null.
  const parentFlags         = flags.filter((f) =>
    f.parent_flag_id == null && !['AMENDED', 'DISMISSED'].includes(f.detection_layer)
  );
  const totalFlagCount      = parentFlags.length;
  const actionedFlagCount   = parentFlags.filter((f) => getFlagState(f, flags).state !== 'UNACTIONED').length;
  const unactionedFlagCount = totalFlagCount - actionedFlagCount;
  const canSubmit           = totalFlagCount === 0 || actionedFlagCount === totalFlagCount;

  // Buttons render only when role + session status permit
  const canActOnFlags =
    !isLocked && (
      (reviewerRole === 'L1' && status === 'PENDING') ||
      (reviewerRole === 'L2' && (status === 'SUBMITTED_FOR_REVIEW' || status === 'NEEDS_FINAL_REVIEW'))
    );

  // ── Feature A — Click flag card to jump to matching turn ─────────────────
  const handleFlagCardClick = (flag) => {
    const text = (i) => (turns[i].message_text || '').toLowerCase();

    const doScroll = (idx) => {
      turnRefs.current[idx]?.scrollIntoView({ behavior: 'smooth', block: 'center' });
      setHighlightedTurnIdx(idx);
      setFlagScrollMsg((prev) => ({ ...prev, [flag.flag_id]: 'viewing' }));
      setTimeout(() => {
        setHighlightedTurnIdx(null);
        setFlagScrollMsg((prev) => { const n = { ...prev }; delete n[flag.flag_id]; return n; });
      }, 2000);
    };

    // Step 0: pattern_matched exact substring (reliable for MANUAL flags whose
    // pattern_matched is the first 200 chars of the flagged message_text)
    if (flag.turn_id == null) {
      const pm = (flag.pattern_matched || '').trim().toLowerCase();
      if (pm.length >= 4) {
        for (let i = 0; i < turns.length; i++) {
          const tm = text(i).trim();
          if (tm.includes(pm) || pm.includes(tm)) { doScroll(i); return; }
        }
        if (flag.detection_layer === 'MANUAL') {
          console.warn('No turn match for flag', flag.flag_id, pm);
          return;
        }
      }
    }

    // Step 1: tokens from pattern_matched > 6 chars
    const patParts = (flag.pattern_matched || '').toLowerCase().split(/\s+/).filter(t => t.length > 6);
    for (let i = 0; i < turns.length; i++) {
      if (patParts.some(t => text(i).includes(t))) { doScroll(i); return; }
    }

    // Step 2: tokens from reasoning > 6 chars
    const reasonParts = (flag.reasoning || '').toLowerCase().split(/\s+/).filter(t => t.length > 6);
    for (let i = 0; i < turns.length; i++) {
      if (reasonParts.some(t => text(i).includes(t))) { doScroll(i); return; }
    }

    // Step 3: words from category_code (split by _) > 4 chars
    const catParts = (flag.category_code || '').toLowerCase().split('_').filter(w => w.length > 4);
    for (let i = 0; i < turns.length; i++) {
      if (catParts.some(w => text(i).includes(w))) { doScroll(i); return; }
    }

    // Step 4: no match
  };

  // ── Render ────────────────────────────────────────────────────────────────
  return (
    <div style={{ height: '100%', display: 'flex', flexDirection: 'column', background: C.bgPage }}>

      {/* ── Locked banner ────────────────────────────────────────────────── */}
      {!loading && isLocked && (
        <div style={{
          flexShrink: 0, background: '#F1EFE8', borderBottom: '2px solid #D3D1C7',
          padding: '10px 20px', fontSize: 12, fontFamily: MONO, color: '#444441',
        }}>
          🔒 Locked by {session.locked_by} on {session.locked_at ? String(session.locked_at).slice(0, 10) : '—'} — this session is finalised and read-only
        </div>
      )}

      {/* ── Submitted banner ─────────────────────────────────────────────── */}
      {!loading && isSubmitted && (
        <div style={{
          flexShrink: 0, background: '#EFF6FF', borderBottom: '2px solid #B5D4F4',
          padding: '10px 20px', fontSize: 12, fontFamily: MONO, color: '#0C447C',
        }}>
          ✓ Submitted for L2 review by {session.submitted_by || '—'} on {session.submitted_at ? String(session.submitted_at).slice(0, 10) : '—'} — awaiting Amogh's sign-off
        </div>
      )}

      {/* ── Needs Final Review banner (L2 view only) ─────────────────────── */}
      {!loading && isNeedsFinalReview && reviewerRole === 'L2' && (
        <div style={{
          flexShrink: 0, background: '#FFFBEB', borderBottom: '2px solid #FAC775',
          padding: '10px 20px', fontSize: 12, fontFamily: MONO, color: '#854F0B',
        }}>
          ⚑ Needs Final Review — marked by Amogh for team discussion before locking
        </div>
      )}

      {/* ── Header ───────────────────────────────────────────────────────── */}
      <div style={{
        flexShrink: 0, display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        padding: '12px 20px', background: C.bgSurface, borderBottom: `1px solid ${C.border}`,
        gap: 16, minHeight: 52,
      }}>
        <button onClick={onBack} className="btn-back-link"
          style={{ fontSize: 13, color: C.accent, background: 'none', border: 'none',
            cursor: 'pointer', flexShrink: 0, padding: 0 }}>
          ← Queue
        </button>

        <div style={{ display: 'flex', alignItems: 'center', gap: 10, flex: 1,
          justifyContent: 'center', flexWrap: 'wrap' }}>
          <span style={{ fontSize: 13, fontFamily: MONO, color: C.textSecondary }}>
            {isLocked && <span style={{ marginRight: 4 }}>🔒</span>}{sessionId}
          </span>
          {!loading && <VerdictBadge verdict={session.overall_verdict} />}
          {!loading && session.language_detected && (
            <span style={{ fontSize: 11, fontFamily: MONO, background: C.bgStatsrow,
              border: `1px solid ${C.border}`, padding: '2px 8px', borderRadius: 3, color: C.textSecondary }}>
              {session.language_detected}
            </span>
          )}
          {!loading && session.duration_minutes != null && (
            <span style={{ fontSize: 12, color: C.textMuted }}>
              {Math.round(session.duration_minutes)} min
            </span>
          )}
        </div>

        <div style={{ display: 'flex', gap: 6, flexShrink: 0 }}>
          {[{ label: '← Prev', id: prevId }, { label: 'Next →', id: nextId }].map(({ label, id }) => (
            <button key={label} disabled={!id} onClick={() => id && onNavigate(id)}
              className={id ? 'btn-nav' : ''}
              style={{
                padding: '5px 12px', fontSize: 12, border: `1px solid ${C.border}`,
                borderRadius: 4, background: C.bgSurface, color: id ? C.textPrimary : C.border,
                cursor: id ? 'pointer' : 'not-allowed', opacity: id ? 1 : 0.4, transition: 'background 150ms',
              }}>
              {label}
            </button>
          ))}
        </div>
      </div>

      {/* ── Body ─────────────────────────────────────────────────────────── */}
      <div style={{ flex: 1, display: 'flex', overflow: 'hidden' }}>

        {/* ── LEFT: Transcript ──────────────────────────────────────────── */}
        <div style={{ width: '60%', overflowY: 'auto', padding: 20, borderRight: `1px solid ${C.border}` }}>
          <div style={{ fontSize: 10, fontFamily: MONO, textTransform: 'uppercase',
            letterSpacing: '0.08em', color: C.textMuted, marginBottom: 16 }}>
            Transcript
          </div>

          {loading ? <SkeletonPane /> : turns.length === 0 ? (
            <div style={{ fontSize: 13, color: C.textSecondary, fontStyle: 'italic' }}>
              No transcript available for this session.
            </div>
          ) : (
            turns.map((turn, idx) => {
              const isAstrologer   = turn.speaker === 'ASTROLOGER';
              const turnFlags      = flagsByTurnIdx[idx] || [];
              const isFirstFlagged = idx === firstFlaggedIdx;
              const isHovered      = hoveredTurnIdx === idx;
              const isPopoverOpen  = openFlagPopover === idx;

              // Compute live badge state for each parent flag on this turn.
              // DISMISSED → no badge; AMENDED → show the amended category_code + severity.
              const liveTurnBadges = turnFlags.map((f) => {
                const { state, child } = getFlagState(f, flags);
                if (state === 'DISMISSED') return null;
                return {
                  label:    state === 'AMENDED' ? child.category_code : f.category_code,
                  severity: state === 'AMENDED' ? child.severity      : f.severity,
                };
              }).filter(Boolean);

              const maxSev = liveTurnBadges.length
                ? (liveTurnBadges.some((b) => b.severity === 'HIGH') ? 'HIGH'
                  : liveTurnBadges.some((b) => b.severity === 'MEDIUM') ? 'MEDIUM' : 'LOW')
                : null;
              const flagColor     = maxSev === 'HIGH' ? C.severeBorder : maxSev === 'MEDIUM' ? C.flaggedBorder : maxSev === 'LOW' ? C.cleanBorder : null;
              const flagBg        = maxSev === 'HIGH' ? C.severeBg    : maxSev === 'MEDIUM' ? C.flaggedBg    : maxSev === 'LOW' ? C.cleanBg    : null;
              const flagTextColor = maxSev === 'HIGH' ? C.severeText  : maxSev === 'MEDIUM' ? C.flaggedText  : maxSev === 'LOW' ? C.cleanText  : null;

              return (
                <div
                  key={turn.turn_id ?? idx}
                  ref={(el) => { turnRefs.current[idx] = el; if (isFirstFlagged) firstFlaggedRef.current = el; }}
                  style={{ display: 'flex', flexDirection: 'column',
                    alignItems: isAstrologer ? 'flex-start' : 'flex-end',
                    marginBottom: isPopoverOpen ? 0 : 16, position: 'relative' }}
                  onMouseEnter={() => setHoveredTurnIdx(idx)}
                  onMouseLeave={() => { if (!isPopoverOpen) setHoveredTurnIdx(null); }}
                >
                  {/* Category badges above bubble — live state: DISMISSED hidden, AMENDED shows amended category */}
                  {liveTurnBadges.length > 0 && (
                    <div style={{ display: 'flex', gap: 4, marginBottom: 4, flexWrap: 'wrap' }}>
                      {liveTurnBadges.map((b, bi) => (
                        <span key={bi} style={{
                          fontSize: 10, fontFamily: MONO, fontWeight: 500,
                          padding: '1px 6px', borderRadius: 3, textTransform: 'uppercase',
                          background: flagBg, color: flagTextColor, border: `1px solid ${flagColor}`,
                        }}>
                          {b.label}
                        </span>
                      ))}
                    </div>
                  )}

                  {/* Speaker label */}
                  <div style={{ fontSize: 10, fontFamily: MONO, textTransform: 'uppercase',
                    color: C.textSecondary, marginBottom: 4,
                    textAlign: isAstrologer ? 'left' : 'right' }}>
                    {isAstrologer ? 'Astrologer' : 'User'}
                  </div>

                  {/* Bubble + Flag button (flex row) */}
                  <div style={{
                    display: 'flex', alignItems: 'flex-start', gap: 6, maxWidth: '78%',
                    flexDirection: isAstrologer ? 'row' : 'row-reverse',
                  }}>
                    <div style={{
                      padding: '10px 14px',
                      borderRadius: isAstrologer ? '0 8px 8px 8px' : '8px 0 8px 8px',
                      fontSize: 13, lineHeight: 1.6,
                      background: liveTurnBadges.length > 0 ? '#FCEBEB' : (isAstrologer ? '#F1F5F9' : '#EFF6FF'),
                      color: liveTurnBadges.length > 0 ? '#791F1F' : C.textPrimary,
                      border: liveTurnBadges.length > 0 ? '1px solid #F7C1C1' : undefined,
                      wordBreak: 'break-word', flex: 1,
                      boxShadow: highlightedTurnIdx === idx ? '0 0 0 3px #F0C419' : undefined,
                      transition: 'box-shadow 0.4s ease-out',
                    }}>
                      {turn.message_text || '(empty)'}
                      {turn.has_link === 1 && (
                        <span style={{
                          display: 'inline-block', marginLeft: 6, fontSize: 10,
                          fontFamily: MONO, background: '#E6F1FB', border: '1px solid #B5D4F4',
                          color: '#0C447C', borderRadius: 3, padding: '1px 6px',
                        }}>
                          🔗 Link
                        </span>
                      )}
                    </div>

                    {/* + Flag button — shows on hover, hidden when locked */}
                    {!isLocked && (isHovered || isPopoverOpen) && (
                      <button
                        onMouseDown={(e) => e.stopPropagation()}
                        onClick={(e) => {
                          e.stopPropagation();
                          if (isPopoverOpen) {
                            setOpenFlagPopover(null);
                          } else {
                            setOpenFlagPopover(idx);
                            setPopoverCategory(INTENT_CATEGORIES[0]);
                            setPopoverNote('');
                          }
                        }}
                        style={{
                          flexShrink: 0, alignSelf: 'flex-start',
                          fontSize: 11, fontFamily: MONO,
                          background: C.bgSurface,
                          border: `1px solid ${isPopoverOpen ? C.accent : C.border}`,
                          borderRadius: 3, padding: '2px 7px',
                          color: isPopoverOpen ? C.accent : C.textSecondary,
                          cursor: 'pointer', marginTop: 4,
                          whiteSpace: 'nowrap',
                        }}
                      >
                        + Flag
                      </button>
                    )}
                  </div>

                  {/* "Flagged ✓" inline confirmation */}
                  {confirmedTurns.has(idx) && (
                    <div style={{ fontSize: 11, fontFamily: MONO, color: C.cleanText, marginTop: 3 }}>
                      ✓ Flagged
                    </div>
                  )}

                  {/* Timestamp */}
                  {turn.timestamp && (
                    <div style={{ fontSize: 10, fontFamily: MONO, color: C.textMuted, marginTop: 3,
                      textAlign: isAstrologer ? 'left' : 'right' }}>
                      {String(turn.timestamp).slice(11, 16)}
                    </div>
                  )}

                  {/* Inline popover — appears below the turn in document flow */}
                  {isPopoverOpen && (
                    <div
                      ref={popoverRef}
                      style={{
                        alignSelf: isAstrologer ? 'flex-start' : 'flex-end',
                        marginTop: 6, marginBottom: 16,
                        background: C.bgSurface,
                        border: `1px solid ${C.border}`,
                        borderRadius: 6, padding: 14, width: 280,
                        boxShadow: '0 4px 12px rgba(0,0,0,0.08)',
                        zIndex: 100,
                      }}
                    >
                      <div style={{ fontSize: 11, fontFamily: MONO, textTransform: 'uppercase',
                        letterSpacing: '0.06em', color: C.textSecondary, marginBottom: 8 }}>
                        Flag this message
                      </div>

                      <select
                        value={popoverCategory}
                        onChange={(e) => setPopoverCategory(e.target.value)}
                        style={{
                          width: '100%', border: `1px solid ${C.border}`, borderRadius: 4,
                          padding: '7px 10px', fontSize: 12, background: C.bgMuted, color: C.textPrimary,
                        }}
                      >
                        {INTENT_CATEGORIES.map((cat) => (
                          <option key={cat} value={cat}>{cat}</option>
                        ))}
                      </select>

                      <textarea
                        rows={2}
                        value={popoverNote}
                        onChange={(e) => setPopoverNote(e.target.value)}
                        onFocus={() => setPopoverNoteFocused(true)}
                        onBlur={() => setPopoverNoteFocused(false)}
                        placeholder="Why are you flagging this?"
                        style={{
                          width: '100%', marginTop: 8, border: `1px solid ${popoverNoteFocused ? C.accent : C.border}`,
                          borderRadius: 4, padding: '7px 10px', fontSize: 12,
                          background: popoverNoteFocused ? C.bgSurface : C.bgMuted,
                          resize: 'none', color: C.textPrimary,
                          transition: 'border-color 150ms, background 150ms',
                        }}
                      />

                      <div style={{ display: 'flex', gap: 8, marginTop: 8 }}>
                        <button
                          onClick={() => setOpenFlagPopover(null)}
                          style={{
                            flex: 1, padding: '6px 0', fontSize: 12, background: C.bgSurface,
                            border: `1px solid ${C.border}`, borderRadius: 4, color: C.textSecondary, cursor: 'pointer',
                          }}
                        >
                          Cancel
                        </button>
                        <button
                          disabled={flaggingProgress}
                          onClick={() => handleManualFlag(turn, idx)}
                          style={{
                            flex: 1, padding: '6px 0', fontSize: 12, fontWeight: 500,
                            background: flaggingProgress ? '#D4D0C9' : C.accent,
                            border: 'none', borderRadius: 4, color: '#FFFFFF', cursor: flaggingProgress ? 'not-allowed' : 'pointer',
                          }}
                        >
                          {flaggingProgress ? '…' : 'Add Flag'}
                        </button>
                      </div>
                    </div>
                  )}
                </div>
              );
            })
          )}
        </div>

        {/* ── RIGHT: Note + Flags + Review ──────────────────────────────── */}
        <div style={{ width: '40%', display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>

          {/* Feature 2 — Session Note (above flags) */}
          <div style={{
            flexShrink: 0, padding: '14px 20px 12px',
            borderBottom: `1px solid ${C.borderLight}`,
          }}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 8 }}>
              <span style={{ fontSize: 10, fontFamily: MONO, textTransform: 'uppercase',
                letterSpacing: '0.08em', color: C.textMuted }}>
                Session Note
              </span>
              {noteSaved && (
                <span style={{ fontSize: 10, fontFamily: MONO, color: C.accent }}>Saved</span>
              )}
            </div>
            <textarea
              rows={3}
              value={sessionNote}
              disabled={isLocked}
              onChange={(e) => !isLocked && handleNoteChange(e.target.value)}
              onFocus={() => !isLocked && setSessionNoteFocused(true)}
              onBlur={() => setSessionNoteFocused(false)}
              placeholder="Add an overall observation about this session..."
              style={{
                width: '100%', padding: '10px 12px', fontSize: 13,
                border: `1px solid ${sessionNoteFocused ? C.accent : C.border}`,
                borderRadius: 5,
                background: isLocked ? C.bgStatsrow : sessionNoteFocused ? C.bgSurface : C.bgMuted,
                resize: 'none', color: isLocked ? C.textSecondary : C.textPrimary,
                transition: 'border-color 150ms, background 150ms',
                cursor: isLocked ? 'not-allowed' : undefined,
              }}
            />
          </div>

          {/* Flags list */}
          <div style={{ flex: 1, overflowY: 'auto', padding: 20 }}>
            <div style={{ fontSize: 10, fontFamily: MONO, textTransform: 'uppercase',
              letterSpacing: '0.08em', color: C.textMuted, marginBottom: 14 }}>
              {totalFlagCount > 0 ? `Flags Detected (${totalFlagCount})` : 'Flags Detected'}
            </div>

            {loading ? <SkeletonPane /> : parentFlags.length === 0 ? (
              <div style={{ fontSize: 13, color: C.textSecondary, fontStyle: 'italic' }}>
                No flags detected for this session.
              </div>
            ) : (
              parentFlags.map((parentFlag, fi) => {
                const { state, child }  = getFlagState(parentFlag, flags);
                const isEditingParent   = editingFlagId === parentFlag.flag_id;
                const isDismissConf     = dismissingFlagId === parentFlag.flag_id;
                const isHoveredParent   = flagCardHoverId === parentFlag.flag_id;
                const scrollMsg         = flagScrollMsg[parentFlag.flag_id];
                const parentOpacity     = state === 'AMENDED' ? 0.45 : state === 'DISMISSED' ? 0.3 : 1;

                const showConfirm       = canActOnFlags && state === 'UNACTIONED';
                const showEditParent    = canActOnFlags && (state === 'UNACTIONED' || state === 'CONFIRMED');
                const showDismissParent = canActOnFlags && (state === 'UNACTIONED' || state === 'CONFIRMED');

                const isEditingChild    = child && editingFlagId === child.flag_id;
                const isHoveredChild    = child && flagCardHoverId === child.flag_id;
                const showEditChild     = canActOnFlags && state === 'AMENDED';

                return (
                  <React.Fragment key={parentFlag.flag_id ?? fi}>
                    {/* ── Parent flag card ── */}
                    <div
                      onClick={() => !isEditingParent && !isDismissConf && handleFlagCardClick(parentFlag)}
                      onMouseEnter={() => setFlagCardHoverId(parentFlag.flag_id)}
                      onMouseLeave={() => setFlagCardHoverId(null)}
                      style={{
                        background: isHoveredParent && !isEditingParent && !isDismissConf ? '#FAFAF8' : C.bgSurface,
                        border: `1px solid ${isHoveredParent && !isEditingParent && !isDismissConf ? '#D4D0C9' : C.border}`,
                        borderRadius: 6, padding: '14px 16px', marginBottom: child ? 4 : 10,
                        cursor: isEditingParent || isDismissConf ? 'default' : 'pointer',
                        opacity: parentOpacity,
                        transition: 'opacity 0.3s, background 150ms, border-color 150ms',
                      }}
                    >
                      {isEditingParent ? (
                        /* ── Edit form (parent → POST /amend) ── */
                        <div onClick={(e) => e.stopPropagation()}>
                          <div style={{ fontSize: 10, fontFamily: MONO, textTransform: 'uppercase',
                            letterSpacing: '0.06em', color: C.textMuted, marginBottom: 10 }}>
                            Edit Flag
                          </div>
                          <select
                            value={editForm.category_code}
                            onChange={(e) => setEditForm((f) => ({ ...f, category_code: e.target.value }))}
                            style={{ width: '100%', padding: '7px 10px', fontSize: 12,
                              border: `1px solid ${C.border}`, borderRadius: 4,
                              background: C.bgMuted, color: C.textPrimary, marginBottom: 8 }}
                          >
                            {INTENT_CATEGORIES.map((cat) => (
                              <option key={cat} value={cat}>{cat}</option>
                            ))}
                          </select>
                          <select
                            value={editForm.severity}
                            onChange={(e) => setEditForm((f) => ({ ...f, severity: e.target.value }))}
                            style={{ width: '100%', padding: '7px 10px', fontSize: 12,
                              border: `1px solid ${C.border}`, borderRadius: 4,
                              background: C.bgMuted, color: C.textPrimary, marginBottom: 8 }}
                          >
                            {['HIGH', 'MEDIUM', 'LOW'].map((s) => (
                              <option key={s} value={s}>{s}</option>
                            ))}
                          </select>
                          <textarea
                            rows={3}
                            value={editForm.reasoning}
                            onChange={(e) => setEditForm((f) => ({ ...f, reasoning: e.target.value }))}
                            style={{ width: '100%', padding: '7px 10px', fontSize: 12,
                              border: `1px solid ${C.border}`, borderRadius: 4,
                              background: C.bgMuted, color: C.textPrimary,
                              resize: 'none', marginBottom: 10 }}
                          />
                          <div style={{ display: 'flex', gap: 8 }}>
                            <button
                              disabled={editSaving}
                              onClick={() => handleSaveAmend(parentFlag)}
                              style={{ flex: 1, padding: '7px 0', fontSize: 12, fontWeight: 500,
                                background: editSaving ? '#D4D0C9' : C.accent,
                                border: 'none', borderRadius: 4, color: '#FFFFFF',
                                cursor: editSaving ? 'not-allowed' : 'pointer' }}
                            >
                              {editSaving ? '…' : 'Save Amendment'}
                            </button>
                            <button
                              onClick={() => setEditingFlagId(null)}
                              style={{ flex: 1, padding: '7px 0', fontSize: 12,
                                background: C.bgSurface, border: `1px solid ${C.border}`,
                                borderRadius: 4, color: C.textSecondary, cursor: 'pointer' }}
                            >
                              Cancel
                            </button>
                          </div>
                        </div>
                      ) : (
                        <>
                          {/* Category + badge + action buttons */}
                          <div style={{ display: 'flex', alignItems: 'center',
                            justifyContent: 'space-between', gap: 8 }}>
                            <span style={{ fontSize: 13, fontFamily: MONO, fontWeight: 500,
                              color: state === 'DISMISSED' ? '#9B9890' : C.textPrimary,
                              textTransform: 'uppercase', flex: 1,
                              overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                              {parentFlag.category_code}
                            </span>
                            <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexShrink: 0 }}>
                              {state === 'CONFIRMED' && (
                                <span style={{
                                  fontSize: 10, fontFamily: MONO,
                                  background: '#E1F5EE', border: '1px solid #9FE1CB',
                                  color: '#085041', borderRadius: 3, padding: '2px 8px',
                                  cursor: 'default', whiteSpace: 'nowrap',
                                }}>✓ Confirmed</span>
                              )}
                              {showConfirm && (
                                <FlagActionButton
                                  label={confirmingFlagId === parentFlag.flag_id ? '…' : 'Confirm'}
                                  onClick={(e) => { e.stopPropagation(); handleConfirmFlag(parentFlag); }}
                                  hoverColor="#0F6E56" hoverBorder="#0F6E56" />
                              )}
                              {showEditParent && (
                                <FlagActionButton label="Edit"
                                  onClick={(e) => { e.stopPropagation(); openEditForm(parentFlag); }}
                                  hoverColor="#0F6E56" hoverBorder="#0F6E56" />
                              )}
                              {showDismissParent && (
                                <FlagActionButton label="Dismiss"
                                  onClick={(e) => { e.stopPropagation(); setDismissingFlagId(parentFlag.flag_id); setDismissNote(''); }}
                                  hoverColor="#A32D2D" hoverBorder="#F7C1C1" />
                              )}
                              <DetectionBadge layer={parentFlag.detection_layer} />
                            </div>
                          </div>

                          {/* Severity + confidence + FP risk */}
                          <div style={{ display: 'flex', alignItems: 'center', gap: 8,
                            marginTop: 8, flexWrap: 'wrap' }}>
                            <VerdictBadge verdict={parentFlag.severity} />
                            {parentFlag.confidence_score != null && (
                              <span style={{ fontSize: 11, fontFamily: MONO, background: C.bgStatsrow,
                                border: `1px solid ${C.border}`, borderRadius: 3, padding: '2px 7px',
                                color: state === 'DISMISSED' ? '#9B9890' : C.textSecondary }}>
                                {Math.round(parentFlag.confidence_score * 100)}%
                              </span>
                            )}
                            {parentFlag.false_positive_risk && (
                              <span style={{ fontSize: 11,
                                color: state === 'DISMISSED' ? '#9B9890' : C.textSecondary }}>
                                FP risk: <b>{parentFlag.false_positive_risk}</b>
                              </span>
                            )}
                          </div>

                          {/* Reasoning */}
                          {parentFlag.reasoning && (
                            <div style={{ fontSize: 12,
                              color: state === 'DISMISSED' ? '#9B9890' : C.textSecondary,
                              lineHeight: 1.5, marginTop: 10, paddingTop: 10,
                              borderTop: `1px solid ${C.borderLight}`, fontStyle: 'italic' }}>
                              {parentFlag.reasoning}
                            </div>
                          )}

                          {/* "Flagged by" — only for MANUAL flags */}
                          {parentFlag.flagged_by && (
                            <div style={{ fontSize: 11, fontFamily: MONO,
                              color: C.textMuted, marginTop: 6 }}>
                              Flagged by {parentFlag.flagged_by}
                            </div>
                          )}

                          {/* "Confirmed by" — shown when CONFIRMED */}
                          {state === 'CONFIRMED' && parentFlag.confirmed_by && (
                            <div style={{ fontSize: 10, fontFamily: MONO,
                              color: '#9B9890', marginTop: 4 }}>
                              Confirmed by {parentFlag.confirmed_by}
                            </div>
                          )}

                          {/* State labels */}
                          {state === 'AMENDED' && (
                            <div style={{ fontSize: 10, fontFamily: MONO,
                              color: '#0F6E56', marginTop: 6 }}>
                              Amended
                            </div>
                          )}
                          {state === 'DISMISSED' && (
                            <div style={{ fontSize: 10, fontFamily: MONO,
                              color: '#A32D2D', marginTop: 6 }}>
                              Dismissed
                            </div>
                          )}

                          {/* Dismiss confirmation panel */}
                          {isDismissConf && (
                            <div
                              onClick={(e) => e.stopPropagation()}
                              style={{ marginTop: 12, paddingTop: 12,
                                borderTop: `1px solid ${C.borderLight}` }}
                            >
                              <div style={{ fontSize: 12, color: '#1C1C1A', marginBottom: 8 }}>
                                Dismiss this flag?
                              </div>
                              <input
                                type="text"
                                placeholder="Reason for dismissal..."
                                value={dismissNote}
                                onChange={(e) => setDismissNote(e.target.value)}
                                style={{ width: '100%', padding: '7px 10px', fontSize: 12,
                                  border: `1px solid ${C.border}`, borderRadius: 4,
                                  background: C.bgMuted, color: C.textPrimary, marginBottom: 8 }}
                              />
                              <div style={{ display: 'flex', gap: 8 }}>
                                <button
                                  disabled={dismissSaving}
                                  onClick={() => handleConfirmDismiss(parentFlag)}
                                  style={{ flex: 1, padding: '6px 0', fontSize: 12, fontWeight: 500,
                                    background: dismissSaving ? '#D4D0C9' : '#A32D2D',
                                    border: 'none', borderRadius: 4, color: '#FFFFFF',
                                    cursor: dismissSaving ? 'not-allowed' : 'pointer' }}
                                >
                                  {dismissSaving ? '…' : 'Confirm Dismiss'}
                                </button>
                                <button
                                  onClick={() => setDismissingFlagId(null)}
                                  style={{ flex: 1, padding: '6px 0', fontSize: 12,
                                    background: C.bgSurface, border: `1px solid ${C.border}`,
                                    borderRadius: 4, color: C.textSecondary, cursor: 'pointer' }}
                                >
                                  Cancel
                                </button>
                              </div>
                            </div>
                          )}
                        </>
                      )}

                      {/* Scroll feedback */}
                      {scrollMsg && !isEditingParent && (
                        <div style={{ fontSize: 10, fontFamily: MONO, marginTop: 6, color: '#0F6E56' }}>
                          ↑ Viewing in transcript
                        </div>
                      )}
                    </div>

                    {/* ── Child flag card (AMENDED or DISMISSED) ── */}
                    {child && (
                      <div
                        onMouseEnter={() => setFlagCardHoverId(child.flag_id)}
                        onMouseLeave={() => setFlagCardHoverId(null)}
                        style={{
                          background: state === 'DISMISSED' ? '#F5F4F0' : (isHoveredChild && !isEditingChild ? '#FAFAF8' : C.bgSurface),
                          border: `1px solid ${state === 'DISMISSED' || (isHoveredChild && !isEditingChild) ? '#D4D0C9' : C.border}`,
                          borderRadius: 6, padding: '14px 16px', marginBottom: 10, marginLeft: 16,
                          transition: 'background 150ms, border-color 150ms',
                        }}
                      >
                        {isEditingChild ? (
                          /* ── Edit form (child → PATCH) ── */
                          <div onClick={(e) => e.stopPropagation()}>
                            <div style={{ fontSize: 10, fontFamily: MONO, textTransform: 'uppercase',
                              letterSpacing: '0.06em', color: C.textMuted, marginBottom: 10 }}>
                              Edit Amendment
                            </div>
                            <select
                              value={editForm.category_code}
                              onChange={(e) => setEditForm((f) => ({ ...f, category_code: e.target.value }))}
                              style={{ width: '100%', padding: '7px 10px', fontSize: 12,
                                border: `1px solid ${C.border}`, borderRadius: 4,
                                background: C.bgMuted, color: C.textPrimary, marginBottom: 8 }}
                            >
                              {INTENT_CATEGORIES.map((cat) => (
                                <option key={cat} value={cat}>{cat}</option>
                              ))}
                            </select>
                            <select
                              value={editForm.severity}
                              onChange={(e) => setEditForm((f) => ({ ...f, severity: e.target.value }))}
                              style={{ width: '100%', padding: '7px 10px', fontSize: 12,
                                border: `1px solid ${C.border}`, borderRadius: 4,
                                background: C.bgMuted, color: C.textPrimary, marginBottom: 8 }}
                            >
                              {['HIGH', 'MEDIUM', 'LOW'].map((s) => (
                                <option key={s} value={s}>{s}</option>
                              ))}
                            </select>
                            <textarea
                              rows={3}
                              value={editForm.reasoning}
                              onChange={(e) => setEditForm((f) => ({ ...f, reasoning: e.target.value }))}
                              style={{ width: '100%', padding: '7px 10px', fontSize: 12,
                                border: `1px solid ${C.border}`, borderRadius: 4,
                                background: C.bgMuted, color: C.textPrimary,
                                resize: 'none', marginBottom: 10 }}
                            />
                            <div style={{ display: 'flex', gap: 8 }}>
                              <button
                                disabled={editSaving}
                                onClick={() => handleSaveAmend(child)}
                                style={{ flex: 1, padding: '7px 0', fontSize: 12, fontWeight: 500,
                                  background: editSaving ? '#D4D0C9' : C.accent,
                                  border: 'none', borderRadius: 4, color: '#FFFFFF',
                                  cursor: editSaving ? 'not-allowed' : 'pointer' }}
                              >
                                {editSaving ? '…' : 'Save Amendment'}
                              </button>
                              <button
                                onClick={() => setEditingFlagId(null)}
                                style={{ flex: 1, padding: '7px 0', fontSize: 12,
                                  background: C.bgSurface, border: `1px solid ${C.border}`,
                                  borderRadius: 4, color: C.textSecondary, cursor: 'pointer' }}
                              >
                                Cancel
                              </button>
                            </div>
                          </div>
                        ) : (
                          <>
                            {/* Child header: category + badge + Edit button */}
                            <div style={{ display: 'flex', alignItems: 'center',
                              justifyContent: 'space-between', gap: 8 }}>
                              <span style={{ fontSize: 13, fontFamily: MONO, fontWeight: 500,
                                color: state === 'DISMISSED' ? '#9B9890' : C.textPrimary,
                                textTransform: 'uppercase', flex: 1,
                                overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                                {child.category_code}
                              </span>
                              <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexShrink: 0 }}>
                                {showEditChild && (
                                  <FlagActionButton label="Edit"
                                    onClick={(e) => { e.stopPropagation(); openEditForm(child); }}
                                    hoverColor="#0F6E56" hoverBorder="#0F6E56" />
                                )}
                                <DetectionBadge layer={child.detection_layer} />
                              </div>
                            </div>

                            {/* Child severity + confidence */}
                            <div style={{ display: 'flex', alignItems: 'center', gap: 8,
                              marginTop: 8, flexWrap: 'wrap' }}>
                              <VerdictBadge verdict={child.severity} />
                              {child.confidence_score != null && (
                                <span style={{ fontSize: 11, fontFamily: MONO, background: C.bgStatsrow,
                                  border: `1px solid ${C.border}`, borderRadius: 3, padding: '2px 7px',
                                  color: state === 'DISMISSED' ? '#9B9890' : C.textSecondary }}>
                                  {Math.round(child.confidence_score * 100)}%
                                </span>
                              )}
                            </div>

                            {/* Child reasoning */}
                            {child.reasoning && (
                              <div style={{ fontSize: 12,
                                color: state === 'DISMISSED' ? '#9B9890' : C.textSecondary,
                                lineHeight: 1.5, marginTop: 10, paddingTop: 10,
                                borderTop: `1px solid ${C.borderLight}`, fontStyle: 'italic' }}>
                                {child.reasoning}
                              </div>
                            )}
                          </>
                        )}
                      </div>
                    )}
                  </React.Fragment>
                );
              })
            )}
          </div>

          {/* ── Decision panel — hidden when locked ──────────────────────── */}
          {!isLocked && (
          <div style={{ flexShrink: 0, borderTop: `2px solid ${C.border}`, background: C.bgSurface, padding: 20 }}>

            {/* ── L1: PENDING — submit form ────────────────────────────────── */}
            {reviewerRole === 'L1' && status === 'PENDING' && (
              <>
                <div style={{ fontSize: 10, fontFamily: MONO, textTransform: 'uppercase',
                  letterSpacing: '0.06em', color: C.textMuted, marginBottom: 12 }}>
                  Your Decision
                </div>

                {/* Flag progress */}
                {totalFlagCount === 0 ? (
                  <div style={{ fontSize: 12, fontFamily: MONO, color: '#0F6E56', marginBottom: 12 }}>
                    No flags — ready to submit
                  </div>
                ) : (
                  <div style={{ marginBottom: 12 }}>
                    <div style={{ fontSize: 11, fontFamily: MONO, color: C.textSecondary, marginBottom: 6 }}>
                      {actionedFlagCount} of {totalFlagCount} flags reviewed
                    </div>
                    <div style={{ height: 4, background: '#E2DED8', borderRadius: 2 }}>
                      <div style={{
                        height: '100%', borderRadius: 2, background: '#0F6E56',
                        width: `${Math.round((actionedFlagCount / totalFlagCount) * 100)}%`,
                        transition: 'width 300ms ease',
                      }} />
                    </div>
                  </div>
                )}

                {/* Note for L2 */}
                <div style={{ fontSize: 10, fontFamily: MONO, textTransform: 'uppercase',
                  letterSpacing: '0.05em', color: '#9B9890', marginBottom: 6 }}>
                  Note for L2 reviewer (optional)
                </div>
                <textarea
                  rows={2}
                  value={l2Note}
                  onChange={(e) => setL2Note(e.target.value)}
                  onFocus={() => setL2NoteFocused(true)}
                  onBlur={() => setL2NoteFocused(false)}
                  placeholder="Add any context for Amogh..."
                  style={{
                    width: '100%', padding: '10px 12px', fontSize: 13,
                    border: `1px solid ${l2NoteFocused ? C.accent : C.border}`,
                    borderRadius: 5, background: l2NoteFocused ? C.bgSurface : C.bgMuted,
                    color: C.textPrimary, resize: 'none',
                    transition: 'border-color 150ms, background 150ms', marginBottom: 10,
                  }}
                />

                {/* Submit button / success */}
                {submitSuccess ? (
                  <div style={{
                    background: '#E1F5EE', borderRadius: 5, padding: '12px 16px',
                    fontSize: 13, color: '#085041', textAlign: 'center',
                  }}>
                    Submitted for L2 review ✓
                  </div>
                ) : (
                  <div title={!canSubmit
                    ? `Action all flags before submitting — ${unactionedFlagCount} flag(s) remaining`
                    : undefined}>
                    <button
                      disabled={!canSubmit || submitting}
                      onClick={handleSessionSubmit}
                      style={{
                        width: '100%', padding: 11, fontSize: 14, fontWeight: 500,
                        borderRadius: 5, border: 'none',
                        background: (!canSubmit || submitting) ? '#D4D0C9' : '#0F6E56',
                        color: (!canSubmit || submitting) ? C.textMuted : '#FFFFFF',
                        cursor: (!canSubmit || submitting) ? 'not-allowed' : 'pointer',
                        transition: 'background 150ms',
                      }}
                    >
                      {submitting ? 'Submitting…' : 'Submit for L2 Review →'}
                    </button>
                  </div>
                )}
              </>
            )}

            {/* ── L1: SUBMITTED or NFR — read-only status banner ───────────── */}
            {reviewerRole === 'L1' && (isSubmitted || isNeedsFinalReview) && (
              <div style={{
                background: '#EFF6FF', border: '1px solid #B5D4F4',
                borderRadius: 5, padding: '14px 16px',
              }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
                  <span style={{ color: '#185FA5', fontSize: 15 }}>✓</span>
                  <span style={{ fontSize: 14, fontWeight: 500, color: '#185FA5' }}>
                    Submitted for L2 Review
                  </span>
                </div>
                <div style={{ fontSize: 11, fontFamily: MONO, color: '#6B6860' }}>
                  Submitted by {session.submitted_by || '—'}
                  {session.submitted_at ? ` · ${String(session.submitted_at).slice(0, 10)}` : ''}
                </div>
                {isNeedsFinalReview && (
                  <div style={{ fontSize: 11, fontFamily: MONO, color: '#854F0B', marginTop: 8 }}>
                    ⚑ Marked for final review by Amogh
                  </div>
                )}
              </div>
            )}

            {/* ── L2: SUBMITTED or NFR — action panel ──────────────────────── */}
            {reviewerRole === 'L2' && (isSubmitted || isNeedsFinalReview) && (
              <>
                {/* Submission info */}
                <div style={{ fontSize: 11, fontFamily: MONO, color: '#6B6860', marginBottom: 16 }}>
                  Submitted by {session.submitted_by || '—'}
                  {session.submitted_at ? ` on ${String(session.submitted_at).slice(0, 10)}` : ''}
                </div>

                {/* NFR amber alert */}
                {isNeedsFinalReview && (
                  <div style={{
                    background: '#FFFBEB', border: '1px solid #FAC775', color: '#854F0B',
                    borderRadius: 5, padding: '10px 14px', marginBottom: 12,
                    fontSize: 11, fontFamily: MONO,
                  }}>
                    ⚑ Needs Final Review — discuss with team before locking
                  </div>
                )}

                {/* Mark NFR — only on SUBMITTED_FOR_REVIEW */}
                {isSubmitted && (
                  <button
                    disabled={submitting}
                    onClick={handleMarkNeedsFinalReview}
                    style={{
                      width: '100%', padding: 10, fontSize: 13, fontWeight: 500,
                      background: '#FAEEDA', color: '#633806', border: '1px solid #FAC775',
                      borderRadius: 5, cursor: submitting ? 'not-allowed' : 'pointer',
                      marginBottom: 8, transition: 'opacity 150ms',
                    }}
                  >
                    Mark: Needs Final Review
                  </button>
                )}

                {/* Lock */}
                <button
                  disabled={submitting}
                  onMouseEnter={() => setLockBtnHover(true)}
                  onMouseLeave={() => setLockBtnHover(false)}
                  onClick={handleLockSession}
                  style={{
                    width: '100%', padding: 10, fontSize: 13, fontWeight: 500,
                    background: submitting ? '#555' : lockBtnHover ? '#333330' : '#1C1C1A',
                    color: '#FFFFFF', border: 'none',
                    borderRadius: 5, cursor: submitting ? 'not-allowed' : 'pointer',
                    transition: 'background 150ms',
                  }}
                >
                  Lock Session 🔒
                </button>
              </>
            )}

            {/* ── L2: PENDING — waiting for L1 ─────────────────────────────── */}
            {reviewerRole === 'L2' && status === 'PENDING' && (
              <div style={{ fontSize: 12, color: C.textSecondary }}>
                Awaiting L1 review
              </div>
            )}

            {/* ── Legacy: CONFIRMED / OVERRIDDEN / REVIEWED (old workflow) ──── */}
            {status && !['PENDING', 'SUBMITTED_FOR_REVIEW', 'NEEDS_FINAL_REVIEW'].includes(status) && (
              isReviewed && !showUpdateForm ? (
                <div>
                  <div style={{ fontSize: 12, color: C.textSecondary, marginBottom: 6 }}>
                    Reviewed by <b>{session.reviewer_id}</b>
                    {' · '}{STATUS_LABEL[session.review_status] || session.review_status}
                  </div>
                  {session.reviewer_note && (
                    <div style={{ fontSize: 12, color: C.textSecondary, fontStyle: 'italic', marginBottom: 8 }}>
                      "{session.reviewer_note}"
                    </div>
                  )}
                  <button className="btn-link-accent" onClick={() => setShowUpdateForm(true)}
                    style={{ fontSize: 12, color: C.accent, background: 'none', border: 'none', cursor: 'pointer', padding: 0 }}>
                    Update decision
                  </button>
                </div>
              ) : (
                <>
                  <div style={{ fontSize: 10, fontFamily: MONO, textTransform: 'uppercase',
                    letterSpacing: '0.06em', color: C.textMuted, marginBottom: 12 }}>
                    Your Decision
                  </div>
                  <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8, marginBottom: 12 }}>
                    {ACTIONS.map((a) => {
                      const isSelected   = selectedAction === a.key;
                      const clearBlocked = a.key === 'CLEAR' && totalFlagCount > 0;
                      const s = isSelected ? a.active : { bg: C.bgStatsrow, color: C.textSecondary, border: C.border };
                      return (
                        <button
                          key={a.key}
                          disabled={clearBlocked}
                          title={clearBlocked ? 'Clear is only available for sessions with no flags' : undefined}
                          onClick={() => { if (!clearBlocked) { setSelectedAction(a.key); setNoteValidation(false); } }}
                          style={{
                            padding: '9px 0', fontSize: 13, fontWeight: isSelected ? 600 : 500,
                            borderRadius: 5, border: `1px solid ${clearBlocked ? C.border : s.border}`,
                            background: clearBlocked ? C.bgStatsrow : s.bg,
                            color: clearBlocked ? '#C4C0B8' : s.color,
                            cursor: clearBlocked ? 'not-allowed' : 'pointer',
                            outline: isSelected ? `2px solid ${s.border}` : 'none',
                            outlineOffset: 1, transition: 'all 120ms',
                            opacity: clearBlocked ? 0.6 : 1,
                          }}
                        >
                          {a.label}
                        </button>
                      );
                    })}
                  </div>
                  <div style={{ fontSize: 11, fontFamily: MONO,
                    color: noteRequired ? '#A32D2D' : C.textMuted, marginBottom: 6 }}>
                    {noteRequired ? 'Add a note (required for this decision)' : 'Add a note (optional)...'}
                  </div>
                  <textarea rows={3} value={note}
                    onChange={(e) => { setNote(e.target.value); if (e.target.value.trim().length >= 10) setNoteValidation(false); }}
                    onFocus={() => setNoteFocused(true)} onBlur={() => setNoteFocused(false)}
                    style={{
                      width: '100%', padding: '10px 12px', fontSize: 13,
                      border: `1px solid ${noteFocused ? C.accent : C.border}`,
                      borderRadius: 5, background: noteFocused ? C.bgSurface : C.bgMuted,
                      color: C.textPrimary, resize: 'vertical',
                      transition: 'border-color 150ms, background 150ms', marginBottom: 0,
                    }}
                  />
                  {noteValidation && noteRequired && note.trim().length < 10 && (
                    <div style={{ fontSize: 11, fontFamily: MONO, color: '#A32D2D', marginTop: 6 }}>
                      Please explain your reasoning (min 10 characters)
                    </div>
                  )}
                  <div
                    onMouseDown={() => { if (noteRequired && note.trim().length < 10) setNoteValidation(true); }}
                    style={{ marginTop: 10, cursor: submitDisabled ? 'not-allowed' : 'pointer' }}
                  >
                    <button onClick={handleSubmit} disabled={submitDisabled} style={{
                      width: '100%', padding: 11, fontSize: 14, fontWeight: 500,
                      borderRadius: 5, border: 'none',
                      background: submitDisabled ? '#D4D0C9' : C.accent,
                      color: submitDisabled ? C.textMuted : '#FFFFFF',
                      cursor: submitDisabled ? 'not-allowed' : 'pointer',
                      pointerEvents: submitDisabled ? 'none' : 'auto',
                      animation: submitting ? 'pulse 1s ease-in-out infinite' : 'none',
                      transition: 'background 150ms',
                    }}>
                      {submitting ? 'Saving…' : 'Submit Review'}
                    </button>
                  </div>
                </>
              )
            )}
          </div>
          )}
        </div>
      </div>

      {toast && <Toast message={toast} />}
    </div>
  );
}
