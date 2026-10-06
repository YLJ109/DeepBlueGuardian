/* 深蓝智护 · 前端公共层：接口客户端、实时通道、格式化与图标。 */
(function () {
'use strict';


const API_BASE = '/api/v1';

const TOKEN_KEY = 'deepblue.token';
const USER_KEY = 'deepblue.user';

const store = {
  get token() { return localStorage.getItem(TOKEN_KEY) || ''; },
  set token(v) { v ? localStorage.setItem(TOKEN_KEY, v) : localStorage.removeItem(TOKEN_KEY); },
  get user() {
    try { return JSON.parse(localStorage.getItem(USER_KEY) || 'null'); } catch (e) { return null; }
  },
  set user(v) { v ? localStorage.setItem(USER_KEY, JSON.stringify(v)) : localStorage.removeItem(USER_KEY); },
  clear() { this.token = ''; this.user = null; },
};

class ApiError extends Error {
  constructor(message, status) { super(message); this.name = 'ApiError'; this.status = status; }
}

async function request(path, { method = 'GET', body, query, raw = false } = {}) {
  const url = new URL(API_BASE + path, window.location.origin);
  if (query) {
    Object.entries(query).forEach(([k, v]) => {
      if (v !== undefined && v !== null && v !== '') url.searchParams.set(k, v);
    });
  }
  const headers = {};
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  if (store.token) headers.Authorization = 'Bearer ' + store.token;

  const resp = await fetch(url.toString(), {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });

  if (raw) return resp;
  let payload = null;
  const text = await resp.text();
  if (text) { try { payload = JSON.parse(text); } catch (e) { payload = text; } }

  if (!resp.ok) {
    const detail = payload && payload.detail ? (typeof payload.detail === 'string' ? payload.detail : JSON.stringify(payload.detail)) : `请求失败（${resp.status}）`;
    if (resp.status === 401 && !path.startsWith('/auth/login')) store.clear();
    throw new ApiError(detail, resp.status);
  }
  return payload;
}

const api = {
  login: (username, password) => request('/auth/login', { method: 'POST', body: { username, password } }),
  me: () => request('/auth/me'),
  changePassword: (old_password, new_password) => request('/auth/password', { method: 'POST', body: { old_password, new_password } }),
  users: () => request('/auth/users'),
  createUser: (body) => request('/auth/users', { method: 'POST', body }),
  updateUser: (id, body) => request(`/auth/users/${id}`, { method: 'PATCH', body }),
  audit: (limit = 100) => request('/auth/audit', { query: { limit } }),

  devices: (query) => request('/devices', { query }),
  deviceStats: () => request('/devices/stats'),
  regions: () => request('/devices/regions'),
  device: (code) => request(`/devices/${encodeURIComponent(code)}`),
  createDevice: (body) => request('/devices', { method: 'POST', body }),
  updateDevice: (code, body) => request(`/devices/${encodeURIComponent(code)}`, { method: 'PATCH', body }),
  deleteDevice: (code) => request(`/devices/${encodeURIComponent(code)}`, { method: 'DELETE' }),
  deviceCommand: (code, command, payload) => request(`/devices/${encodeURIComponent(code)}/command`, { method: 'POST', body: { command, payload: payload || {} } }),

  metrics: () => request('/data/metrics'),
  latest: (region_code) => request('/data/latest', { query: { region_code } }),
  history: (query) => request('/data/history', { query }),
  aggregate: (query) => request('/data/aggregate', { query }),
  compare: (query) => request('/data/compare', { query }),
  volume: () => request('/data/volume'),
  exportUrl: (query) => {
    const url = new URL(API_BASE + '/data/export', window.location.origin);
    Object.entries(query).forEach(([k, v]) => { if (v) url.searchParams.set(k, v); });
    return url.toString();
  },

  alerts: (query) => request('/alerts', { query }),
  alertStats: (hours = 24) => request('/alerts/stats', { query: { hours } }),
  handleAlert: (id, body) => request(`/alerts/${id}/handle`, { method: 'POST', body }),
  alertScan: () => request('/alerts/scan', { method: 'POST' }),

  predictOverview: (hours = 48) => request('/predict/overview', { query: { hours } }),
  predictDevice: (code, horizon_hours = 48) => request(`/predict/device/${encodeURIComponent(code)}`, { query: { horizon_hours } }),
  predictHistory: (code, hours = 168) => request(`/predict/history/${encodeURIComponent(code)}`, { query: { hours } }),
  predictRun: () => request('/predict/run', { method: 'POST' }),
  modelInfo: () => request('/predict/model'),

  mangroveRegions: () => request('/mangrove/regions'),
  mangroveStats: () => request('/mangrove/stats'),
  mangroveAnalysis: (region_code, bloom = 0.35) => request('/mangrove/analysis', { query: { region_code, bloom } }),
  mangroveTrend: () => request('/mangrove/health-trend'),

  modelConfigs: () => request('/models'),
  modelRules: () => request('/models/rules'),
  modelMeta: () => request('/models/meta'),
  updateModelConfig: (id, body) => request(`/models/${id}`, { method: 'PATCH', body }),
  reloadModel: (version = 'v1') => request('/models/reload', { method: 'POST', query: { version } }),
  events: (query) => request('/models/events', { query }),

  overview: () => request('/dashboard/overview'),
  agentStatus: () => request('/agent/status'),
  agentExamples: () => request('/agent/examples'),
  agentChat: (question, history) => request('/agent/chat', { method: 'POST', body: { question, history: history || [] } }),
  agentStreamPath: () => API_BASE + '/agent/chat/stream',
  visionStatus: () => request('/vision/status'),
  visionCameras: () => request('/vision/cameras'),
  visionSamples: () => request('/vision/samples'),
  visionDetect: (body) => request('/vision/detect', { method: 'POST', body }),
  visionScan: (body) => request('/vision/scan', { method: 'POST', body }),
  visionAlert: (body) => request('/vision/alert', { method: 'POST', body }),
  visionModels: () => request('/vision/models'),
  visionSwitchModel: (name) => request('/vision/models/switch', { method: 'POST', body: { name } }),
  dashboardHealth: () => request('/dashboard/health'),
  dashboardEvents: (limit = 30) => request('/dashboard/events', { query: { limit } }),
  patrolTasks: (query) => request('/patrol/tasks', { query }),
  patrolTask: (code) => request(`/patrol/tasks/${encodeURIComponent(code)}`),
  patrolCreate: (body) => request('/patrol/tasks', { method: 'POST', body }),
  patrolClaim: (code, body) => request(`/patrol/tasks/${encodeURIComponent(code)}/claim`, { method: 'POST', body: body || {} }),
  patrolSubmit: (code, body) => request(`/patrol/tasks/${encodeURIComponent(code)}/submit`, { method: 'POST', body }),
  patrolSummary: () => request('/patrol/summary'),
  patrolReport: (hours = 24) => request('/patrol/report', { query: { hours } }),
};

/* ------------------------------------------------------------------ */
/* 实时通道                                                            */
/* ------------------------------------------------------------------ */
function createRealtime({ onEvent, onStatus } = {}) {
  const client = {
    socket: null,
    retries: 0,
    closed: false,
    status: 'connecting',
    connect() {
      if (this.closed) return;
      const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
      const socket = new WebSocket(`${proto}//${window.location.host}/ws`);
      this.socket = socket;
      this._set('connecting');
      socket.onopen = () => { this.retries = 0; this._set('online'); };
      socket.onmessage = (evt) => {
        let payload = null;
        try { payload = JSON.parse(evt.data); } catch (e) { return; }
        if (payload.type === 'heartbeat' || payload.type === 'pong') { this._set('online'); return; }
        if (payload.type === 'welcome') this._set('online');
        if (onEvent) onEvent(payload);
      };
      socket.onclose = () => {
        this._set('offline');
        if (this.closed) return;
        this.retries += 1;
        const delay = Math.min(15000, 1200 * this.retries);
        setTimeout(() => this.connect(), delay);
      };
      socket.onerror = () => { this._set('offline'); };
    },
    _set(status) {
      if (this.status === status) return;
      this.status = status;
      if (onStatus) onStatus(status);
    },
    close() { this.closed = true; if (this.socket) this.socket.close(); },
  };
  client.connect();
  return client;
}

/* ------------------------------------------------------------------ */
/* 格式化                                                              */
/* ------------------------------------------------------------------ */
const METRIC_ORDER = ['water_temp', 'ph', 'dissolved_oxygen', 'turbidity', 'chlorophyll_a', 'salinity'];
const METRIC_META = {
  water_temp: { label: '水温', unit: '℃', decimals: 2, color: '#ff9f43' },
  ph: { label: 'pH', unit: '', decimals: 2, color: '#a78bfa' },
  dissolved_oxygen: { label: '溶解氧', unit: 'mg/L', decimals: 2, color: '#33e1ff' },
  turbidity: { label: '浊度', unit: 'NTU', decimals: 2, color: '#ffd166' },
  chlorophyll_a: { label: '叶绿素a', unit: 'μg/L', decimals: 2, color: '#29e7a0' },
  salinity: { label: '盐度', unit: 'PSU', decimals: 2, color: '#4c8dff' },
};

const fmt = {
  num(value, digits = 2) {
    if (value === null || value === undefined || Number.isNaN(value)) return '--';
    return Number(value).toFixed(digits);
  },
  int(value) {
    if (value === null || value === undefined) return '--';
    return Number(value).toLocaleString('zh-CN');
  },
  pct(value, digits = 1) {
    if (value === null || value === undefined) return '--';
    return Number(value).toFixed(digits) + '%';
  },
  time(iso, withSeconds = false) {
    if (!iso) return '--';
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return String(iso);
    const p = (n) => String(n).padStart(2, '0');
    return withSeconds
      ? `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
      : `${p(d.getHours())}:${p(d.getMinutes())}`;
  },
  dateTime(iso) {
    if (!iso) return '--';
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return String(iso);
    const p = (n) => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
  },
  duration(seconds) {
    if (seconds === null || seconds === undefined) return '--';
    const s = Math.max(0, Math.floor(seconds));
    if (s < 60) return `${s} 秒`;
    if (s < 3600) return `${Math.floor(s / 60)} 分 ${s % 60} 秒`;
    if (s < 86400) return `${Math.floor(s / 3600)} 小时 ${Math.floor((s % 3600) / 60)} 分`;
    return `${Math.floor(s / 86400)} 天 ${Math.floor((s % 86400) / 3600)} 小时`;
  },
  age(seconds) {
    if (seconds === null || seconds === undefined) return '--';
    if (seconds < 60) return `${Math.floor(seconds)} 秒前`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`;
    return `${Math.floor(seconds / 86400)} 天前`;
  },
  metric(key, value) {
    const meta = METRIC_META[key] || { unit: '' };
    if (value === null || value === undefined) return '--';
    return `${Number(value).toFixed(2)}${meta.unit ? ' ' + meta.unit : ''}`;
  },
};

function clamp(v, min, max) { return Math.max(min, Math.min(max, v)); }

/* 水质指标健康判定：依据《海水水质标准》GB 3097 与养殖水质经验值 */
function metricStatus(key, value) {
  if (value === null || value === undefined) return 'mute';
  if (key === 'dissolved_oxygen') return value < 3 ? 'crit' : value < 5 ? 'warn' : 'ok';
  if (key === 'chlorophyll_a') return value > 20 ? 'crit' : value > 10 ? 'warn' : 'ok';
  if (key === 'water_temp') return value > 28 || value < 10 ? 'warn' : 'ok';
  if (key === 'ph') return value > 8.6 || value < 7.6 ? 'warn' : 'ok';
  if (key === 'turbidity') return value > 50 ? 'warn' : 'ok';
  if (key === 'salinity') return value < 22 ? 'warn' : 'ok';
  return 'ok';
}

/* ------------------------------------------------------------------ */
/* 图标（Lucide 风格线性图标，统一 24×24 视窗）                          */
/* ------------------------------------------------------------------ */
const ICONS = {
  waves: '<path d="M2 6c.6.5 1.2 1 2.5 1C7 7 7 5 9.5 5c2.6 0 2.4 2 5 2 2.5 0 2.5-2 5-2 1.3 0 1.9.5 2.5 1"/><path d="M2 12c.6.5 1.2 1 2.5 1C7 13 7 11 9.5 11c2.6 0 2.4 2 5 2 2.5 0 2.5-2 5-2 1.3 0 1.9.5 2.5 1"/><path d="M2 18c.6.5 1.2 1 2.5 1C7 19 7 17 9.5 17c2.6 0 2.4 2 5 2 2.5 0 2.5-2 5-2 1.3 0 1.9.5 2.5 1"/>',
  activity: '<path d="M22 12h-4l-3 9L9 3l-3 9H2"/>',
  thermometer: '<path d="M14 14.76V3.5a2.5 2.5 0 0 0-5 0v11.26a4.5 4.5 0 1 0 5 0z"/>',
  droplet: '<path d="M12 2.69l5.66 5.66a8 8 0 1 1-11.31 0z"/>',
  flask: '<path d="M9 3h6v5l4 9a2 2 0 0 1-1.8 3H6.8A2 2 0 0 1 5 17l4-9V3z"/><path d="M8 3h8"/>',
  gauge: '<path d="M12 15l3.5-3.5"/><path d="M20.3 18a9 9 0 1 0-16.6 0"/>',
  alert: '<path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><path d="M12 9v4"/><path d="M12 17h.01"/>',
  bell: '<path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/>',
  cpu: '<rect x="4" y="4" width="16" height="16" rx="2"/><rect x="9" y="9" width="6" height="6"/><path d="M9 2v2M15 2v2M9 20v2M15 20v2M2 9h2M2 15h2M20 9h2M20 15h2"/>',
  server: '<rect x="2" y="2" width="20" height="8" rx="2"/><rect x="2" y="14" width="20" height="8" rx="2"/><path d="M6 6h.01M6 18h.01"/>',
  radio: '<circle cx="12" cy="12" r="2"/><path d="M4.9 4.9a10 10 0 0 0 0 14.2M19.1 4.9a10 10 0 0 1 0 14.2M7.8 7.8a6 6 0 0 0 0 8.4M16.2 7.8a6 6 0 0 1 0 8.4"/>',
  camera: '<path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"/><circle cx="12" cy="13" r="4"/>',
  satellite: '<path d="M13 7 9 3 5 7l4 4"/><path d="m17 11 4 4-4 4-4-4"/><path d="m8 12 4 4 6-6-4-4z"/><path d="M16 8 3 21"/>',
  leaf: '<path d="M11 20A7 7 0 0 1 9.8 6.1C15.5 5 17 4.48 19 2c1 2 2 4.18 2 8 0 5.5-4.78 10-10 10z"/><path d="M2 21c0-3 1.85-5.36 5.08-6"/>',
  map: '<path d="m3 6 6-3 6 3 6-3v15l-6 3-6-3-6 3z"/><path d="M9 3v15M15 6v15"/>',
  layout: '<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18M9 21V9"/>',
  list: '<path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01"/>',
  history: '<path d="M3 3v5h5"/><path d="M3.05 13A9 9 0 1 0 6 5.3L3 8"/><path d="M12 7v5l4 2"/>',
  settings: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.6 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.6a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>',
  users: '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>',
  logout: '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="m16 17 5-5-5-5"/><path d="M21 12H9"/>',
  search: '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/>',
  refresh: '<path d="M21 12a9 9 0 1 1-2.64-6.36"/><path d="M21 3v6h-6"/>',
  download: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M7 10l5 5 5-5"/><path d="M12 15V3"/>',
  check: '<path d="M20 6 9 17l-5-5"/>',
  x: '<path d="M18 6 6 18M6 6l12 12"/>',
  anchor: '<circle cx="12" cy="5" r="3"/><path d="M12 22V8"/><path d="M5 12H2a10 10 0 0 0 20 0h-3"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
  battery: '<rect x="2" y="7" width="16" height="10" rx="2"/><path d="M22 11v2"/><path d="M6 11v2"/>',
  signal: '<path d="M2 20h.01M7 20v-4M12 20v-8M17 20V8M22 20V4"/>',
  target: '<circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="6"/><circle cx="12" cy="12" r="2"/>',
  shield: '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>',
  layers: '<path d="m12 2 9 5-9 5-9-5z"/><path d="m3 12 9 5 9-5"/><path d="m3 17 9 5 9-5"/>',
  database: '<ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5v14c0 1.66 4.03 3 9 3s9-1.34 9-3V5"/><path d="M3 12c0 1.66 4.03 3 9 3s9-1.34 9-3"/>',
  timer: '<path d="M10 2h4"/><path d="M12 14l3-3"/><circle cx="12" cy="14" r="8"/>',
  filter: '<path d="M22 3H2l8 9.46V19l4 2v-8.54z"/>',
  play: '<path d="m5 3 14 9-14 9z"/>',
  edit: '<path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.12 2.12 0 0 1 3 3L12 15l-4 1 1-4z"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  lock: '<rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>',
  user: '<path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/>',
  clock: '<circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/>',
  file: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/>',
  wrench: '<path d="M14.7 6.3a4 4 0 0 0 5 5l-9.4 9.4a2.8 2.8 0 0 1-4-4z"/>',
  arrowUp: '<path d="M12 19V5M5 12l7-7 7 7"/>',
  arrowDown: '<path d="M12 5v14M19 12l-7 7-7-7"/>',
};

function icon(name, size = 16) {
  const body = ICONS[name] || ICONS.activity;
  return `<svg viewBox="0 0 24 24" width="${size}" height="${size}" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${body}</svg>`;
}

/* 轻提示 */
function toast(message, kind = 'ok', timeout = 3200) {
  let stack = document.querySelector('.toast-stack');
  if (!stack) {
    stack = document.createElement('div');
    stack.className = 'toast-stack';
    document.body.appendChild(stack);
  }
  const node = document.createElement('div');
  node.className = `toast ${kind}`;
  node.textContent = message;
  stack.appendChild(node);
  setTimeout(() => node.remove(), timeout);
}

/* 1920×1080 基准等比缩放：宽屏时横向铺满，左右不留黑边 */
function mountScreenScale(onLayout) {
  const el = document.getElementById('screen');
  if (!el) return () => {};
  const resize = () => {
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    const scale = Math.min(vw / 1920, vh / 1080) || 1;
    // 画布尺寸随视口外扩，HUD 始终贴在真实屏幕边缘，中间沙盘自动获得多余空间
    el.style.width = Math.max(1920, vw / scale) + 'px';
    el.style.height = Math.max(1080, vh / scale) + 'px';
    el.style.transform = `translate(-50%, -50%) scale(${scale})`;
    if (onLayout) onLayout(scale);
  };
  resize();
  window.addEventListener('resize', resize);
  return () => window.removeEventListener('resize', resize);
}

window.DeepBlue = { api, store, fmt, icon, ICONS, toast, createRealtime, mountScreenScale, clamp, metricStatus, METRIC_ORDER, METRIC_META, ApiError };

})();
