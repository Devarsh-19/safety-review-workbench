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

export function getStats(params = {}) {
  const qs = new URLSearchParams();
  if (params.reviewer_name) qs.append('reviewer_name', params.reviewer_name);
  if (params.reviewer_role) qs.append('reviewer_role', params.reviewer_role);
  const q = qs.toString() ? `?${qs}` : '';
  return request(`/stats${q}`);
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
  if (filters.verdict)       params.append('verdict',       filters.verdict);
  if (filters.status)        params.append('status',        filters.status);
  if (filters.language)      params.append('language',      filters.language);
  if (filters.reviewer_name) params.append('reviewer_name', filters.reviewer_name);
  if (filters.reviewer_role) params.append('reviewer_role', filters.reviewer_role);
  if (filters.assigned_to)   params.append('assigned_to',   filters.assigned_to);
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

// ── Flag operations ────────────────────────────────────────────────────────

export function confirmFlag(flagId, reviewerId) {
  return request(`/flags/${flagId}/confirm`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ reviewer_id: reviewerId }),
  });
}

export function confirmAllFlags(sessionId, reviewerId) {
  return request(`/sessions/${encodeURIComponent(sessionId)}/confirm-all-flags`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ reviewer_id: reviewerId }),
  });
}

// ── Workflow actions ───────────────────────────────────────────────────────

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

// ── Export ─────────────────────────────────────────────────────────────────
// L1 reviewers get only their own submitted sessions; L2 gets all.
export function exportCsv(reviewerName, reviewerRole) {
  const qs = new URLSearchParams();
  if (reviewerName) qs.append('reviewer_name', reviewerName);
  if (reviewerRole) qs.append('reviewer_role', reviewerRole);
  const q = qs.toString() ? `?${qs}` : '';
  window.open(`${API_BASE}/export/csv${q}`, '_blank');
}
