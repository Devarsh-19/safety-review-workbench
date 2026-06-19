import React, { useState, useEffect, useCallback } from 'react';
import { C, MONO } from '../tokens';
import TopBar from './TopBar';
import Footer from './Footer';
import VerdictBadge from './VerdictBadge';
import StatusBadge from './StatusBadge';
import LoadingSpinner from './LoadingSpinner';
import { getSessions, getStats, submitReview, getReviewerStats, exportCsv, getViolationStats } from '../api';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function truncate(str, n) {
  if (!str) return '—';
  return str.length > n ? str.slice(0, n) + '…' : str;
}

// Session type pill — Chat (blue) / Voice (purple)
function SessionTypePill({ stype }) {
  if (!stype) return <span style={{ color: C.textMuted }}>—</span>;
  const isVoice = stype === 'voice';
  return (
    <span style={{
      fontSize: 10, fontFamily: MONO, padding: '2px 8px', borderRadius: 3,
      background: isVoice ? C.voiceBg   : C.chatBg,
      color:      isVoice ? C.voiceText  : C.chatText,
      border:     `1px solid ${isVoice ? C.voiceBorder : C.chatBorder}`,
      textTransform: 'capitalize',
    }}>
      {stype}
    </span>
  );
}

function formatDuration(minutes) {
  if (!minutes && minutes !== 0) return '—';
  if (minutes < 60)   return `${Math.round(minutes)} min`;
  if (minutes < 1440) return `${(minutes / 60).toFixed(1)} hrs`;
  return `${(minutes / 1440).toFixed(1)} days`;
}

// Feature 7 — Reviewer progress table columns
const PROGRESS_COLS = ['Reviewer', 'Reviewed', 'Confirmed', 'False Pos.', 'Escalated', 'Cleared'];

