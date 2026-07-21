import React, { useState, useEffect, useRef, useCallback, useMemo } from 'react';
import { C, MONO } from '../tokens';
import VerdictBadge from './VerdictBadge';
import SeverityBadge from './SeverityBadge';
import {
  getSessionDetail, getSessionFlags, submitReview,
  manualFlag, saveSessionNote,
  confirmFlag, confirmAllFlags, dismissAllFlags, submitSession, markNeedsFinalReview,
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
  'SELF_HARM',
  'VIOLENCE',
  'INSTIGATION',
  'COMPETITOR_PROMOTION',
  'EXTERNAL_MEDIA_CONTENT',
  'OTHER',
];

// ---------------------------------------------------------------------------
// Active flag resolution
// Returns the display-level flag list: for each original flag, if an
// amendment exists (parent_flag_id set) return the amendment; otherwise
// return the original. This mirrors the backend's active row logic.
// ---------------------------------------------------------------------------
function getActiveFlags(flags) {
  const amendedParentIds = new Set(
    flags.filter((f) => f.parent_flag_id != null).map((f) => f.parent_flag_id)
  );
  return flags.filter((f) => {
    if (f.parent_flag_id != null) return true;   // amendment — this is the active version
    if (amendedParentIds.has(f.flag_id)) return false; // original that has been amended — skip
    return true;                                  // original with no amendment — active
  });
}

