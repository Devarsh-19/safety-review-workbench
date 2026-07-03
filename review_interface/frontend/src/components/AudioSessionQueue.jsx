import React, { useState, useEffect, useCallback } from 'react';
import { C, MONO } from '../tokens';
import TopBar from './TopBar';
import Footer from './Footer';
import StatusBadge from './StatusBadge';
import VerdictBadge from './VerdictBadge';
import LoadingSpinner from './LoadingSpinner';
import { getAudioSessions, getAudioStats } from '../api';

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
  { value: '',                     label: 'All statuses' },
  { value: 'PENDING',              label: 'Pending' },
  { value: 'SUBMITTED_FOR_REVIEW', label: 'Submitted' },
  { value: 'LOCKED',               label: 'Locked' },
  { value: 'REVIEWED',             label: 'Reviewed (unlocked)' },
];

export default function AudioSessionQueue({ reviewerName, reviewerRole, onSelectSession }) {
  const [rows,    setRows]    = useState([]);
  const [total,   setTotal]   = useState(0);
  const [stats,   setStats]   = useState(null);
  const [status,  setStatus]  = useState('');
  const [search,  setSearch]  = useState('');
  const [page,    setPage]    = useState(0);
  const [loading, setLoading] = useState(true);
  const [error,   setError]   = useState('');

  const load = useCallback(() => {
    setLoading(true);
    setError('');
    Promise.all([
      getAudioSessions({ status, search, limit: PAGE_SIZE, offset: page * PAGE_SIZE }),
      getAudioStats(),
    ])
      .then(([list, st]) => {
        setRows(list.rows || []);
        setTotal(list.total || 0);
        setStats(st);
      })
      .catch((e) => setError(String(e.message || e)))
      .finally(() => setLoading(false));
  }, [status, search, page]);

  useEffect(() => { load(); }, [load]);

  const statCells = [
    { label: 'Total',     value: stats?.total_sessions ?? 0 },
    { label: 'Pending',   value: stats?.total_pending  ?? 0 },
    { label: 'Submitted', value: stats?.count_submitted ?? 0 },
    { label: 'Locked',    value: stats?.count_locked   ?? 0 },
    { label: 'Severe',    value: stats?.count_severe   ?? 0 },
    { label: 'Flagged',   value: stats?.count_flagged  ?? 0 },
  ];

  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));

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
        </div>

        {/* Filters */}
        <div style={{ display: 'flex', gap: 8, marginBottom: 12 }}>
          <select
            value={status}
            onChange={(e) => { setStatus(e.target.value); setPage(0); }}
            style={{
              padding: '8px 12px', fontSize: 13, borderRadius: 5,
              border: `1px solid ${C.border}`, background: C.bgSurface,
              color: C.textPrimary, cursor: 'pointer',
            }}
          >
            {STATUS_FILTERS.map((f) => (
              <option key={f.value} value={f.value}>{f.label}</option>
            ))}
          </select>
          <input
            value={search}
            onChange={(e) => { setSearch(e.target.value); setPage(0); }}
            placeholder="Search session id…"
            style={{
              flex: 1, maxWidth: 260, padding: '8px 12px', fontSize: 13,
              borderRadius: 5, border: `1px solid ${C.border}`,
              background: C.bgSurface, color: C.textPrimary,
            }}
          />
        </div>

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
                {['Session', 'Language', 'Duration', 'Segments', 'Flags', 'Speaker Roles', 'Verdict', 'Status'].map((h) => (
                  <th key={h} style={{
                    textAlign: 'left', padding: '10px 14px',
                    fontSize: 11, fontFamily: MONO, textTransform: 'uppercase',
                    letterSpacing: '0.05em', color: C.textSecondary,
                    borderBottom: `1px solid ${C.border}`,
                  }}>
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {loading ? (
                <tr><td colSpan={8} style={{ padding: 32, textAlign: 'center' }}><LoadingSpinner /></td></tr>
              ) : rows.length === 0 ? (
                <tr>
                  <td colSpan={8} style={{ padding: 32, textAlign: 'center', color: C.textSecondary }}>
                    No audio sessions. Ingest results with scripts/ingest_audio_results.py.
                  </td>
                </tr>
              ) : rows.map((r) => (
                <tr
                  key={r.s_id}
                  onClick={() => onSelectSession(r.s_id)}
                  style={{ cursor: 'pointer', borderBottom: `1px solid ${C.borderLight}` }}
                  onMouseEnter={(e) => { e.currentTarget.style.background = C.bgMuted; }}
                  onMouseLeave={(e) => { e.currentTarget.style.background = 'transparent'; }}
                >
                  <td style={{ padding: '10px 14px', fontFamily: MONO }}>{r.s_id}</td>
                  <td style={{ padding: '10px 14px' }}>{r.lang || '—'}</td>
                  <td style={{ padding: '10px 14px', fontFamily: MONO }}>{formatDuration(r.duration_seconds)}</td>
                  <td style={{ padding: '10px 14px', fontFamily: MONO }}>{r.segment_count}</td>
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
                  <td style={{ padding: '10px 14px' }}><VerdictBadge verdict={r.overall_verdict} /></td>
                  <td style={{ padding: '10px 14px' }}><StatusBadge status={r.review_status} /></td>
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
