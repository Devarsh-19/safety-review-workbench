import React, { useState, useEffect, useCallback, useRef } from 'react';
import { C, MONO } from '../tokens';
import TopBar from './TopBar';
import Footer from './Footer';
import StatusBadge from './StatusBadge';
import VerdictBadge from './VerdictBadge';
import LoadingSpinner from './LoadingSpinner';
import HasVideoBadge from './HasVideoBadge';
import { getAudioSessions, getAudioStats, lockAudioSession, getAudioViolationStats } from '../api';

const PAGE_SIZE = 50;

function formatDuration(seconds) {
  const s = Math.round(Number(seconds) || 0);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  const pad = (n) => String(n).padStart(2, '0');
  return `${pad(h)}:${pad(m)}:${pad(sec)}`;
}

const STATUS_FILTERS = [
  { value: '', label: 'All statuses' },
  { value: 'PENDING', label: 'Pending' },
  { value: 'SUBMITTED_FOR_REVIEW', label: 'Submitted' },
  { value: 'LOCKED', label: 'Locked' },
  { value: 'REVIEWED', label: 'Reviewed (unlocked)' },
];

// Bar colours for the violation heatmap — same palette as the chat queue
// (SessionQueue.jsx CATEGORY_COLORS) so both dashboards read identically.
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
  EXTERNAL_MEDIA_CONTENT:      '#185FA5',
  OTHER:                       '#6B6860',
};

const EMPTY_FILTERS = {
  search: '',
  hasVideo: '',
  lang: '',
  durationMin: '',   // minutes
  durationMax: '',   // minutes
  flagsMin: '',
  flagsMax: '',
  roles: '',
  assignedTo: '',
  reviewer: '',
  verdict: '',
  astrotalkVerdict: '',
  status: '',
};

// Minutes string from an input box -> seconds for the API, '' when blank.
function minutesToSeconds(value) {
  if (value === '' || value == null || Number.isNaN(Number(value))) return '';
  return String(Number(value) * 60);
}