// ---------------------------------------------------------------------------
// Flag → turn association
// Priority 1: direct turn_id match (value equality, not index arithmetic)
// Priority 2: pattern_matched exact substring containment (for null turn_id)
// Priority 3: fuzzy word-overlap on reasoning (non-MANUAL, null turn_id only)
// DISMISSED flags are excluded — they should not generate transcript badges.
// ---------------------------------------------------------------------------
function buildFlagsByTurnIdx(turns, flags) {
  const result = {};
  // Only show active flags (amendments are the active version of a flag)
  const activeFlags = getActiveFlags(flags);
  activeFlags.forEach((flag) => {

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
function layerStyle(source) {
  if (source === 'LLM')    return { bg: C.llmBg,    text: C.llmText,    border: C.llmBorder    };
  if (source === 'MANUAL') return { bg: C.manualBg, text: C.manualText, border: C.manualBorder };
  return                           { bg: C.regexBg,  text: C.regexText,  border: C.regexBorder  };
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
  // NOTE: turn hover is handled purely in CSS (.turn-row:hover .turn-flag-btn)
  // so moving the mouse across a long transcript no longer re-renders every turn.
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
  const [dismissSaving,      setDismissSaving]      = useState(false);
  const [confirmingFlagId,   setConfirmingFlagId]   = useState(null);
  const [confirmingAll,      setConfirmingAll]      = useState(false);
  const [dismissingAll,      setDismissingAll]      = useState(false);
  const [flagCardHoverId,    setFlagCardHoverId]    = useState(null);

  // Workflow state
  const [l2Note,             setL2Note]             = useState('');
  const [l2NoteFocused,      setL2NoteFocused]      = useState(false);
  const [submitSuccess,      setSubmitSuccess]      = useState(false);
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
    setDismissSaving(false);
    setConfirmingFlagId(null);
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
        // Capture the real turn_id so the flag attributes to a speaker
        // (astrologer / user) for per-speaker counts.
        turn_id:       turn.turn_id ?? null,
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

  // Refresh both the flags list AND the session object (so the header verdict
  // badge reflects the recomputed overall_verdict after a flag change).
  const refreshSessionAndFlags = async () => {
    const detail = await getSessionDetail(sessionId);
    setData(detail);
    setFlags(detail.flags || []);
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
      const res = await fetch(`/flags/${flag.flag_id}/amend`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...editForm, reviewer_id: reviewerName }),
      });
      if (!res.ok) throw new Error('amend failed');
      await refreshSessionAndFlags();
      setEditingFlagId(null);
      setToast('Flag updated');
      setTimeout(() => setToast(null), 2000);
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
        body: JSON.stringify({ reviewer_id: reviewerName, note: '' }),
      });
      if (!res.ok) throw new Error('dismiss failed');
      await refreshSessionAndFlags();
      setDismissingFlagId(null);
    } catch (_) {
    } finally {
      setDismissSaving(false);
    }
  };

  // ── Flag confirm handler ──────────────────────────────────────────────────
  const handleConfirmFlag = async (flag) => {
    if (confirmingFlagId === flag.flag_id) return;
    setConfirmingFlagId(flag.flag_id);
    try {
      await confirmFlag(flag.flag_id, reviewerName);
      await refreshSessionAndFlags();
      setToast('Flag confirmed');
      setTimeout(() => setToast(null), 2000);
    } catch (_) {
    } finally {
      setConfirmingFlagId(null);
    }
  };

  // ── Confirm-all handler: confirm every unconfirmed active flag at once ──────
  const handleConfirmAll = async (count) => {
    if (confirmingAll) return;
    // eslint-disable-next-line no-alert
    if (!window.confirm(`Confirm all ${count} flag${count === 1 ? '' : 's'} for this session?`)) return;
    setConfirmingAll(true);
    try {
      const res = await confirmAllFlags(sessionId, reviewerName);
      await refreshSessionAndFlags();
      setToast(`Confirmed ${res?.confirmed_count ?? count} flags`);
      setTimeout(() => setToast(null), 2000);
    } catch (_) {
    } finally {
      setConfirmingAll(false);
    }
  };

  // ── Dismiss-all handler: dismiss every unconfirmed active flag at once ──────
  const handleDismissAll = async (count) => {
    if (dismissingAll) return;
    // eslint-disable-next-line no-alert
    if (!window.confirm(`Dismiss all ${count} flag${count === 1 ? '' : 's'} for this session? This permanently deletes them.`)) return;
    setDismissingAll(true);
    try {
      const res = await dismissAllFlags(sessionId, reviewerName);
      await refreshSessionAndFlags();
      setToast(`Dismissed ${res?.dismissed_count ?? count} flags`);
      setTimeout(() => setToast(null), 2000);
    } catch (_) {
    } finally {
      setDismissingAll(false);
    }
  };

  // ── Session workflow handlers ──────────────────────────────────────────────
  const handleSessionSubmit = async () => {
    if (submitting) return;
    setSubmitting(true);
    try {
      // Note is fully optional — pass whatever the reviewer typed, or null.
      await submitSession(sessionId, reviewerName, l2Note.trim() || null);
      // Refresh in place so the panel switches to the submitted state.
      // Do NOT auto-navigate — let the reviewer choose Next / Prev / Queue.
      await refreshSessionAndFlags();
      setToast('Submitted for review');
      setTimeout(() => setToast(null), 2000);
    } catch (err) {
      setToast(`Error: ${err.message}`);
    } finally {
      setSubmitting(false);
    }
  };

  const handleMarkNeedsFinalReview = async () => {
    setSubmitting(true);
    try {
      await markNeedsFinalReview(sessionId, reviewerName);
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
        body: JSON.stringify({ reviewer_id: reviewerName }),
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
  // ── Memoized derived data ─────────────────────────────────────────────────
  // These were previously recomputed on EVERY render — including on each mouse
  // hover over a turn and on every keystroke in the note fields. buildFlagsByTurnIdx
  // is an O(turns × flags) fuzzy match, so on long sessions that made the
  // transcript feel sluggish. Keying them to the data they depend on means they
  // only recompute when the transcript or flag list actually changes.
  const displayedFlags  = useMemo(() => getActiveFlags(flags), [flags]);
  const flagsByTurnIdx  = useMemo(
    () => (data ? buildFlagsByTurnIdx(data.turns || [], flags) : {}),
    [data, flags],
  );
  const firstFlaggedIdx = useMemo(
    () => (data?.turns || []).findIndex((_, i) => flagsByTurnIdx[i]?.length > 0),
    [data, flagsByTurnIdx],
  );

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
  const status             = session?.review_status;
  const isLocked           = status === 'LOCKED';
  const isSubmitted        = status === 'SUBMITTED_FOR_REVIEW';
  const isNeedsFinalReview = status === 'NEEDS_FINAL_REVIEW';
  const isReviewed         = status && status !== 'PENDING' && session.reviewer_id;
  // Flag editability by role:
  //  - L1 can edit/dismiss/confirm only while the session is still PENDING.
  //    Once they submit for L2 review, their flags freeze (read-only).
  //  - L2 (final reviewer) can edit/dismiss/confirm any session that is not
  //    yet LOCKED — including SUBMITTED_FOR_REVIEW / NEEDS_FINAL_REVIEW.
  const flagsEditable = !isLocked && (
    reviewerRole === 'L2' ? true : status === 'PENDING'
  );

  // Flag summary derived client-side — drives L1 submit eligibility.
  // Uses the new model: active flags = amendment-or-original (via getActiveFlags),
  // actioned = status === 'CONFIRMED'.
  const activeFlagsForSummary = displayedFlags;
  const totalFlagCount      = activeFlagsForSummary.length;
  const actionedFlagCount   = activeFlagsForSummary.filter((f) => f.status === 'CONFIRMED').length;
  const unactionedFlagCount = totalFlagCount - actionedFlagCount;
  const canSubmit           = unactionedFlagCount === 0;

  // ── Active flag resolution ───────────────────────────────────────────────
  // Show only the active version of each flag: amendment if it exists, else original.
  // No AMENDED/DISMISSED labels — every flag card looks fresh.
  // (displayedFlags is memoized above.)
  const activeFlagCount = displayedFlags.length;

  // ── Per-speaker flag attribution (who was flagged, how many times) ────────
  const astrologerFlagCount = displayedFlags.filter((f) => f.flagged_speaker === 'ASTROLOGER').length;
  const userFlagCount       = displayedFlags.filter((f) => f.flagged_speaker === 'USER').length;

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

    // AUTHORITATIVE: if the flag has a turn_id, jump straight to that turn.
    // Never fall through to fuzzy text matching — that's what caused flags to
    // land on the wrong message (e.g. message 15 jumping to message 4).
    if (flag.turn_id != null) {
      const idx = turns.findIndex((t) => String(t.turn_id) === String(flag.turn_id));
      if (idx >= 0) { doScroll(idx); return; }
      return; // turn_id set but not found — do not guess
    }

    // ── Fuzzy fallback below — ONLY for legacy flags with no turn_id ──────────

    // Step 0: pattern_matched exact substring (reliable for MANUAL flags whose
    // pattern_matched is the first 200 chars of the flagged message_text)
    {
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
          {!loading && <SeverityBadge severity={session.astrotalk_severity} labeled />}
          {!loading && (
            <span style={{
              fontSize: 11, fontFamily: MONO, padding: '2px 8px', borderRadius: 3,
              background: session.astrotalk_flagged === 1 ? C.severeBg : C.bgStatsrow,
              color:      session.astrotalk_flagged === 1 ? C.severeText : C.textSecondary,
              border: `1px solid ${session.astrotalk_flagged === 1 ? C.severeBorder : C.border}`,
            }}>
              AstroTalk: {session.astrotalk_flagged === 1 ? 'Flagged' : 'Clean'}
            </span>
          )}
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
              const isPopoverOpen  = openFlagPopover === idx;

              const maxSev = turnFlags.length
                ? (turnFlags.some((f) => f.severity === 'HIGH') ? 'HIGH'
                  : turnFlags.some((f) => f.severity === 'MEDIUM') ? 'MEDIUM' : 'LOW')
                : null;
              const flagColor     = maxSev === 'HIGH' ? C.severeBorder : maxSev === 'MEDIUM' ? C.flaggedBorder : maxSev === 'LOW' ? C.cleanBorder : null;
              const flagBg        = maxSev === 'HIGH' ? C.severeBg    : maxSev === 'MEDIUM' ? C.flaggedBg    : maxSev === 'LOW' ? C.cleanBg    : null;
              const flagTextColor = maxSev === 'HIGH' ? C.severeText  : maxSev === 'MEDIUM' ? C.flaggedText  : maxSev === 'LOW' ? C.cleanText  : null;

              return (
                <div
                  key={turn.turn_id ?? idx}
                  className="turn-row"
                  ref={(el) => { turnRefs.current[idx] = el; if (isFirstFlagged) firstFlaggedRef.current = el; }}
                  style={{ display: 'flex', flexDirection: 'column',
                    alignItems: isAstrologer ? 'flex-start' : 'flex-end',
                    marginBottom: isPopoverOpen ? 0 : 16, position: 'relative' }}
                >
                  {/* Category badges above bubble */}
                  {turnFlags.length > 0 && (
                    <div style={{ display: 'flex', gap: 4, marginBottom: 4, flexWrap: 'wrap' }}>
                      {turnFlags.map((f, fi) => (
                        <span key={fi} style={{
                          fontSize: 10, fontFamily: MONO, fontWeight: 500,
                          padding: '1px 6px', borderRadius: 3, textTransform: 'uppercase',
                          background: flagBg, color: flagTextColor, border: `1px solid ${flagColor}`,
                        }}>
                          {f.category_code}
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
                      background: turnFlags.length > 0 ? flagBg : (isAstrologer ? '#F1F5F9' : '#EFF6FF'),
                      color: turnFlags.length > 0 ? flagTextColor : C.textPrimary,
                      border: turnFlags.length > 0 ? `1px solid ${flagColor}` : undefined,
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

                    {/* + Flag button — hidden when locked. Revealed on row hover via
                        CSS (.turn-row:hover .turn-flag-btn); forced visible while its
                        popover is open. Kept out of React hover state so moving the
                        mouse over the transcript doesn't re-render every turn. */}
                    {!isLocked && (
                      <button
                        className="turn-flag-btn"
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
                          ...(isPopoverOpen ? { opacity: 1, pointerEvents: 'auto' } : null),
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
              letterSpacing: '0.08em', color: C.textMuted, marginBottom: 6 }}>
              {activeFlagCount > 0 ? `Flags Detected (${activeFlagCount})` : 'Flags Detected'}
            </div>

            {/* Per-speaker attribution — who was flagged, how many times */}
            {activeFlagCount > 0 && (astrologerFlagCount > 0 || userFlagCount > 0) && (
              <div style={{ display: 'flex', gap: 8, marginBottom: 14, flexWrap: 'wrap' }}>
                {astrologerFlagCount > 0 && (
                  <span style={{
                    fontSize: 10, fontFamily: MONO, padding: '2px 8px', borderRadius: 3,
                    background: '#FCEFE6', color: '#9A4A18', border: '1px solid #F2C9A8',
                  }}>
                    Astrologer: {astrologerFlagCount}
                  </span>
                )}
                {userFlagCount > 0 && (
                  <span style={{
                    fontSize: 10, fontFamily: MONO, padding: '2px 8px', borderRadius: 3,
                    background: '#E6F1FB', color: '#0C447C', border: '1px solid #B5D4F4',
                  }}>
                    User: {userFlagCount}
                  </span>
                )}
              </div>
            )}

            {/* Confirm-all / Dismiss-all: bulk-action every unconfirmed active flag.
                Gated by flagsEditable (same as the per-flag buttons) so it never
                renders on a LOCKED session or one that has frozen for this role. */}
            {!loading && flagsEditable && unactionedFlagCount >= 2 && (
              <div style={{ marginBottom: 14, display: 'flex', gap: 8 }}>
                <button
                  onClick={() => handleConfirmAll(unactionedFlagCount)}
                  disabled={confirmingAll || dismissingAll}
                  style={{
                    fontSize: 12, fontFamily: MONO, fontWeight: 600,
                    padding: '6px 14px', borderRadius: 4,
                    cursor: confirmingAll || dismissingAll ? 'default' : 'pointer',
                    background: C.accent, color: '#FFFFFF',
                    border: `1px solid ${C.accent}`,
                    opacity: confirmingAll || dismissingAll ? 0.6 : 1,
                  }}
                >
                  {confirmingAll ? 'Confirming…' : `Confirm All (${unactionedFlagCount})`}
                </button>
                <button
                  onClick={() => handleDismissAll(unactionedFlagCount)}
                  disabled={confirmingAll || dismissingAll}
                  style={{
                    fontSize: 12, fontFamily: MONO, fontWeight: 600,
                    padding: '6px 14px', borderRadius: 4,
                    cursor: confirmingAll || dismissingAll ? 'default' : 'pointer',
                    background: '#A32D2D', color: '#FFFFFF',
                    border: '1px solid #A32D2D',
                    opacity: confirmingAll || dismissingAll ? 0.6 : 1,
                  }}
                >
                  {dismissingAll ? 'Dismissing…' : `Dismiss All (${unactionedFlagCount})`}
                </button>
              </div>
            )}

            {loading ? <SkeletonPane /> : displayedFlags.length === 0 ? (
              <div style={{ fontSize: 13, color: C.textSecondary, fontStyle: 'italic' }}>
                No flags detected for this session.
              </div>
            ) : (
              displayedFlags.map((flag, fi) => {
                const ls            = layerStyle(flag.source || flag.detection_layer);
                const isEditing     = editingFlagId === flag.flag_id;
                const isDismissConf = dismissingFlagId === flag.flag_id;
                const isHoveredCard = flagCardHoverId === flag.flag_id;
                const isConfirming  = confirmingFlagId === flag.flag_id;
                const isConfirmedStatus = flag.status === 'CONFIRMED';
                const scrollMsg     = flagScrollMsg[flag.flag_id];

                const isSevere = flag.severity === 'SEVERE' || flag.severity === 'HIGH' || flag.severity === 'RED';
                const isFlagged = flag.severity === 'FLAGGED' || flag.severity === 'MEDIUM' || flag.severity === 'AMBER';

                return (
                  <div
                    key={flag.flag_id ?? fi}
                    onClick={() => !isEditing && !isDismissConf && handleFlagCardClick(flag)}
                    onMouseEnter={() => setFlagCardHoverId(flag.flag_id)}
                    onMouseLeave={() => setFlagCardHoverId(null)}
                    style={{
                      background: isConfirmedStatus
                        ? (isSevere ? C.severeBg : isFlagged ? C.flaggedBg : C.cleanBg)
                        : (isHoveredCard && !isEditing && !isDismissConf ? '#FAFAF8' : C.bgSurface),
                      border: `1px solid ${
                        isConfirmedStatus
                        ? (isSevere ? C.severeBorder : isFlagged ? C.flaggedBorder : C.cleanBorder)
                        : isHoveredCard && !isEditing && !isDismissConf ? '#D4D0C9'
                        : C.border}`,
                      borderRadius: 6, padding: '14px 16px', marginBottom: 10,
                      cursor: isEditing || isDismissConf ? 'default' : 'pointer',
                      transition: 'background 150ms, border-color 150ms',
                    }}
                  >
                    {isEditing ? (
                      /* ── Inline edit form ── */
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
                            onClick={() => handleSaveAmend(flag)}
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
                        {/* Category + action buttons + Detection Layer tab */}
                        <div style={{ display: 'flex', alignItems: 'center',
                          justifyContent: 'space-between', gap: 8 }}>
                          <span style={{ fontSize: 13, fontFamily: MONO, fontWeight: 500,
                            color: C.textPrimary, textTransform: 'uppercase', flex: 1,
                            overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                            {flag.category_code}
                            {isConfirmedStatus && (
                              <span style={{ marginLeft: 6, fontSize: 10, fontFamily: MONO,
                                color: '#085041', background: '#E1F5EE', border: '1px solid #9FE1CB',
                                borderRadius: 3, padding: '1px 5px' }}>
                                ✓ Confirmed
                              </span>
                            )}
                          </span>
                          <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexShrink: 0 }}>
                            {/* Action buttons — only while session is PENDING (editable) */}
                            {flagsEditable && !isConfirmedStatus && (
                              <FlagActionButton
                                label={confirmingFlagId === flag.flag_id ? '…' : 'Confirm'}
                                onClick={(e) => { e.stopPropagation(); handleConfirmFlag(flag); }}
                                hoverColor="#085041" hoverBorder="#9FE1CB" />
                            )}
                            {flagsEditable && (
                              <FlagActionButton label="Edit"
                                onClick={(e) => { e.stopPropagation(); openEditForm(flag); }}
                                hoverColor="#0F6E56" hoverBorder="#0F6E56" />
                            )}
                            {flagsEditable && (
                              <FlagActionButton label="Dismiss"
                                onClick={(e) => { e.stopPropagation(); setDismissingFlagId(flag.flag_id); }}
                                hoverColor="#A32D2D" hoverBorder="#F7C1C1" />
                            )}
                            {/* Detection Layer tab — read-only, shows source */}
                            <DetectionBadge layer={flag.source || flag.detection_layer || 'LLM'} />
                          </div>
                        </div>

                        {/* Severity + confidence + FP risk + flagged speaker */}
                        <div style={{ display: 'flex', alignItems: 'center', gap: 8,
                          marginTop: 8, flexWrap: 'wrap' }}>
                          <VerdictBadge verdict={flag.severity} />
                          {flag.confidence_score != null && (
                            <span style={{ fontSize: 11, fontFamily: MONO, background: C.bgStatsrow,
                              border: `1px solid ${C.border}`, borderRadius: 3, padding: '2px 7px',
                              color: C.textSecondary }}>
                              {Math.round(flag.confidence_score * 100)}%
                            </span>
                          )}
                          {flag.flagged_speaker && (
                            <span style={{
                              fontSize: 10, fontFamily: MONO, padding: '2px 7px', borderRadius: 3,
                              textTransform: 'capitalize',
                              background: flag.flagged_speaker === 'ASTROLOGER' ? '#FCEFE6' : '#E6F1FB',
                              color:      flag.flagged_speaker === 'ASTROLOGER' ? '#9A4A18' : '#0C447C',
                              border:     `1px solid ${flag.flagged_speaker === 'ASTROLOGER' ? '#F2C9A8' : '#B5D4F4'}`,
                            }}>
                              {flag.flagged_speaker === 'ASTROLOGER' ? 'Astrologer' : 'User'}
                            </span>
                          )}
                          {flag.false_positive_risk && (
                            <span style={{ fontSize: 11, color: C.textSecondary }}>
                              FP risk: <b>{flag.false_positive_risk}</b>
                            </span>
                          )}
                        </div>

                        {/* Reasoning */}
                        {flag.reasoning && (
                          <div style={{ fontSize: 12, color: C.textSecondary,
                            lineHeight: 1.5, marginTop: 10, paddingTop: 10,
                            borderTop: `1px solid ${C.borderLight}`, fontStyle: 'italic' }}>
                            {flag.reasoning}
                          </div>
                        )}

                        {/* Flagged by — only for MANUAL source flags */}
                        {flag.flagged_by && (
                          <div style={{ fontSize: 11, fontFamily: MONO,
                            color: C.textMuted, marginTop: 6 }}>
                            Flagged by {flag.flagged_by}
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
                              Dismiss this flag? This cannot be undone.
                            </div>
                            <div style={{ display: 'flex', gap: 8 }}>
                              <button
                                disabled={dismissSaving}
                                onClick={() => handleConfirmDismiss(flag)}
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
                    {scrollMsg && !isEditing && (
                      <div style={{ fontSize: 10, fontFamily: MONO, marginTop: 6, color: '#0F6E56' }}>
                        ↑ Viewing in transcript
                      </div>
                    )}

                    {/* Confirming in-progress indicator */}
                    {isConfirming && (
                      <div style={{ fontSize: 10, fontFamily: MONO, color: '#085041', marginTop: 4 }}>
                        Confirming…
                      </div>
                    )}
                  </div>
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
                      const clearBlocked = a.key === 'CLEAR' && activeFlagCount > 0;
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
