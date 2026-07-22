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

// Returns { rows: [...page...], total: <full filtered count> }.
// Filtering, sorting and pagination all happen server-side.
export function getSessions(filters = {}) {
  const params = new URLSearchParams();
  const add = (k, v) => {
    if (v !== undefined && v !== null && v !== '') params.append(k, v);
  };
  add('verdict',        filters.verdict);
  add('status',         filters.status);
  add('language',       filters.language);
  add('reviewer_name',  filters.reviewer_name);
  add('reviewer_role',  filters.reviewer_role);
  add('assigned_to',    filters.assigned_to);
  add('flag_category',  filters.flag_category);
  add('search',         filters.search);
  add('session_type',   filters.session_type);
  add('astrotalk',      filters.astrotalk);
  if (filters.min_confidence) add('min_confidence', filters.min_confidence);
  add('min_duration', filters.min_duration);
  add('max_duration', filters.max_duration);
  add('min_turns', filters.min_turns);
  add('max_turns', filters.max_turns);
  add('sort_col', filters.sort_col);
  add('sort_dir', filters.sort_dir);
  if (filters.limit != null) add('limit', filters.limit);
  if (filters.offset != null) add('offset', filters.offset);
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

export function dismissAllFlags(sessionId, reviewerId) {
  return request(`/sessions/${encodeURIComponent(sessionId)}/dismiss-all-flags`, {
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

// L2 bulk action: lock every chat session currently SUBMITTED_FOR_REVIEW.
export function lockAllSubmittedSessions(reviewerId) {
  return request('/sessions/lock-all-submitted', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ reviewer_id: reviewerId }),
  });
}

// ── Audio review ───────────────────────────────────────────────────────────

export function getAudioStats(params = {}) {
  const qs = new URLSearchParams();
  if (params.reviewer_name) qs.append('reviewer_name', params.reviewer_name);
  if (params.reviewer_role) qs.append('reviewer_role', params.reviewer_role);
  const q = qs.toString() ? `?${qs}` : '';
  return request(`/audio/stats${q}`);
}

export function getAudioViolationStats() {
  return request('/audio/stats/violations');
}

export function getAudioLanguages() {
  return request('/audio/languages');
}

export function getAudioSessions(filters = {}) {
  const params = new URLSearchParams();
  const add = (k, v) => {
    if (v !== undefined && v !== null && v !== '') params.append(k, v);
  };
  add('status', filters.status);
  add('search', filters.search);
  add('reviewer_name', filters.reviewer_name);
  add('reviewer_role', filters.reviewer_role);
  add('assigned_to', filters.assigned_to);
  add('has_video', filters.has_video);
  add('lang', filters.lang);
  add('duration_min', filters.duration_min);
  add('duration_max', filters.duration_max);
  add('flags_min', filters.flags_min);
  add('flags_max', filters.flags_max);
  add('pauses_min', filters.pauses_min);
  add('pauses_max', filters.pauses_max);
  add('roles', filters.roles);
  add('reviewer', filters.reviewer);
  add('verdict', filters.verdict);
  add('astrotalk_verdict', filters.astrotalk_verdict);
  add('flag_category', filters.flag_category);
  add('sort_col', filters.sort_col);
  add('sort_dir', filters.sort_dir);
  if (filters.limit != null) add('limit', filters.limit);
  if (filters.offset != null) add('offset', filters.offset);
  const qs = params.toString() ? `?${params}` : '';
  return request(`/audio/sessions${qs}`);
}

export function getAudioSessionDetail(sId) {
  return request(`/audio/sessions/${sId}`);
}

export function saveSpeakerRoles(sId, speaker1Role, speaker2Role, reviewerId) {
  return request(`/audio/sessions/${sId}/speaker-roles`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      speaker1_role: speaker1Role,
      speaker2_role: speaker2Role,
      reviewer_id: reviewerId,
    }),
  });
}

export function confirmAudioFlag(flagId, reviewerId) {
  return request(`/audio/flags/${flagId}/confirm`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ reviewer_id: reviewerId }),
  });
}

export function amendAudioFlag(flagId, { intent, severity, reasoning, reviewer_id }) {
  return request(`/audio/flags/${flagId}/amend`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ intent, severity, reasoning: reasoning || '', reviewer_id }),
  });
}

export function dismissAudioFlag(flagId, reviewerId, note = '') {
  return request(`/audio/flags/${flagId}/dismiss`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ reviewer_id: reviewerId, note }),
  });
}

export function saveAudioSessionRisk(sId, risk, reviewerId) {
  return request(`/audio/sessions/${sId}/session-risk`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ risk, reviewer_id: reviewerId }),
  });
}

export function confirmAllAudioFlags(sId, reviewerId) {
  return request(`/audio/sessions/${sId}/confirm-all-flags`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ reviewer_id: reviewerId }),
  });
}

export function dismissAllAudioFlags(sId, reviewerId) {
  return request(`/audio/sessions/${sId}/dismiss-all-flags`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ reviewer_id: reviewerId }),
  });
}

export function saveAudioSessionNote(sId, note, reviewerId) {
  return request(`/audio/sessions/${sId}/session-note`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ note, reviewer_id: reviewerId }),
  });
}

export function submitAudioSession(sId, reviewerId, note) {
  return request(`/audio/sessions/${sId}/submit`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ reviewer_id: reviewerId, note: note || null }),
  });
}

export function lockAudioSession(sId, reviewerId) {
  return request(`/audio/sessions/${sId}/lock`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ reviewer_id: reviewerId }),
  });
}

export function unlockAudioSession(sId, reviewerId) {
  return request(`/audio/sessions/${sId}/unlock`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ reviewer_id: reviewerId }),
  });
}

// L2 bulk action: lock every audio session currently SUBMITTED_FOR_REVIEW.
export function lockAllSubmittedAudioSessions(reviewerId) {
  return request('/audio/sessions/lock-all-submitted', {
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