// Column keys used for client-side sorting
const SORT_KEYS = {
  'Session ID':   'session_id',
  'Duration':     'duration_minutes',
  'Turns':        'turn_count',
  'Flags':        'flag_count',
  'LLM Flags':    'llm_flag_count',
  'Manual Flags': 'manual_flag_count',
};

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export default function SessionQueue({ reviewerName, reviewerRole, onSelectSession }) {
  // ── Server-side filtered state ─────────────────────────────────────────
  const [sessions,      setSessions]      = useState([]);
  const [stats,         setStats]         = useState(null);
  const [loading,       setLoading]       = useState(true);
  const [verdictFilter, setVerdictFilter] = useState('');
  const [statusFilter,  setStatusFilter]  = useState('');
  const [hoveredRow,    setHoveredRow]    = useState(null);

  // ── Client-side filter state ───────────────────────────────────────────
  const [searchQuery,   setSearchQuery]   = useState('');       // Feature 6
  const [searchFocused, setSearchFocused] = useState(false);
  const [minConfidence, setMinConfidence] = useState(0);        // Feature 8

  // ── Action state ───────────────────────────────────────────────────────
  const [clearingSession, setClearingSession] = useState(null); // Feature 4
  const [exporting,       setExporting]       = useState(false);// Feature 5

  // ── Team progress state ────────────────────────────────────────────────
  const [showProgress,    setShowProgress]    = useState(false);// Feature 7
  const [reviewerStats,   setReviewerStats]   = useState([]);
  const [loadingProgress, setLoadingProgress] = useState(false);
  // L2 Reviewer Progress (assignment breakdown) — collapsible, expanded by default
  const [showReviewerProgress, setShowReviewerProgress] = useState(true);

  // ── Violation heatmap state ────────────────────────────────────────────
  const [showHeatmap,     setShowHeatmap]     = useState(false);
  const [heatmapData,     setHeatmapData]     = useState([]);
  const [loadingHeatmap,  setLoadingHeatmap]  = useState(false);

  // ── Column filter state ────────────────────────────────────────────────
  const [colFilterId,     setColFilterId]     = useState('');
  const [colFilterAstro,  setColFilterAstro]  = useState('');   // '', 'flagged', 'clean'
  const [colFilterLang,   setColFilterLang]   = useState('');
  const [colFilterType,   setColFilterType]   = useState('');
  const [colFilterMinDur,   setColFilterMinDur]   = useState('');
  const [colFilterMaxDur,   setColFilterMaxDur]   = useState('');
  const [colFilterMinTurns, setColFilterMinTurns] = useState('');
  const [colFilterMaxTurns, setColFilterMaxTurns] = useState('');
  const [focusedFilter,     setFocusedFilter]     = useState(null);

  // ── Sort state ─────────────────────────────────────────────────────────
  const [sortCol,         setSortCol]         = useState(null);
  const [sortDir,         setSortDir]         = useState(null);

  // ── Assignee filter (L2 only) ──────────────────────────────────────────
  const [assigneeFilter,  setAssigneeFilter]  = useState('');

  // ── Dynamic column list — L2 gets an 'Assigned To' column after Session ID
  const COLS = reviewerRole === 'L2'
    ? ['Session ID', 'Assigned To', 'Verdict', 'AstroTalk', 'Flags', 'LLM Flags', 'Manual Flags', 'Language', 'Type', 'Duration', 'Turns', 'Status', 'Reviewer', 'Action']
    : ['Session ID', 'Verdict', 'AstroTalk', 'Flags', 'LLM Flags', 'Manual Flags', 'Language', 'Type', 'Duration', 'Turns', 'Status', 'Reviewer', 'Action'];

  // ── Data fetching ──────────────────────────────────────────────────────

  const fetchAll = useCallback(() => {
    return Promise.all([
      getSessions({
        verdict:       verdictFilter   || undefined,
        status:        statusFilter    || undefined,
        reviewer_name: reviewerName    || undefined,
        reviewer_role: reviewerRole    || undefined,
        assigned_to:   assigneeFilter  || undefined,
      }),
      getStats({ reviewer_name: reviewerName || undefined, reviewer_role: reviewerRole || undefined }),
    ]).then(([sess, st]) => {
      setSessions(sess);
      setStats(st);
    }).catch(() => {});
  }, [verdictFilter, statusFilter, reviewerName, reviewerRole, assigneeFilter]);

  useEffect(() => {
    setLoading(true);
    fetchAll().finally(() => setLoading(false));
  }, [fetchAll]);

  useEffect(() => {
    const id = setInterval(fetchAll, 30_000);
    return () => clearInterval(id);
  }, [fetchAll]);

  // ── Derived values ─────────────────────────────────────────────────────

  const clearFilters = () => { setVerdictFilter(''); setStatusFilter(''); setAssigneeFilter(''); };
  const hasFilters   = verdictFilter || statusFilter || assigneeFilter;

  const severe           = stats?.count_severe              ?? 0;
  const flagged          = stats?.count_flagged             ?? 0;
  const clean            = stats?.count_clean               ?? 0;
  const unprocessed      = stats?.count_unprocessed         ?? 0;
  const locked           = stats?.count_locked              ?? 0;
  const submitted        = stats?.count_submitted           ?? 0;
  const needsFinalReview = stats?.count_needs_final_review  ?? 0;
  const total            = stats?.total_sessions            ?? 0;
  const pending          = stats?.total_pending             ?? 0;
  const reviewed         = total - pending;

  const statCells = [
    { label: 'Severe',         value: severe,           color: C.severeText  },
    { label: 'Flagged',        value: flagged,           color: C.flaggedText },
    { label: 'Clean',          value: clean,             color: C.cleanText   },
    { label: 'Unprocessed',    value: unprocessed,       color: '#444441'     },
    { label: 'Locked',         value: locked,            color: '#444441'     },
    { label: 'Submitted',      value: submitted,         color: '#185FA5'     },
    { label: 'Needs Review',   value: needsFinalReview,  color: '#854F0B'     },
    { label: 'Total sessions', value: total,             color: C.textPrimary },
    { label: 'Pending review', value: pending,           color: C.accent      },
  ];

  const handleSortClick = (colLabel) => {
    const key = SORT_KEYS[colLabel];
    if (!key) return;
    if (sortCol !== key)       { setSortCol(key); setSortDir('asc'); }
    else if (sortDir === 'asc') { setSortDir('desc'); }
    else                        { setSortCol(null); setSortDir(null); }
  };

  // Client-side filters (column filters AND-ed with top-bar filters) + optional sort
  let displayedSessions = sessions
    .filter((s) => !searchQuery.trim()    || s.session_id.includes(searchQuery.trim()))
    .filter((s) => minConfidence === 0    || ((s.confidence_score ?? 0) * 100 >= minConfidence))
    .filter((s) => !colFilterId.trim()    || s.session_id.includes(colFilterId.trim()))
    .filter((s) => !colFilterAstro        || (colFilterAstro === 'flagged' ? s.astrotalk_flagged === 1 : s.astrotalk_flagged !== 1))
    .filter((s) => !colFilterLang.trim()  || (s.language_detected || '').toLowerCase().includes(colFilterLang.trim().toLowerCase()))
    .filter((s) => !colFilterType         || s.session_type === colFilterType)
    .filter((s) => !colFilterMinDur         || (s.duration_minutes ?? 0) >= Number(colFilterMinDur))
    .filter((s) => !colFilterMaxDur         || (s.duration_minutes ?? 0) <= Number(colFilterMaxDur))
    .filter((s) => !colFilterMinTurns       || (s.turn_count ?? 0) >= Number(colFilterMinTurns))
    .filter((s) => !colFilterMaxTurns       || (s.turn_count ?? 0) <= Number(colFilterMaxTurns));
  if (sortCol && sortDir) {
    displayedSessions = [...displayedSessions].sort((a, b) => {
      const va = a[sortCol] ?? -Infinity;
      const vb = b[sortCol] ?? -Infinity;
      const cmp = va < vb ? -1 : va > vb ? 1 : 0;
      return sortDir === 'asc' ? cmp : -cmp;
    });
  }

  const noResults     = displayedSessions.length === 0;
  const emptyMessage  = sessions.length === 0
    ? 'No sessions match the selected filters.'
    : 'No sessions match the search or confidence filter.';
  const showClearLink = sessions.length === 0 && hasFilters;
  const heatmapMax    = heatmapData.length > 0 ? Math.max(...heatmapData.map(d => d.count)) : 1;

  // ── Feature 4 — Quick-clear ────────────────────────────────────────────

  const handleQuickClear = async (sid) => {
    if (clearingSession) return;
    setClearingSession(sid);
    try {
      await submitReview(sid, 'CLEAR', reviewerName, 'Cleared from queue without full review', null);
      await fetchAll();
    } catch (_) {
    } finally {
      setClearingSession(null);
    }
  };

  const handleLockSession = async (sid) => {
    if (!window.confirm('Lock this session? This will freeze all flags and the review decision.')) return;
    try {
      await fetch(`/sessions/${sid}/lock`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ reviewer_id: reviewerName }),
      });
      await fetchAll();
    } catch (_) {}
  };

  // ── Feature 5 — Export CSV ─────────────────────────────────────────────

  const handleExport = () => {
    setExporting(true);
    // L1 reviewers export only their own submitted sessions; L2 exports all.
    exportCsv(reviewerName, reviewerRole);
    setTimeout(() => setExporting(false), 1000);
  };

  // ── Violation heatmap ──────────────────────────────────────────────────

  const CATEGORY_COLORS = {
    OFF_PLATFORM_SOLICITATION:   '#0F6E56',
    NSFW:                        '#A32D2D',
    NSFW_EXPLICIT:               '#A32D2D',
    NSFW_GROOMING:               '#A32D2D',
    NSFW_APPEARANCE:             '#854F0B',
    CSAM_RISK:                   '#6B0000',
    FEAR_MANIPULATION:           '#854F0B',
    FINANCIAL_SOLICITATION:      '#854F0B',
    PERSONAL_DATA_COLLECTION:    '#185FA5',
    ABUSIVE_LANGUAGE:            '#A32D2D',
    HATE_SPEECH:                 '#A32D2D',
    IDENTITY_FRAUD:              '#185FA5',
    FAKE_REMEDIES:               '#854F0B',
    UNAUTHORIZED_MEDICAL_ADVICE: '#3B6D11',
    SELF_HARM:                   '#6B0000',
    VIOLENCE:                    '#A32D2D',
    INSTIGATION:                 '#8A2BE2',
    COMPETITOR_PROMOTION:        '#6B6860',
    RE_ENGAGEMENT_SOLICITATION:  '#0F6E56',
    EXTERNAL_MEDIA_CONTENT:      '#185FA5',
    OTHER:                       '#6B6860',
  };

  const handleToggleHeatmap = () => {
    const next = !showHeatmap;
    setShowHeatmap(next);
    if (next) {
      setLoadingHeatmap(true);
      getViolationStats()
        .then((data) => { setHeatmapData(data); setLoadingHeatmap(false); })
        .catch(() => setLoadingHeatmap(false));
    }
  };

  // ── Feature 7 — Team Progress toggle ──────────────────────────────────

  const handleToggleProgress = () => {
    const next = !showProgress;
    setShowProgress(next);
    if (next && reviewerStats.length === 0) {
      setLoadingProgress(true);
      getReviewerStats()
        .then((data) => { setReviewerStats(data); setLoadingProgress(false); })
        .catch(() => setLoadingProgress(false));
    }
  };

  // ── Shared styles ──────────────────────────────────────────────────────

  const selectSt = {
    padding: '6px 10px', fontSize: 13, fontFamily: 'inherit',
    border: `1px solid ${C.border}`, borderRadius: 4,
    background: C.bgSurface, color: C.textPrimary, cursor: 'pointer',
  };

  const th = {
    padding: '9px 14px', textAlign: 'left', fontSize: 10, fontFamily: MONO, fontWeight: 600,
    textTransform: 'uppercase', letterSpacing: '0.06em', color: C.textSecondary,
    background: C.bgStatsrow, borderBottom: `1px solid ${C.border}`, whiteSpace: 'nowrap',
  };

  const td = (last) => ({
    padding: '10px 14px', fontSize: 13,
    borderBottom: last ? 'none' : `1px solid ${C.borderLight}`,
    color: C.textPrimary, verticalAlign: 'middle',
  });

  const tdMono = (last) => ({ ...td(last), fontFamily: MONO, fontSize: 12, color: C.textSecondary });

  const filterInputSt = (name) => ({
    width: '100%', fontSize: 11,
    border: `1px solid ${focusedFilter === name ? '#0F6E56' : '#E2DED8'}`,
    borderRadius: 3, padding: '4px 6px', background: 'white', outline: 'none',
  });

  // ── Render ─────────────────────────────────────────────────────────────

  return (
    <div style={{ height: '100%', display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
      <TopBar reviewerName={reviewerName} />

      {/* Sub-bar: filters + right controls */}
      <div style={{
        flexShrink: 0, display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        padding: '8px 20px', background: C.bgSurface, borderBottom: `1px solid ${C.border}`,
        gap: 12, flexWrap: 'wrap',
      }}>

        {/* Left group: search + filter selects + confidence + clear */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
          {/* Feature 6 — Search */}
          <input
            type="text"
            placeholder="Search session ID..."
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            onFocus={() => setSearchFocused(true)}
            onBlur={() => setSearchFocused(false)}
            style={{
              width: 180, padding: '6px 12px', fontSize: 12,
              border: `1px solid ${searchFocused ? C.accent : C.border}`,
              borderRadius: 4, background: searchFocused ? C.bgSurface : C.bgMuted,
              color: C.textPrimary, transition: 'border-color 150ms, background 150ms',
            }}
          />

          <span style={{ fontSize: 11, fontFamily: MONO, textTransform: 'uppercase', color: C.textSecondary }}>
            Filter:
          </span>

          <select style={selectSt} value={verdictFilter} onChange={(e) => setVerdictFilter(e.target.value)}>
            <option value="">All Verdicts</option>
            <option value="SEVERE">SEVERE</option>
            <option value="FLAGGED">FLAGGED</option>
            <option value="CLEAN">CLEAN</option>
            <option value="UNPROCESSED">UNPROCESSED</option>
          </select>

          <select style={selectSt} value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
            <option value="">All Statuses</option>
            <option value="PENDING">PENDING</option>
            <option value="SUBMITTED_FOR_REVIEW">SUBMITTED FOR REVIEW</option>
            <option value="NEEDS_FINAL_REVIEW">NEEDS FINAL REVIEW</option>
            <option value="REVIEWED">REVIEWED</option>
            <option value="CONFIRMED">CONFIRMED</option>
            <option value="OVERRIDDEN">OVERRIDDEN</option>
            <option value="LOCKED">LOCKED</option>
          </select>

          {/* L2 — Assignee filter */}
          {reviewerRole === 'L2' && (
            <select style={selectSt} value={assigneeFilter} onChange={(e) => setAssigneeFilter(e.target.value)}>
              <option value="">All Reviewers</option>
              <option value="Nikhil">Nikhil</option>
              <option value="Yusuf">Yusuf</option>
              <option value="Vineet">Vineet</option>
              <option value="Divyansh">Divyansh</option>
            </select>
          )}

          {/* Feature 8 — Confidence slider */}
          <span style={{ fontSize: 11, fontFamily: MONO, color: C.textSecondary, whiteSpace: 'nowrap' }}>
            Min confidence:
          </span>
          <input
            type="range" min={0} max={100} step={5} value={minConfidence}
            onChange={(e) => setMinConfidence(Number(e.target.value))}
            style={{ width: 100, accentColor: C.accent, cursor: 'pointer' }}
          />
          <span style={{ fontSize: 11, fontFamily: MONO, color: C.accent, minWidth: 28 }}>
            {minConfidence}%
          </span>

          {hasFilters && (
            <button className="btn-link-accent" onClick={clearFilters}
              style={{ fontSize: 12, color: C.accent, background: 'none', border: 'none',
                cursor: 'pointer', marginLeft: 4, padding: 0 }}>
              Clear filters
            </button>
          )}
        </div>

        {/* Right group: Team Progress toggle + Export + progress pill */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexShrink: 0 }}>
          <button
            onClick={handleToggleProgress}
            style={{
              fontSize: 12, padding: '5px 12px', background: C.bgSurface,
              border: `1px solid ${C.border}`, borderRadius: 4,
              color: showProgress ? C.accent : C.textPrimary, cursor: 'pointer',
            }}
          >
            Team Progress {showProgress ? '▴' : '▾'}
          </button>

          <button
            onClick={handleExport}
            style={{
              fontSize: 12, padding: '5px 14px', background: C.bgSurface,
              border: `1px solid ${C.border}`, borderRadius: 4,
              color: C.textPrimary, cursor: 'pointer',
            }}
          >
            {exporting ? 'Exporting…' : '↓ Export CSV'}
          </button>

          <div style={{
            fontSize: 11, fontFamily: MONO, background: C.bgStatsrow,
            border: `1px solid ${C.border}`, borderRadius: 4, padding: '4px 12px',
            color: C.textSecondary, whiteSpace: 'nowrap',
          }}>
            <span style={{ color: C.accent, fontWeight: 500 }}>{reviewed}</span>
            {' of '}
            <span style={{ color: C.textPrimary }}>{total}</span>
            {' reviewed'}
          </div>
        </div>
      </div>

      {/* Stats strip */}
      <div style={{ flexShrink: 0, display: 'flex', alignItems: 'stretch', background: C.bgStatsrow, borderBottom: `1px solid ${C.border}` }}>
        {statCells.map((cell) => (
          <div key={cell.label} style={{ flex: 1, padding: '10px 20px',
            borderRight: `1px solid ${C.border}` }}>
            <div style={{ fontSize: 18, fontFamily: MONO, fontWeight: 500, color: cell.color }}>
              {cell.value}
            </div>
            <div style={{ fontSize: 11, color: C.textSecondary, marginTop: 2 }}>{cell.label}</div>
          </div>
        ))}
        <div style={{ display: 'flex', alignItems: 'center', padding: '0 20px', flexShrink: 0 }}>
          <span
            onClick={handleToggleHeatmap}
            style={{ fontSize: 11, fontFamily: MONO, color: '#0F6E56', cursor: 'pointer', whiteSpace: 'nowrap' }}
          >
            Violation Breakdown {showHeatmap ? '▴' : '▾'}
          </span>
        </div>
      </div>

      {/* L1 assignment banner */}
      {reviewerRole === 'L1' && reviewerName && (
        <div style={{
          flexShrink: 0, padding: '7px 20px', background: '#EFF6FF',
          borderBottom: '1px solid #B5D4F4', display: 'flex', alignItems: 'center', gap: 8,
        }}>
          <span style={{ fontSize: 12, color: '#0C447C' }}>
            Showing sessions assigned to <strong>{reviewerName}</strong>
          </span>
        </div>
      )}

      {/* L2 reviewer progress — assignment-based breakdown per reviewer (collapsible) */}
      {reviewerRole === 'L2' && stats?.reviewer_stats?.length > 0 && (
        <div style={{ flexShrink: 0, padding: '10px 20px', background: C.bgSurface, borderBottom: `1px solid ${C.border}` }}>
          <button
            onClick={() => setShowReviewerProgress((v) => !v)}
            style={{
              display: 'flex', alignItems: 'center', gap: 6, background: 'none',
              border: 'none', cursor: 'pointer', padding: 0,
              marginBottom: showReviewerProgress ? 8 : 0,
              fontSize: 10, fontFamily: MONO, fontWeight: 600, textTransform: 'uppercase',
              letterSpacing: '0.06em', color: C.textSecondary,
            }}
          >
            <span>{showReviewerProgress ? '▾' : '▸'}</span>
            Reviewer Progress
          </button>
          {showReviewerProgress && (
          <div style={{ border: `1px solid ${C.border}`, borderRadius: 6, overflow: 'hidden' }}>
            <table style={{ width: '100%', borderCollapse: 'collapse', background: C.bgSurface }}>
              <thead>
                <tr style={{ background: C.bgStatsrow }}>
                  {['Reviewer', 'Assigned', 'Pending', 'Submitted', 'Locked', 'Progress'].map((h) => (
                    <th key={h} style={{
                      padding: '6px 12px', textAlign: 'left', fontSize: 10, fontFamily: MONO,
                      fontWeight: 600, textTransform: 'uppercase', letterSpacing: '0.06em',
                      color: C.textSecondary, borderBottom: `1px solid ${C.border}`, whiteSpace: 'nowrap',
                    }}>
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {stats.reviewer_stats.map((r, i) => {
                  const isLast = i === stats.reviewer_stats.length - 1;
                  const border = isLast ? 'none' : `1px solid ${C.borderLight}`;
                  const pct = r.total > 0 ? Math.round(((r.submitted + r.locked) / r.total) * 100) : 0;
                  const cellSt = { padding: '6px 12px', fontSize: 12, fontFamily: MONO, borderBottom: border };
                  return (
                    <tr key={r.reviewer} style={{ background: C.bgSurface }}>
                      <td style={{ ...cellSt, color: C.textPrimary, fontWeight: 500 }}>{r.reviewer || '—'}</td>
                      <td style={{ ...cellSt, color: C.textPrimary }}>{r.total}</td>
                      <td style={{ ...cellSt, color: r.pending > 0 ? C.accent : C.textSecondary }}>{r.pending}</td>
                      <td style={{ ...cellSt, color: '#185FA5' }}>{r.submitted}</td>
                      <td style={{ ...cellSt, color: '#444441' }}>{r.locked}</td>
                      <td style={{ ...cellSt, minWidth: 140 }}>
                        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                          <div style={{ flex: 1, height: 6, background: '#E2DED8', borderRadius: 3 }}>
                            <div style={{
                              height: '100%', borderRadius: 3,
                              background: pct === 100 ? C.accent : '#185FA5',
                              width: `${pct}%`, transition: 'width 300ms',
                            }} />
                          </div>
                          <span style={{ fontSize: 11, color: C.textSecondary, minWidth: 30, textAlign: 'right' }}>
                            {pct}%
                          </span>
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          )}
        </div>
      )}

      {/* Violation heatmap — collapsible */}
      {showHeatmap && (
        <div style={{
          flexShrink: 0, margin: '0 20px 12px', background: '#FFFFFF',
          border: '1px solid #E2DED8', borderRadius: 6, padding: '16px 20px',
        }}>
          {loadingHeatmap ? (
            <div style={{ display: 'flex', alignItems: 'center', gap: 10,
              color: C.textSecondary, fontSize: 13 }}>
              <LoadingSpinner size={16} /> Loading…
            </div>
          ) : heatmapData.length === 0 ? (
            <div style={{ fontSize: 13, color: '#6B6860', fontStyle: 'italic', textAlign: 'center' }}>
              No violation data yet.
            </div>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
              {heatmapData.map((item) => (
                <div key={item.category_code} style={{ display: 'flex', alignItems: 'center', height: 32 }}>
                  <span style={{
                    fontSize: 13, fontFamily: MONO, color: '#1C1C1A',
                    width: 260, flexShrink: 0,
                    overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                  }}>
                    {item.category_code}
                  </span>
                  <div style={{
                    flex: 1, height: 8, background: '#E2DED8',
                    borderRadius: 4, margin: '0 12px', position: 'relative',
                  }}>
                    <div style={{
                      height: '100%', borderRadius: 4,
                      width: `${(item.count / heatmapMax) * 100}%`,
                      background: CATEGORY_COLORS[item.category_code] ?? '#6B6860',
                    }} />
                  </div>
                  <span style={{
                    fontSize: 13, fontFamily: MONO, color: '#6B6860',
                    minWidth: 32, textAlign: 'right',
                  }}>
                    {item.count}
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* Feature 7 — Team Progress collapsible section */}
      {showProgress && (
        <div style={{ flexShrink: 0, padding: '12px 20px', background: C.bgSurface,
          borderBottom: `1px solid ${C.border}` }}>
          {loadingProgress ? (
            <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '8px 0',
              color: C.textSecondary, fontSize: 13 }}>
              <LoadingSpinner size={16} /> Loading reviewer stats…
            </div>
          ) : reviewerStats.length === 0 ? (
            <div style={{ fontSize: 13, color: C.textSecondary, padding: '6px 0' }}>
              No review activity yet.
            </div>
          ) : (
            <div style={{ border: `1px solid ${C.border}`, borderRadius: 6, overflow: 'hidden' }}>
              <table style={{ width: '100%', borderCollapse: 'collapse', background: C.bgSurface }}>
                <thead>
                  <tr style={{ background: C.bgStatsrow, borderBottom: `1px solid ${C.border}` }}>
                    {PROGRESS_COLS.map((h) => (
                      <th key={h} style={{
                        padding: '7px 14px', textAlign: 'left', fontSize: 10,
                        fontFamily: MONO, fontWeight: 600, textTransform: 'uppercase',
                        letterSpacing: '0.06em', color: C.textSecondary, whiteSpace: 'nowrap',
                      }}>
                        {h}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {reviewerStats.map((r, i) => {
                    const isLast = i === reviewerStats.length - 1;
                    const border = isLast ? 'none' : `1px solid ${C.borderLight}`;
                    const cellSt = { padding: '7px 14px', fontSize: 12, fontFamily: MONO,
                      color: C.textPrimary, borderBottom: border };
                    return (
                      <tr key={r.reviewer_id} style={{ background: C.bgSurface }}>
                        <td style={{ ...cellSt, color: C.textPrimary }}>{r.reviewer_id}</td>
                        <td style={{ ...cellSt, color: C.accent, fontWeight: 500 }}>{r.sessions_reviewed}</td>
                        <td style={cellSt}>{r.confirmed}</td>
                        <td style={cellSt}>{r.false_positives}</td>
                        <td style={{ ...cellSt, color: r.escalated > 0 ? C.severeText : C.textPrimary }}>
                          {r.escalated}
                        </td>
                        <td style={cellSt}>{r.cleared}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      {/* Table area */}
      <div style={{ flex: 1, overflowY: 'auto', overflowX: 'auto', padding: '16px 20px' }}>
        {loading ? (
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center',
            padding: '60px 0', gap: 12, color: C.textSecondary, fontSize: 14 }}>
            <LoadingSpinner /> Loading sessions…
          </div>
        ) : (
          <div style={{ border: `1px solid ${C.border}`, borderRadius: 6, overflow: 'hidden' }}>
            <table style={{ width: '100%', borderCollapse: 'collapse', background: C.bgSurface }}>
              <thead>
                {/* Header row — sortable columns show ↑/↓ indicator */}
                <tr style={{ background: C.bgStatsrow, borderBottom: `1px solid ${C.border}` }}>
                  {COLS.map((h) => {
                    const key      = SORT_KEYS[h];
                    const isActive = key && sortCol === key;
                    return (
                      <th key={h} onClick={() => key && handleSortClick(h)}
                        style={{ ...th, cursor: key ? 'pointer' : 'default', userSelect: 'none' }}>
                        {h}
                        {isActive && (
                          <span style={{ marginLeft: 4, fontSize: 10, color: '#0F6E56' }}>
                            {sortDir === 'asc' ? '↑' : '↓'}
                          </span>
                        )}
                      </th>
                    );
                  })}
                </tr>
                {/* Column filter row */}
                <tr style={{ background: '#FAFAF8', borderBottom: '1px solid #E2DED8' }}>
                  <th style={{ padding: '4px 8px', fontWeight: 'normal' }}>
                    <input type="text" placeholder="Filter ID..."
                      value={colFilterId} onChange={(e) => setColFilterId(e.target.value)}
                      onFocus={() => setFocusedFilter('id')} onBlur={() => setFocusedFilter(null)}
                      style={filterInputSt('id')} />
                  </th>
                  {reviewerRole === 'L2' && <th style={{ padding: '4px 8px' }} />/* Assigned To */}
                  <th style={{ padding: '4px 8px' }} />{/* Verdict */}
                  <th style={{ padding: '4px 8px', fontWeight: 'normal' }}>{/* AstroTalk */}
                    <select value={colFilterAstro} onChange={(e) => setColFilterAstro(e.target.value)}
                      style={filterInputSt('astro')}>
                      <option value="">All</option>
                      <option value="flagged">Flagged</option>
                      <option value="clean">Clean</option>
                    </select>
                  </th>
                  <th style={{ padding: '4px 8px' }} />{/* Flags */}
                  <th style={{ padding: '4px 8px' }} />{/* LLM Flags */}
                  <th style={{ padding: '4px 8px' }} />{/* Manual Flags */}
                  <th style={{ padding: '4px 8px', fontWeight: 'normal' }}>{/* Language */}
                    <input type="text" placeholder="Filter lang..."
                      value={colFilterLang} onChange={(e) => setColFilterLang(e.target.value)}
                      onFocus={() => setFocusedFilter('lang')} onBlur={() => setFocusedFilter(null)}
                      style={filterInputSt('lang')} />
                  </th>
                  <th style={{ padding: '4px 8px', fontWeight: 'normal' }}>
                    <select value={colFilterType} onChange={(e) => setColFilterType(e.target.value)}
                      style={filterInputSt('type')}>
                      <option value="">All</option>
                      <option value="chat">Chat</option>
                      <option value="voice">Voice</option>
                    </select>
                  </th>
                  <th style={{ padding: '4px 8px', fontWeight: 'normal' }}>
                    <div style={{ display: 'flex', gap: 3 }}>
                      <input type="number" placeholder="Min"
                        value={colFilterMinDur} onChange={(e) => setColFilterMinDur(e.target.value)}
                        onFocus={() => setFocusedFilter('durMin')} onBlur={() => setFocusedFilter(null)}
                        style={{ ...filterInputSt('durMin'), width: '50%' }} />
                      <input type="number" placeholder="Max"
                        value={colFilterMaxDur} onChange={(e) => setColFilterMaxDur(e.target.value)}
                        onFocus={() => setFocusedFilter('durMax')} onBlur={() => setFocusedFilter(null)}
                        style={{ ...filterInputSt('durMax'), width: '50%' }} />
                    </div>
                  </th>
                  <th style={{ padding: '4px 8px', fontWeight: 'normal' }}>
                    <div style={{ display: 'flex', gap: 3 }}>
                      <input type="number" placeholder="Min"
                        value={colFilterMinTurns} onChange={(e) => setColFilterMinTurns(e.target.value)}
                        onFocus={() => setFocusedFilter('turnsMin')} onBlur={() => setFocusedFilter(null)}
                        style={{ ...filterInputSt('turnsMin'), width: '50%' }} />
                      <input type="number" placeholder="Max"
                        value={colFilterMaxTurns} onChange={(e) => setColFilterMaxTurns(e.target.value)}
                        onFocus={() => setFocusedFilter('turnsMax')} onBlur={() => setFocusedFilter(null)}
                        style={{ ...filterInputSt('turnsMax'), width: '50%' }} />
                    </div>
                  </th>
                  <th style={{ padding: '4px 8px' }} />
                  <th style={{ padding: '4px 8px' }} />
                  <th style={{ padding: '4px 8px' }} />
                </tr>
              </thead>
              <tbody>
                {displayedSessions.length > 0 ? displayedSessions.map((s, idx) => {
                  const isLast     = idx === displayedSessions.length - 1;
                  const isHovered  = hoveredRow === s.session_id;
                  const isClearing = clearingSession === s.session_id;
                  const isNFR      = s.review_status === 'NEEDS_FINAL_REVIEW';
                  return (
                    <tr
                      key={s.session_id}
                      className="session-row"
                      onMouseEnter={() => setHoveredRow(s.session_id)}
                      onMouseLeave={() => setHoveredRow(null)}
                      style={{
                        background: isHovered ? C.bgMuted : C.bgSurface,
                        transition: 'background 150ms',
                        ...(reviewerRole === 'L2' && isNFR ? { borderLeft: '3px solid #FAC775' } : {}),
                      }}
                    >
                      <td style={tdMono(isLast)}>
                        {s.review_status === 'LOCKED' && <span style={{ marginRight: 4 }}>🔒</span>}
                        {s.session_id}
                      </td>

                      {reviewerRole === 'L2' && (
                        <td style={tdMono(isLast)}>
                          {s.assigned_to || <span style={{ color: C.textMuted }}>—</span>}
                        </td>
                      )}

                      <td style={td(isLast)}><VerdictBadge verdict={s.overall_verdict} /></td>

                      {/* AstroTalk's own flag from the input CSV */}
                      <td style={td(isLast)}>
                        {s.astrotalk_flagged === 1 ? (
                          <span style={{
                            fontSize: 10, fontFamily: MONO, padding: '2px 7px', borderRadius: 3,
                            background: C.severeBg, color: C.severeText, border: `1px solid ${C.severeBorder}`,
                          }}>Flagged</span>
                        ) : (
                          <span style={{
                            fontSize: 10, fontFamily: MONO, padding: '2px 7px', borderRadius: 3,
                            background: C.bgStatsrow, color: C.textSecondary, border: `1px solid ${C.border}`,
                          }}>Clean</span>
                        )}
                      </td>

                      {/* Total flags (excludes DISMISSED) */}
                      <td style={td(isLast)}>
                        {(s.flag_count ?? 0) > 0 ? (
                          <span style={{ display: 'flex', alignItems: 'center', gap: 5 }}>
                            <span style={{
                              width: 6, height: 6, borderRadius: '50%', flexShrink: 0,
                              background: s.overall_verdict === 'SEVERE' ? C.severeText
                                : s.overall_verdict === 'FLAGGED' ? C.flaggedText : C.cleanText,
                            }} />
                            <span style={{ fontFamily: MONO, fontSize: 12 }}>{s.flag_count}</span>
                          </span>
                        ) : <span style={{ color: C.textMuted }}>—</span>}
                      </td>

                      {/* LLM + REGEX flags */}
                      <td style={td(isLast)}>
                        {(s.llm_flag_count ?? 0) > 0 ? (
                          <span style={{ display: 'flex', alignItems: 'center', gap: 5 }}>
                            <span style={{
                              width: 6, height: 6, borderRadius: '50%', flexShrink: 0,
                              background: s.overall_verdict === 'SEVERE' ? C.severeText
                                : s.overall_verdict === 'FLAGGED' ? C.flaggedText : C.cleanText,
                            }} />
                            <span style={{ fontFamily: MONO, fontSize: 12 }}>{s.llm_flag_count}</span>
                          </span>
                        ) : <span style={{ color: C.textMuted }}>—</span>}
                      </td>

                      {/* Manual flags */}
                      <td style={td(isLast)}>
                        {(s.manual_flag_count ?? 0) > 0 ? (
                          <span style={{ display: 'flex', alignItems: 'center', gap: 5 }}>
                            <span style={{
                              width: 6, height: 6, borderRadius: '50%', flexShrink: 0,
                              background: '#7F77DD',
                            }} />
                            <span style={{ fontFamily: MONO, fontSize: 12 }}>{s.manual_flag_count}</span>
                          </span>
                        ) : <span style={{ color: C.textMuted }}>—</span>}
                      </td>

                      <td style={td(isLast)}>
                        <span style={{ textTransform: 'capitalize', fontSize: 13 }}>
                          {s.language_detected || '—'}
                        </span>
                      </td>

                      <td style={td(isLast)}>
                        <SessionTypePill stype={s.session_type} />
                      </td>

                      <td style={tdMono(isLast)}>
                        <span style={{ display: 'flex', alignItems: 'center', flexWrap: 'nowrap' }}>
                          {formatDuration(s.duration_minutes)}
                          {s.duration_minutes > 1440 && (
                            <span style={{
                              marginLeft: 6,
                              fontSize: 10,
                              fontFamily: 'DM Mono',
                              background: '#FAEEDA',
                              color: '#633806',
                              border: '1px solid #FAC775',
                              borderRadius: 3,
                              padding: '1px 6px',
                            }}>
                              multi-day
                            </span>
                          )}
                        </span>
                      </td>

                      <td style={tdMono(isLast)}>
                        {(s.turn_count ?? 0) > 0
                          ? <span style={{ fontSize: 12, color: '#6B6860' }}>{s.turn_count}</span>
                          : <span style={{ color: C.textMuted }}>—</span>}
                      </td>

                      <td style={td(isLast)}><StatusBadge status={s.review_status} /></td>

                      <td style={td(isLast)}>
                        {['SUBMITTED_FOR_REVIEW', 'NEEDS_FINAL_REVIEW', 'LOCKED'].includes(s.review_status) && s.submitted_by ? (
                          <div>
                            <span style={{ fontSize: 12, color: '#1C1C1A' }}>{truncate(s.submitted_by, 15)}</span>
                            {s.review_status === 'LOCKED' && s.locked_by && (
                              <div style={{ fontSize: 10, fontFamily: MONO, color: '#9B9890', marginTop: 2 }}>
                                Locked by {s.locked_by}
                              </div>
                            )}
                          </div>
                        ) : s.reviewer_id && s.review_status !== 'PENDING' ? (
                          <span style={{ fontSize: 12, color: '#1C1C1A' }}>{truncate(s.reviewer_id, 15)}</span>
                        ) : (
                          <span style={{ color: '#9B9890' }}>—</span>
                        )}
                      </td>

                      <td style={td(isLast)}>
                        <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'stretch', gap: 5 }}>
                          {reviewerRole === 'L2' && ['SUBMITTED_FOR_REVIEW', 'NEEDS_FINAL_REVIEW', 'REVIEWED', 'CONFIRMED', 'OVERRIDDEN'].includes(s.review_status) && (
                            <button
                              onClick={() => handleLockSession(s.session_id)}
                              style={{
                                padding: '5px 10px', fontSize: 11, background: '#FFFFFF',
                                border: `1px solid ${C.accent}`, borderRadius: 4,
                                color: C.accent, cursor: 'pointer', whiteSpace: 'nowrap',
                              }}
                            >
                              Lock 🔒
                            </button>
                          )}
                          <button
                            className="review-btn"
                            onClick={() => onSelectSession(s.session_id, sessions)}
                            style={{
                              padding: '5px 14px', fontSize: 12, fontWeight: 500,
                              background: s.review_status === 'LOCKED' ? C.bgStatsrow : C.accent,
                              color: s.review_status === 'LOCKED' ? C.textSecondary : '#FFFFFF',
                              border: s.review_status === 'LOCKED' ? `1px solid ${C.border}` : 'none',
                              borderRadius: 4, transition: 'background 150ms', whiteSpace: 'nowrap',
                            }}
                          >
                            {s.review_status === 'LOCKED' ? 'View 🔒' : 'Review →'}
                          </button>
                        </div>
                      </td>
                    </tr>
                  );
                }) : (
                  <tr>
                    <td colSpan={COLS.length} style={{ textAlign: 'center', padding: '40px 0' }}>
                      <div style={{ fontSize: 13, color: '#6B6860' }}>
                        No sessions match the selected filters.
                      </div>
                      <span
                        onClick={() => {
                          setSearchQuery('');
                          setMinConfidence(0);
                          setVerdictFilter('');
                          setStatusFilter('');
                          setAssigneeFilter('');
                          setColFilterId('');
                          setColFilterAstro('');
                          setColFilterLang('');
                          setColFilterType('');
                          setColFilterMinDur('');
                          setColFilterMaxDur('');
                          setColFilterMinTurns('');
                          setColFilterMaxTurns('');
                        }}
                        style={{ fontSize: 12, color: '#0F6E56', cursor: 'pointer',
                          display: 'inline-block', marginTop: 10 }}
                      >
                        Clear filters
                      </span>
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <Footer />
    </div>
  );
}
