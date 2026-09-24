/* ==========================================================================
   WEMIX — API client
   Talks to the Django backend. Change API_BASE_URL if the backend isn't
   running on localhost:8000.
   ========================================================================== */

const API_BASE_URL = window.HABARI_API_BASE_URL || 'http://127.0.0.1:8000';

const TOKEN_KEY = 'habari_access_token';
const REFRESH_KEY = 'habari_refresh_token';
const USER_KEY = 'habari_user';

const Auth = {
  getAccessToken() { return localStorage.getItem(TOKEN_KEY); },
  getRefreshToken() { return localStorage.getItem(REFRESH_KEY); },
  getUser() {
    const raw = localStorage.getItem(USER_KEY);
    return raw ? JSON.parse(raw) : null;
  },
  isLoggedIn() { return !!this.getAccessToken(); },
  setSession({ access, refresh, user }) {
    if (access) localStorage.setItem(TOKEN_KEY, access);
    if (refresh) localStorage.setItem(REFRESH_KEY, refresh);
    if (user) localStorage.setItem(USER_KEY, JSON.stringify(user));
  },
  clear() {
    localStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(REFRESH_KEY);
    localStorage.removeItem(USER_KEY);
  },
  logout() {
    this.clear();
    window.location.href = 'index.html';
  },
};

/**
 * Core fetch wrapper. Attaches the Bearer token automatically, retries
 * once with a refreshed access token on a 401, and throws an Error with a
 * `.data` property containing the parsed error body on failure so callers
 * can render field-level messages.
 */
async function apiFetch(path, { method = 'GET', body, isForm = false, auth = true, _retried = false } = {}) {
  const headers = {};
  if (!isForm) headers['Content-Type'] = 'application/json';

  if (auth && Auth.getAccessToken()) {
    headers['Authorization'] = `Bearer ${Auth.getAccessToken()}`;
  }

  const resp = await fetch(`${API_BASE_URL}${path}`, {
    method,
    headers,
    body: body === undefined ? undefined : (isForm ? body : JSON.stringify(body)),
  });

  if (resp.status === 401 && auth && !_retried && Auth.getRefreshToken()) {
    const refreshed = await tryRefreshToken();
    if (refreshed) {
      return apiFetch(path, { method, body, isForm, auth, _retried: true });
    }
    Auth.clear();
  }

  let data = null;
  const text = await resp.text();
  if (text) {
    try { data = JSON.parse(text); } catch (e) { data = { detail: text }; }
  }

  if (!resp.ok) {
    const err = new Error((data && (data.detail || JSON.stringify(data))) || `Request failed (${resp.status})`);
    err.status = resp.status;
    err.data = data;
    throw err;
  }

  return data;
}

async function tryRefreshToken() {
  try {
    const resp = await fetch(`${API_BASE_URL}/api/accounts/login/refresh/`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refresh: Auth.getRefreshToken() }),
    });
    if (!resp.ok) return false;
    const data = await resp.json();
    Auth.setSession({ access: data.access });
    return true;
  } catch (e) {
    return false;
  }
}

/** Renders a field-error object ({field: [msg, ...]} or {detail: msg} or
 * a plain array of strings) into a short human-readable string. */
function formatApiError(err) {
  const data = err && err.data;
  if (!data) return (err && err.message) || 'Something went wrong.';
  if (typeof data === 'string') return data;
  if (Array.isArray(data)) return data.join(' ');
  if (data.detail) return typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail);
  const parts = [];
  for (const [field, msgs] of Object.entries(data)) {
    const msg = Array.isArray(msgs) ? msgs.join(' ') : msgs;
    parts.push(field === 'non_field_errors' ? msg : `${field}: ${msg}`);
  }
  return parts.join(' — ') || 'Something went wrong.';
}

function money(amount, currency = 'TZS') {
  const n = Number(amount);
  return `${n.toLocaleString('en-US', { minimumFractionDigits: 0, maximumFractionDigits: 0 })} ${currency}`;
}

function timeAgo(isoString) {
  const diff = (Date.now() - new Date(isoString).getTime()) / 1000;
  if (diff < 60) return 'just now';
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}

function escapeHtml(str) {
  const div = document.createElement('div');
  div.textContent = str == null ? '' : String(str);
  return div.innerHTML;
}