export default function AudioSessionQueue({ reviewerName, reviewerRole, onSelectSession }) {
  const [rows, setRows] = useState([]);
  const [total, setTotal] = useState(0);
  const [stats, setStats] = useState(null);
  const [filterInputs, setFilterInputs] = useState(EMPTY_FILTERS); // raw input values
  const [filters, setFilters] = useState(EMPTY_FILTERS);           // debounced values used for queries
  const [page, setPage] = useState(0);
  const [sortCol, setSortCol] = useState('s_id');
  const [sortDir, setSortDir] = useState('asc');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  // Monotonic id per request — a response only lands if it is still the
  // latest one, so slow/out-of-order responses can't overwrite fresh rows.
  const requestIdRef = useRef(0);

  // L2 reviewer progress — collapsible, expanded by default (chat parity)
  const [showReviewerProgress, setShowReviewerProgress] = useState(true);

  // Violation heatmap — collapsible, fetched on first open (chat parity)
  const [showHeatmap, setShowHeatmap] = useState(false);
  const [heatmapData, setHeatmapData] = useState([]);
  const [loadingHeatmap, setLoadingHeatmap] = useState(false);

  const handleToggleHeatmap = () => {
    const next = !showHeatmap;
    setShowHeatmap(next);
    if (next) {
      setLoadingHeatmap(true);
      getAudioViolationStats()
        .then((data) => { setHeatmapData(data); setLoadingHeatmap(false); })
        .catch(() => setLoadingHeatmap(false));
    }
  };

  const heatmapMax = heatmapData.length > 0 ? Math.max(...heatmapData.map((d) => d.count)) : 1;

  // Debounce every filter control: query 300ms after the user stops typing.
  useEffect(() => {
    const t = setTimeout(() => {
      setFilters(filterInputs);
      setPage(0);
    }, 300);
    return () => clearTimeout(t);
  }, [filterInputs]);

  const setFilter = (key, value) =>
    setFilterInputs((prev) => ({ ...prev, [key]: value }));

  const hasActiveFilters = Object.values(filterInputs).some((v) => v !== '');
  const clearFilters = () => setFilterInputs(EMPTY_FILTERS);

  const load = useCallback(() => {
    const reqId = ++requestIdRef.current;
    setLoading(true);
    setError('');
    Promise.all([
      getAudioSessions({
        status: filters.status,
        search: filters.search,
        has_video: filters.hasVideo,
        lang: filters.lang,
        duration_min: minutesToSeconds(filters.durationMin),
        duration_max: minutesToSeconds(filters.durationMax),
        flags_min: filters.flagsMin,
        flags_max: filters.flagsMax,
        roles: filters.roles,
        assigned_to: filters.assignedTo,
        reviewer: filters.reviewer,
        verdict: filters.verdict,
        astrotalk_verdict: filters.astrotalkVerdict,
        reviewer_name: reviewerName, reviewer_role: reviewerRole,
        sort_col: sortCol, sort_dir: sortDir,
        limit: PAGE_SIZE, offset: page * PAGE_SIZE,
      }),
      getAudioStats({ reviewer_name: reviewerName, reviewer_role: reviewerRole }),
    ])
      .then(([list, st]) => {
        if (reqId !== requestIdRef.current) return;  // stale response
        setRows(list.rows || []);
        setTotal(list.total || 0);
        setStats(st);
      })
      .catch((e) => {
        if (reqId !== requestIdRef.current) return;
        setError(String(e.message || e));
      })
      .finally(() => {
        if (reqId === requestIdRef.current) setLoading(false);
      });
  }, [filters, page, sortCol, sortDir, reviewerName, reviewerRole]);

  useEffect(() => { load(); }, [load]);

  const handleLock = (sId) => {
    // eslint-disable-next-line no-alert
    if (!window.confirm('Lock this session? This will freeze all flags and the review decision.')) return;
    lockAudioSession(sId, reviewerName)
      .then(load)
      .catch((err) => setError(String(err.message || err)));
  };

  const statCells = [
    { label: 'Total', value: stats?.total_sessions ?? 0 },
    { label: 'Pending', value: stats?.total_pending ?? 0 },
    { label: 'Submitted', value: stats?.count_submitted ?? 0 },
    { label: 'Locked', value: stats?.count_locked ?? 0 },
    { label: 'Severe', value: stats?.count_severe ?? 0 },
    { label: 'Flagged', value: stats?.count_flagged ?? 0 },
  ];

  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  const handleSort = (col) => {
    if (sortCol === col) {
      setSortDir(sortDir === 'asc' ? 'desc' : 'asc');
    } else {
      setSortCol(col);
      setSortDir('asc');
    }
  };

  const TABLE_COLUMNS = [
    { key: 's_id', label: 'Session', sortable: true },
    { key: 'has_video', label: 'Video', sortable: false },
    { key: 'lang', label: 'Language', sortable: false },
    { key: 'duration', label: 'Duration', sortable: true },
    { key: 'flags', label: 'Flags', sortable: true },
    { key: 'roles', label: 'Speaker Roles', sortable: false },
    ...(reviewerRole === 'L2' ? [
      { key: 'assigned_to', label: 'Assigned To', sortable: false },
      { key: 'reviewer', label: 'Reviewer', sortable: false },
    ] : []),
    { key: 'verdict', label: 'LLM Verdict', sortable: true },
    { key: 'astrotalk_verdict', label: 'Astrotalk', sortable: false },
    { key: 'status', label: 'Status', sortable: true },
    { key: 'action', label: 'Action', sortable: false },
  ];

  const filterInputStyle = {
    width: '100%', boxSizing: 'border-box', padding: '4px 6px',
    fontSize: 11, borderRadius: 4, border: `1px solid ${C.border}`,
    background: C.bgSurface, color: C.textPrimary,
  };
  const filterSelectStyle = { ...filterInputStyle, cursor: 'pointer' };
  const filterCellStyle = {
    padding: '6px 10px', background: C.bgMuted,
    borderBottom: `1px solid ${C.border}`,
  };

  // One filter control per column, keyed by column key.
  const FILTER_CONTROLS = {
    s_id: (
      <input
        value={filterInputs.search}
        onChange={(e) => setFilter('search', e.target.value)}
        placeholder="Search id…"
        style={filterInputStyle}
      />
    ),
    has_video: (
      <select
        value={filterInputs.hasVideo}
        onChange={(e) => setFilter('hasVideo', e.target.value)}
        style={filterSelectStyle}
      >
        <option value="">All</option>
        <option value="1">Yes</option>
        <option value="0">No</option>
      </select>
    ),
    lang: (
      <input
        value={filterInputs.lang}
        onChange={(e) => setFilter('lang', e.target.value)}
        placeholder="e.g. HINDI"
        style={filterInputStyle}
      />
    ),
    duration: (
      <div style={{ display: 'flex', gap: 4 }}>
        <input
          type="number" min="0"
          value={filterInputs.durationMin}
          onChange={(e) => setFilter('durationMin', e.target.value)}
          placeholder="min (m)"
          style={filterInputStyle}
        />
        <input
          type="number" min="0"
          value={filterInputs.durationMax}
          onChange={(e) => setFilter('durationMax', e.target.value)}
          placeholder="max (m)"
          style={filterInputStyle}
        />
      </div>
    ),
    flags: (
      <div style={{ display: 'flex', gap: 4 }}>
        <input
          type="number" min="0"
          value={filterInputs.flagsMin}
          onChange={(e) => setFilter('flagsMin', e.target.value)}
          placeholder="min"
          style={filterInputStyle}
        />
        <input
          type="number" min="0"
          value={filterInputs.flagsMax}
          onChange={(e) => setFilter('flagsMax', e.target.value)}
          placeholder="max"
          style={filterInputStyle}
        />
      </div>
    ),
    roles: (
      <select
        value={filterInputs.roles}
        onChange={(e) => setFilter('roles', e.target.value)}
        style={filterSelectStyle}
      >
        <option value="">All</option>
        <option value="assigned">Assigned</option>
        <option value="unassigned">Not assigned</option>
      </select>
    ),
    assigned_to: (
      <input
        value={filterInputs.assignedTo}
        onChange={(e) => setFilter('assignedTo', e.target.value)}
        placeholder="Name…"
        style={filterInputStyle}
      />
    ),
    reviewer: (
      <input
        value={filterInputs.reviewer}
        onChange={(e) => setFilter('reviewer', e.target.value)}
        placeholder="Name…"
        style={filterInputStyle}
      />
    ),
    verdict: (
      <select
        value={filterInputs.verdict}
        onChange={(e) => setFilter('verdict', e.target.value)}
        style={filterSelectStyle}
      >
        <option value="">All</option>
        <option value="SEVERE">Severe</option>
        <option value="FLAGGED">Flagged</option>
        <option value="CLEAN">Clean</option>
      </select>
    ),
    astrotalk_verdict: (
      <select
        value={filterInputs.astrotalkVerdict}
        onChange={(e) => setFilter('astrotalkVerdict', e.target.value)}
        style={filterSelectStyle}
      >
        <option value="">All</option>
        <option value="FLAGGED">Flagged</option>
        <option value="CLEAN">Clean</option>
      </select>
    ),
    status: (
      <select
        value={filterInputs.status}
        onChange={(e) => setFilter('status', e.target.value)}
        style={filterSelectStyle}
      >
        {STATUS_FILTERS.map((f) => (
          <option key={f.value} value={f.value}>{f.label}</option>
        ))}
      </select>
    ),
    action: null,
  };

  return (
    <div style={{ height: '100%', display: 'flex', flexDirection: 'column' }}>
      <TopBar reviewerName={`${reviewerName} · Audio Review`} />

      <div style={{ flex: 1, overflow: 'auto', padding: 24, background: C.bgPage }}>
        {/* Stats strip */}
        <div style={{
          display: 'flex',
          border: `1px solid ${C.border}`,
          borderRadius: 5,
          overflow: 'hidden',
          marginBottom: 16,
          background: C.bgSurface,
        }}>
          {statCells.map((cell, i) => (
            <div key={cell.label} style={{
              flex: 1,
              padding: '12px 8px',
              textAlign: 'center',
              borderRight: i < statCells.length - 1 ? `1px solid ${C.border}` : 'none',
            }}>
              <div style={{ fontSize: 20, fontFamily: MONO, fontWeight: 500, color: C.textPrimary }}>
                {cell.value}
              </div>
              <div style={{ fontSize: 11, color: C.textSecondary, marginTop: 2 }}>
                {cell.label}
              </div>
            </div>
          ))}
          <div style={{
            display: 'flex', alignItems: 'center', padding: '0 20px',
            flexShrink: 0, borderLeft: `1px solid ${C.border}`,
          }}>
            <span
              onClick={handleToggleHeatmap}
              style={{ fontSize: 11, fontFamily: MONO, color: C.accent, cursor: 'pointer', whiteSpace: 'nowrap' }}
            >
              Violation Breakdown {showHeatmap ? '▴' : '▾'}
            </span>
          </div>
        </div>

        {/* L2 reviewer progress — assignment-based breakdown per reviewer,
            collapsible; identical visuals to the chat queue (SessionQueue.jsx) */}
        {reviewerRole === 'L2' && stats?.reviewer_stats?.length > 0 && (
          <div style={{ marginBottom: 16 }}>
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

        {/* Violation heatmap — collapsible (chat parity: SessionQueue.jsx) */}
        {showHeatmap && (
          <div style={{
            background: C.bgSurface, border: `1px solid ${C.border}`,
            borderRadius: 6, padding: '16px 20px', marginBottom: 16,
          }}>
            {loadingHeatmap ? (
              <div style={{
                display: 'flex', alignItems: 'center', gap: 10,
                color: C.textSecondary, fontSize: 13,
              }}>
                <LoadingSpinner size={16} /> Loading…
              </div>
            ) : heatmapData.length === 0 ? (
              <div style={{ fontSize: 13, color: C.textSecondary, fontStyle: 'italic', textAlign: 'center' }}>
                No violation data yet.
              </div>
            ) : (
              <div style={{
                display: 'flex', flexDirection: 'column', gap: 4,
                maxHeight: '45vh', overflowY: 'auto',
              }}>
                {heatmapData.map((item) => (
                  <div key={item.category_code} style={{ display: 'flex', alignItems: 'center', height: 32 }}>
                    <span style={{
                      fontSize: 13, fontFamily: MONO, color: C.textPrimary,
                      width: 260, flexShrink: 0,
                      overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                    }}>
                      {item.category_code}
                    </span>
                    <div style={{
                      flex: 1, height: 8, background: C.border,
                      borderRadius: 4, margin: '0 12px', position: 'relative',
                    }}>
                      <div style={{
                        height: '100%', borderRadius: 4,
                        width: `${(item.count / heatmapMax) * 100}%`,
                        background: CATEGORY_COLORS[item.category_code] ?? C.textSecondary,
                      }} />
                    </div>
                    <span style={{
                      fontSize: 13, fontFamily: MONO, color: C.textSecondary,
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

        {/* Filters live in the table header (one control per column). This bar
            only hosts the reset action so active filters are easy to back out of. */}
        {hasActiveFilters && (
          <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 8 }}>
            <button
              onClick={clearFilters}
              style={{
                padding: '6px 12px', fontSize: 12, borderRadius: 4,
                border: `1px solid ${C.border}`, background: C.bgSurface,
                color: C.textPrimary, cursor: 'pointer',
              }}
            >
              ✕ Clear all filters
            </button>
          </div>
        )}

        {error && (
          <div style={{
            padding: 12, marginBottom: 12, borderRadius: 5,
            background: C.severeBg, border: `1px solid ${C.severeBorder}`,
            color: C.severeText, fontSize: 13,
          }}>
            {error}
          </div>
        )}

        {/* Table */}
        <div style={{
          background: C.bgSurface,
          border: `1px solid ${C.border}`,
          borderRadius: 6,
          overflow: 'hidden',
        }}>
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
            <thead>
              <tr style={{ background: C.bgMuted }}>
                {TABLE_COLUMNS.map((col) => (
                  <th
                    key={col.key}
                    onClick={() => col.sortable && handleSort(col.key)}
                    style={{
                      textAlign: 'left', padding: '10px 14px',
                      fontSize: 11, fontFamily: MONO, textTransform: 'uppercase',
                      letterSpacing: '0.05em', color: C.textSecondary,
                      borderBottom: `1px solid ${C.border}`,
                      cursor: col.sortable ? 'pointer' : 'default',
                      userSelect: 'none',
                    }}
                  >
                    <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                      {col.label}
                      {col.sortable && (
                        <span style={{
                          opacity: sortCol === col.key ? 1 : 0.3,
                          fontSize: 10,
                        }}>
                          {sortCol === col.key ? (sortDir === 'asc' ? '↑' : '↓') : '↕'}
                        </span>
                      )}
                    </div>
                  </th>
                ))}
              </tr>
              {/* Per-column filter row */}
              <tr style={{ background: C.bgMuted }}>
                {TABLE_COLUMNS.map((col) => (
                  <td key={`filter-${col.key}`} style={filterCellStyle}>
                    {FILTER_CONTROLS[col.key] || null}
                  </td>
                ))}
              </tr>
            </thead>
            <tbody>
              {loading ? (
                <tr><td colSpan={TABLE_COLUMNS.length} style={{ padding: 32, textAlign: 'center' }}><LoadingSpinner /></td></tr>
              ) : rows.length === 0 ? (
                <tr>
                  <td colSpan={TABLE_COLUMNS.length} style={{ padding: 32, textAlign: 'center', color: C.textSecondary }}>
                    No audio sessions. Ingest results with scripts/ingest_audio_results.py
                    {reviewerRole === 'L1' ? ' — or none are assigned to you yet (scripts/assign_audio_sessions.py).' : '.'}
                  </td>
                </tr>
              ) : rows.map((r) => (
                <tr
                  key={r.s_id}
                  style={{ borderBottom: `1px solid ${C.borderLight}` }}
                  onMouseEnter={(e) => { e.currentTarget.style.background = C.bgMuted; }}
                  onMouseLeave={(e) => { e.currentTarget.style.background = 'transparent'; }}
                >
                  <td style={{ padding: '10px 14px', fontFamily: MONO }}>{r.s_id}</td>
                  <td style={{ padding: '10px 14px' }}><HasVideoBadge value={r.has_video} compact /></td>
                  <td style={{ padding: '10px 14px' }}>{r.lang || '—'}</td>
                  <td style={{ padding: '10px 14px', fontFamily: MONO }}>{formatDuration(r.duration_seconds)}</td>
                  <td style={{
                    padding: '10px 14px', fontFamily: MONO,
                    color: r.flag_count > 0 ? C.severeText : C.textSecondary,
                    fontWeight: r.flag_count > 0 ? 600 : 400,
                  }}>
                    {r.flag_count}
                  </td>
                  <td style={{ padding: '10px 14px', fontSize: 12, color: C.textSecondary }}>
                    {r.speaker1_role && r.speaker2_role
                      ? `S1: ${r.speaker1_role} · S2: ${r.speaker2_role}`
                      : 'Not assigned'}
                  </td>
                  {reviewerRole === 'L2' && (
                    <td style={{ padding: '10px 14px', fontSize: 12, color: C.textSecondary }}>
                      {r.assigned_to || '—'}
                    </td>
                  )}
                  {reviewerRole === 'L2' && (
                    <td style={{ padding: '10px 14px', fontSize: 12, color: C.textSecondary }}>
                      {r.submitted_by || r.reviewer_id || '—'}
                    </td>
                  )}
                  <td style={{ padding: '10px 14px' }}><VerdictBadge verdict={r.overall_verdict} /></td>
                  <td style={{ padding: '10px 14px' }}><VerdictBadge verdict={r.astrotalk_verdict} /></td>
                  <td style={{ padding: '10px 14px' }}><StatusBadge status={r.review_status} /></td>
                  <td style={{ padding: '10px 14px' }}>
                    <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'stretch', gap: 5 }}>
                      {reviewerRole === 'L2' && ['SUBMITTED_FOR_REVIEW', 'REVIEWED'].includes(r.review_status) && (
                        <button
                          onClick={() => handleLock(r.s_id)}
                          style={{
                            padding: '5px 10px', fontSize: 11, background: C.bgSurface,
                            border: `1px solid ${C.accent}`, borderRadius: 4,
                            color: C.accent, cursor: 'pointer', whiteSpace: 'nowrap',
                          }}
                        >
                          Lock 🔒
                        </button>
                      )}
                      <button
                        onClick={() => onSelectSession(r.s_id, rows)}
                        style={{
                          padding: '5px 14px', fontSize: 12, fontWeight: 500,
                          background: r.review_status === 'LOCKED' ? C.bgStatsrow : C.accent,
                          color: r.review_status === 'LOCKED' ? C.textSecondary : '#FFFFFF',
                          border: r.review_status === 'LOCKED' ? `1px solid ${C.border}` : 'none',
                          borderRadius: 4, transition: 'background 150ms', whiteSpace: 'nowrap',
                          cursor: 'pointer',
                        }}
                      >
                        {r.review_status === 'LOCKED' ? 'View 🔒' : 'Review →'}
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        {/* Pagination */}
        <div style={{
          display: 'flex', alignItems: 'center', justifyContent: 'space-between',
          marginTop: 12, fontSize: 12, color: C.textSecondary,
        }}>
          <span>{total} session{total === 1 ? '' : 's'}</span>
          <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
            <button
              disabled={page === 0}
              onClick={() => setPage((p) => Math.max(0, p - 1))}
              style={{
                padding: '6px 12px', borderRadius: 4, fontSize: 12,
                border: `1px solid ${C.border}`, background: C.bgSurface,
                color: page === 0 ? C.textMuted : C.textPrimary,
                cursor: page === 0 ? 'not-allowed' : 'pointer',
              }}
            >
              ← Prev
            </button>
            <span style={{ fontFamily: MONO }}>{page + 1} / {totalPages}</span>
            <button
              disabled={page + 1 >= totalPages}
              onClick={() => setPage((p) => p + 1)}
              style={{
                padding: '6px 12px', borderRadius: 4, fontSize: 12,
                border: `1px solid ${C.border}`, background: C.bgSurface,
                color: page + 1 >= totalPages ? C.textMuted : C.textPrimary,
                cursor: page + 1 >= totalPages ? 'not-allowed' : 'pointer',
              }}
            >
              Next →
            </button>
          </div>
        </div>
      </div>

      <Footer />
    </div>
  );
}
