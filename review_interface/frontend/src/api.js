const API_BASE = window.location.origin;

async function request(path, options = {}) {
  const res = await fetch(`${API_BASE}${path}`, options);
  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText);
    throw new Error(`HTTP ${res.status}: ${text}`);
  }
  return res.json();
}

// ── Aggregate stats ────────────────────────────────────────────────────────

export function getStats() {
  return request('/stats');
}

export function getReviewerStats() {
  return request('/stats/reviewer');
}

export async function getViolationStats() {
  const res = await fetch(`${API_BASE}/stats/violations`);
  if (!res.ok) throw new Error('Failed to fetch violation stats');
  return res.json();
}

// ── Session list ───────────────────────────────────────────────────────────

export function getSessions(filters = {}) {
  const params = new URLSearchParams();
  if (filters.verdict)           params.append('verdict',           filters.verdict);
  if (filters.status)            params.append('status',            filters.status);
  if (filters.language)          params.append('language',          filters.language);
  if (filters.reviewer)          params.append('reviewer',          filters.reviewer);
  if (filters.assigned_reviewer) params.append('assigned_reviewer', filters.assigned_reviewer);
  const qs = params.toString() ? `?${params}` : '';
  return request(`/sessions${qs}`);
}

export function getPendingSessions(limit = 50) {
  return request(`/sessions/pending?limit=${limit}`);
}

// ── Session detail and sub-resources ──────────────────────────────────────

export function getSessionDetail(sessionId) {
  return request(`/sessions/${encodeURIComponent(sessionId)}`);
}

export function getSessionFlags(sessionId) {
  return request(`/sessions/${encodeURIComponent(sessionId)}/flags`);
}

export function submitReview(sessionId, action, reviewerId, note = '', flagId = null) {
  return request(`/sessions/${encodeURIComponent(sessionId)}/review`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      action,
      reviewer_id: reviewerId,
      note,
      flag_id: flagId,
    }),
  });
}

export function manualFlag(sessionId, { turn_id, category_code, note, reviewer_id, message_text }) {
  return request(`/sessions/${encodeURIComponent(sessionId)}/manual-flag`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ turn_id, category_code, note, reviewer_id, message_text }),
  });
}

export function saveSessionNote(sessionId, note, reviewerId) {
  return request(`/sessions/${encodeURIComponent(sessionId)}/session-note`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ note, reviewer_id: reviewerId }),
  });
}

// ── Workflow actions ───────────────────────────────────────────────────────

export function confirmFlag(flagId, reviewerId) {
  return request(`/flags/${flagId}/confirm`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ reviewer_id: reviewerId }),
  });
}

export function submitSession(sessionId, reviewerId, note) {
  return request(`/sessions/${encodeURIComponent(sessionId)}/submit`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ reviewer_id: reviewerId, note: note || null }),
  });
}

export function markNeedsFinalReview(sessionId, reviewerId) {
  return request(`/sessions/${encodeURIComponent(sessionId)}/needs-final-review`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ reviewer_id: reviewerId }),
  });
}

export function finalApproveSession(sessionId, reviewerId) {
  return request(`/sessions/${encodeURIComponent(sessionId)}/final-approve`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ reviewer_id: reviewerId }),
  });
}

export function finalApproveSessions(sessionIds, reviewerId) {
  return request('/sessions/final-approve-bulk', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ reviewer_id: reviewerId, session_ids: sessionIds }),
  });
}

// ── Export ─────────────────────────────────────────────────────────────────

export function exportCsv() {
  window.open(`${API_BASE}/export/csv`, '_blank');
}
