const DATASET_LABELS = {
  indiamart: "IndiaMART",
  abhfl: "ABHFL",
  amber: "Amber",
  muthoot: "Muthoot",
};

const AUDIO_SYNC_STORAGE_KEY = "golden_set_audio_sync";

function readAudioSyncPreference() {
  try {
    const stored = localStorage.getItem(AUDIO_SYNC_STORAGE_KEY);
    if (stored === "0" || stored === "false") return false;
    if (stored === "1" || stored === "true") return true;
  } catch {
    /* ignore */
  }
  return true;
}

const state = {
  dataset: "indiamart",
  page: 1,
  perPage: 50,
  search: "",
  status: "all",
  domain: "",
  subdomain: "",
  labelStatus: "all",
  sort: "number",
  selectedId: null,
  selectedCallIds: new Set(),
  exportScope: "selected",
  currentCall: null,
  draft: [],
  labelDraft: {
    domain: "",
    subdomain: "",
    dirty: false,
  },
  labelSuggestions: { domains: [], subdomains: [], byDomain: {} },
  searchTimer: null,
  phraseTimer: null,
  activeSuggestIndex: -1,
  activeTextarea: null,
  suggestKind: "phrase",
  wavesurfer: null,
  highlightTimer: null,
  lastActiveTurn: -1,
  savedSnapshot: "",
  translitCache: new Map(),
  translitInflight: new Map(),
  translitTimer: null,
  wordRefreshTimer: null,
  playbackWords: [],
  turnSegments: [],
  currentUser: "",
  lastStats: null,
  canManageSarvamStt: document.body.dataset.canManageSarvam === "true",
  canManageLabelLlm: document.body.dataset.canManageLabel === "true",
  audioSyncEnabled: readAudioSyncPreference(),
};

const els = {
  stats: document.getElementById("stats"),
  queueStats: document.getElementById("queueStats"),
  subtitle: document.getElementById("subtitle"),
  datasetTabs: document.getElementById("datasetTabs"),
  progressPanel: document.getElementById("progressPanel"),
  progressFill: document.getElementById("progressFill"),
  progressMeta: document.getElementById("progressMeta"),
  labelProgressPanel: document.getElementById("labelProgressPanel"),
  labelProgressFill: document.getElementById("labelProgressFill"),
  labelProgressMeta: document.getElementById("labelProgressMeta"),
  search: document.getElementById("search"),
  statusFilter: document.getElementById("statusFilter"),
  domainFilter: document.getElementById("domainFilter"),
  subdomainFilter: document.getElementById("subdomainFilter"),
  labelStatusFilter: document.getElementById("labelStatusFilter"),
  sortSelect: document.getElementById("sortSelect"),
  callList: document.getElementById("callList"),
  pagination: document.getElementById("pagination"),
  emptyState: document.getElementById("emptyState"),
  callDetail: document.getElementById("callDetail"),
  callId: document.getElementById("callId"),
  saveStatusBadge: document.getElementById("saveStatusBadge"),
  callMeta: document.getElementById("callMeta"),
  player: document.getElementById("player"),
  waveform: document.getElementById("waveform"),
  playPauseBtn: document.getElementById("playPauseBtn"),
  timeDisplay: document.getElementById("timeDisplay"),
  timeRemaining: document.getElementById("timeRemaining"),
  speedSelect: document.getElementById("speedSelect"),
  volumeSlider: document.getElementById("volumeSlider"),
  audioSyncToggle: document.getElementById("audioSyncToggle"),
  realignTimestampsBtn: document.getElementById("realignTimestampsBtn"),
  syncHint: document.getElementById("syncHint"),
  transcriptGrid: document.getElementById("transcriptGrid"),
  prevCallBtn: document.getElementById("prevCallBtn"),
  nextCallBtn: document.getElementById("nextCallBtn"),
  saveBtn: document.getElementById("saveBtn"),
  resetBtn: document.getElementById("resetBtn"),
  verifyBtn: document.getElementById("verifyBtn"),
  unfitBtn: document.getElementById("unfitBtn"),
  uploadInput: document.getElementById("uploadInput"),
  uploadHint: document.getElementById("uploadHint"),
  startSttBtn: document.getElementById("startSttBtn"),
  startLabelBtn: document.getElementById("startLabelBtn"),
  exportDataBtn: document.getElementById("exportDataBtn"),
  exportModal: document.getElementById("exportModal"),
  closeExportModal: document.getElementById("closeExportModal"),
  cancelExportBtn: document.getElementById("cancelExportBtn"),
  confirmExportBtn: document.getElementById("confirmExportBtn"),
  exportStatusFilter: document.getElementById("exportStatusFilter"),
  exportSelectedCount: document.getElementById("exportSelectedCount"),
  exportFilteredCount: document.getElementById("exportFilteredCount"),
  exportAllCount: document.getElementById("exportAllCount"),
  exportHint: document.getElementById("exportHint"),
  selectAllPage: document.getElementById("selectAllPage"),
  selectionCount: document.getElementById("selectionCount"),
  labelPanel: document.getElementById("labelPanel"),
  labelStatusChip: document.getElementById("labelStatusChip"),
  labelDomainInput: document.getElementById("labelDomainInput"),
  labelSubdomainInput: document.getElementById("labelSubdomainInput"),
  labelDomainSuggestions: document.getElementById("labelDomainSuggestions"),
  labelSubdomainSuggestions: document.getElementById("labelSubdomainSuggestions"),
  labelAutoHint: document.getElementById("labelAutoHint"),
  saveLabelBtn: document.getElementById("saveLabelBtn"),
  resetLabelBtn: document.getElementById("resetLabelBtn"),
  clearLabelBtn: document.getElementById("clearLabelBtn"),
  rerunLabelBtn: document.getElementById("rerunLabelBtn"),
  currentUser: document.getElementById("currentUser"),
  suggestPopup: document.getElementById("suggestPopup"),
  toast: document.getElementById("toast"),
  settingsBtn: document.getElementById("settingsBtn"),
  settingsMenu: document.getElementById("settingsMenu"),
  notifBtn: document.getElementById("notifBtn"),
  notifDot: document.getElementById("notifDot"),
  workspace: document.getElementById("workspace"),
};

state.currentUser = (els.currentUser?.textContent || "").trim();

function getReviewer() {
  return state.currentUser || "";
}

function setAudioSyncEnabled(enabled) {
  state.audioSyncEnabled = Boolean(enabled);
  try {
    localStorage.setItem(AUDIO_SYNC_STORAGE_KEY, state.audioSyncEnabled ? "1" : "0");
  } catch {
    /* ignore */
  }
  updateAudioSyncChrome();
  if (!state.audioSyncEnabled) {
    clearActiveWordHighlight();
    state.lastActiveTurn = -1;
  } else {
    syncHighlight();
  }
}

function updateAudioSyncChrome() {
  if (!els.audioSyncToggle) return;
  const enabled = state.audioSyncEnabled;
  els.audioSyncToggle.classList.toggle("active", enabled);
  els.audioSyncToggle.setAttribute("aria-pressed", enabled ? "true" : "false");
  els.audioSyncToggle.title = enabled
    ? "Disable audio–transcript sync"
    : "Enable audio–transcript sync";
  els.audioSyncToggle.innerHTML = enabled
    ? `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M12 2v4M12 18v4M4.9 4.9l2.8 2.8M16.3 16.3l2.8 2.8M2 12h4M18 12h4M4.9 19.1l2.8-2.8M16.3 7.7l2.8-2.8"/><circle cx="12" cy="12" r="3"/></svg> Sync on`
    : `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M12 2v4M12 18v4M4.9 4.9l2.8 2.8M16.3 16.3l2.8 2.8M2 12h4M18 12h4M4.9 19.1l2.8-2.8M16.3 7.7l2.8-2.8"/><circle cx="12" cy="12" r="3" opacity="0.35"/></svg> Sync off`;
  if (els.syncHint) {
    els.syncHint.textContent = enabled
      ? "Original text · STT-matched timestamps · click a word to seek"
      : "Sync off · Space play/pause · ←/→ ±10s";
  }
}

function clearActiveWordHighlight() {
  els.transcriptGrid?.querySelectorAll(".sync-word.active-word").forEach((span) => {
    span.classList.remove("active-word");
  });
}

function updateWordHighlights(time) {
  let activeTurn = -1;
  const tolerance = 0.04;

  els.transcriptGrid
    ?.querySelectorAll('[data-source="original"] .sync-word, [data-source="final"] .sync-word')
    .forEach((span) => {
    const start = Number(span.dataset.start);
    const end = Number(span.dataset.end);
    const wordEnd = Number.isFinite(end) && end > start ? end : start + 0.2;
    const active =
      state.audioSyncEnabled &&
      Number.isFinite(start) &&
      time + tolerance >= start &&
      time - tolerance < wordEnd;
    span.classList.toggle("active-word", active);
    if (active) {
      const block = span.closest(".turn-block");
      if (block) activeTurn = Number(block.dataset.index);
    }
  });
  return activeTurn;
}

function refreshWordTracksForTurn(index) {
  const msg = state.draft[index];
  const block = els.transcriptGrid.querySelector(`.turn-block[data-index="${index}"]`);
  if (!msg || !block) return;

  const replaceNode = (selector, html) => {
    const node = block.querySelector(selector);
    if (node) node.outerHTML = html;
  };

  replaceNode(
    '[data-source="original"]',
    renderSyncedColumn(msg.originalContent, msg.originalWordTimings, "original", index)
  );
  replaceNode(
    '[data-source="sarvam"]',
    renderSyncedColumn(msg.sttContent, msg.sttWordTimings, "sarvam", index)
  );
  const finalHost = block.querySelector(".final-word-host");
  if (finalHost) {
    finalHost.innerHTML = renderWordTrackHtml(msg.wordTimings, "final", index);
  }
  rebuildPlaybackWordIndex();
}

function datasetParams(extra = {}) {
  return new URLSearchParams({ dataset: state.dataset, ...extra });
}

function apiUrl(path, extra = {}) {
  return `${path}?${datasetParams(extra)}`;
}

function showToast(message, tone = "") {
  els.toast.textContent = message;
  els.toast.classList.remove("hidden", "success", "error");
  if (tone) els.toast.classList.add(tone);
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => {
    els.toast.classList.add("hidden");
    els.toast.classList.remove("success", "error");
  }, 2800);
}

function updateTimeDisplays(current, duration) {
  const remaining = Math.max(0, (duration || 0) - (current || 0));
  els.timeDisplay.textContent = `${formatTime(current)} / ${formatTime(duration)}`;
  if (els.timeRemaining) {
    els.timeRemaining.textContent = `−${formatTime(remaining)}`;
  }
}

function detectLanguageLabel() {
  const sample = state.draft.map((m) => m.content || m.originalContent || "").join(" ");
  if (!sample.trim()) return "—";
  const hasDevanagari = /[\u0900-\u097F]/.test(sample);
  const hasLatin = /[A-Za-z]/.test(sample);
  if (hasDevanagari && hasLatin) return "hi / en";
  if (hasDevanagari) return "hi-IN";
  if (hasLatin) return "en";
  return "—";
}

function callDurationLabel(item) {
  if (item.duration != null && Number.isFinite(Number(item.duration))) {
    return formatTime(Number(item.duration));
  }
  return null;
}

function queueProgressPercent(item) {
  if (item.status === "verified") return 100;
  if (item.status === "verified_once") return 83;
  if (item.status === "edited") return 66;
  if (item.status === "unfit") return 33;
  if (item.hasStt) return 33;
  return 0;
}

function sortCallItems(items) {
  const sorted = [...items];
  const rank = { pending: 0, edited: 1, verified_once: 2, verified: 3, unfit: -1 };
  if (state.sort === "status") {
    sorted.sort(
      (a, b) =>
        (rank[a.status] ?? 0) - (rank[b.status] ?? 0) ||
        (a.number || 0) - (b.number || 0)
    );
  } else if (state.sort === "id") {
    sorted.sort((a, b) => String(a.id).localeCompare(String(b.id)));
  } else {
    sorted.sort((a, b) => (a.number || 0) - (b.number || 0));
  }
  return sorted;
}

function autoResizeTextarea(textarea) {
  if (!textarea) return;
  textarea.style.height = "auto";
  textarea.style.height = `${Math.max(120, textarea.scrollHeight)}px`;
}

function updateCharCount(textarea) {
  const index = textarea?.dataset?.index;
  if (index == null) return;
  const counter = els.transcriptGrid.querySelector(`.char-count[data-index="${index}"]`);
  if (counter) counter.textContent = `${textarea.value.length} chars`;
}

async function fetchJSON(url, options) {
  const res = await fetch(url, {
    credentials: "same-origin",
    ...options,
  });
  if (res.status === 401) {
    window.location.href = "/login";
    throw new Error("Login required");
  }
  const text = await res.text();
  let data = {};
  try {
    data = text ? JSON.parse(text) : {};
  } catch {
    throw new Error(text.slice(0, 180) || `Request failed (${res.status})`);
  }
  if (!res.ok) {
    throw new Error(data.error || data.message || `Request failed (${res.status})`);
  }
  return data;
}

function escapeHtml(text) {
  return String(text)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function formatTime(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return "0:00";
  const m = Math.floor(seconds / 60);
  const s = seconds - m * 60;
  if (Math.abs(s - Math.round(s)) < 0.05) {
    return `${m}:${String(Math.round(s)).padStart(2, "0")}`;
  }
  return `${m}:${s.toFixed(1).padStart(4, "0")}`;
}

const TIMING_PRECISION = 3;

function roundTime(seconds) {
  if (!Number.isFinite(seconds)) return null;
  const factor = 10 ** TIMING_PRECISION;
  return Math.round(seconds * factor) / factor;
}

function formatTimeInput(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return "";
  const rounded = roundTime(seconds);
  const m = Math.floor(rounded / 60);
  const s = rounded - m * 60;
  return `${m}:${s.toFixed(TIMING_PRECISION).padStart(TIMING_PRECISION + 3, "0")}`;
}

function parseTimeInput(value) {
  const raw = String(value || "").trim();
  if (!raw) return null;
  if (raw.includes(":")) {
    const [mins, secs] = raw.split(":", 2);
    const minutes = Number(mins);
    const seconds = Number(secs);
    if (!Number.isFinite(minutes) || !Number.isFinite(seconds) || minutes < 0 || seconds < 0) {
      return null;
    }
    return roundTime(minutes * 60 + seconds);
  }
  const seconds = Number(raw);
  return Number.isFinite(seconds) && seconds >= 0 ? roundTime(seconds) : null;
}

function tokenizeWords(text) {
  const cleaned = String(text || "").trim();
  if (!cleaned || cleaned === "—" || cleaned === "Not generated yet") return [];
  return cleaned.split(/\s+/).filter(Boolean);
}

function timingsFromCreatedAt(messages) {
  if (!messages?.length) return [];
  const stamps = messages.map((msg) => {
    const value = Number(msg.createdAt);
    return Number.isFinite(value) ? value : null;
  });
  const valid = stamps.filter((t) => t != null && t >= 0);
  if (!valid.length) return messages.map(() => ({ start: null, end: null }));

  const base = Math.min(...valid);
  const relative = stamps.map((t) => {
    if (t == null) return null;
    if (t >= 1_000_000_000) return Math.max(0, t - base);
    return Math.max(0, t);
  });

  const starts = [];
  let prev = 0;
  for (const rel of relative) {
    if (rel == null) {
      starts.push(null);
      continue;
    }
    let start = rel;
    if (starts.length && prev != null && start < prev) start = prev;
    if (starts.length && prev != null && Math.abs(start - prev) < 0.05) {
      start = prev + 0.05;
    }
    starts.push(start);
    prev = start;
  }

  return messages.map((msg, index) => {
    const start = starts[index];
    if (start == null) return { start: null, end: null };
    let end = null;
    for (let i = index + 1; i < starts.length; i += 1) {
      if (starts[i] != null && starts[i] > start) {
        end = starts[i];
        break;
      }
    }
    if (end == null) {
      const contentLen = String(msg.content || "").trim().length;
      end = start + Math.max(1.2, Math.min(10, contentLen / 14 || 1.5));
    }
    if (end <= start) end = start + 0.2;
    return { start: roundTime(start), end: roundTime(end) };
  });
}

function buildWordTimings(text, start, end) {
  const words = tokenizeWords(text);
  if (!words.length) return [];
  if (start == null || !Number.isFinite(start)) return [];

  const startTime = roundTime(start);
  let endTime =
    end != null && Number.isFinite(end) && end > startTime
      ? roundTime(end)
      : roundTime(startTime + words.length * 0.28);
  if (endTime <= startTime) {
    endTime = roundTime(startTime + words.length * 0.28);
  }

  const duration = endTime - startTime;
  const weights = words.map((word) => {
    const core = word.replace(/[^\w\u0900-\u097F]/g, "");
    return Math.max(1, core.length || word.length || 1);
  });
  const totalWeight = weights.reduce((sum, weight) => sum + weight, 0) || words.length;
  let cursor = startTime;

  return words.map((word, index) => {
    const slice = (weights[index] / totalWeight) * duration;
    const wordStart = roundTime(cursor);
    const wordEnd = roundTime(index === words.length - 1 ? endTime : cursor + slice);
    cursor = wordEnd;
    return { word, start: wordStart, end: wordEnd };
  });
}

function segmentTimes(seg) {
  const pick = (keys) => {
    for (const key of keys) {
      if (seg[key] != null && seg[key] !== "") {
        const value = Number(seg[key]);
        if (Number.isFinite(value)) return value;
      }
    }
    return null;
  };
  const start = roundTime(
    pick([
      "start_s",
      "start_time_seconds",
      "start_time_sec",
      "start_seconds",
      "start_sec",
      "start",
      "startSeconds",
      "start_time",
    ])
  );
  const end = roundTime(
    pick([
      "end_s",
      "end_time_seconds",
      "end_time_sec",
      "end_seconds",
      "end_sec",
      "end",
      "endSeconds",
      "end_time",
    ])
  );
  if (!Number.isFinite(start)) return { start: null, end: null };
  if (!Number.isFinite(end) || end <= start) {
    return { start, end: roundTime(start + 0.35) };
  }
  return { start, end };
}

function mergeAdjacentSegments(segments) {
  if (segments.length <= 1) return segments;
  const segs = segments.map((seg) => ({ ...seg }));
  while (segs.length > 1) {
    let bestI = segs.length - 2;
    let bestGap = Infinity;
    for (let i = 0; i < segs.length - 1; i += 1) {
      const left = segmentTimes(segs[i]);
      const right = segmentTimes(segs[i + 1]);
      const gap =
        left.end != null && right.start != null
          ? right.start - left.end
          : 0;
      if (gap < bestGap) {
        bestGap = gap;
        bestI = i;
      }
    }
    const left = segs[bestI];
    const right = segs[bestI + 1];
    const leftTimes = segmentTimes(left);
    const rightTimes = segmentTimes(right);
    segs.splice(bestI, 2, {
      ...left,
      content: `${left.content || ""} ${right.content || ""}`.trim(),
      start_s: leftTimes.start,
      end_s: rightTimes.end,
      start_time_seconds: leftTimes.start,
      end_time_seconds: rightTimes.end,
    });
    if (segs.length === 1) break;
  }
  return segs;
}

function fitSegmentsToCount(segments, target) {
  if (target <= 0) return [];
  if (segments.length <= target) return segments;
  let segs = segments.map((seg) => ({ ...seg }));
  while (segs.length > target) {
    let bestI = segs.length - 2;
    let bestGap = Infinity;
    for (let i = 0; i < segs.length - 1; i += 1) {
      const left = segmentTimes(segs[i]);
      const right = segmentTimes(segs[i + 1]);
      const gap =
        left.end != null && right.start != null
          ? right.start - left.end
          : 0;
      if (gap < bestGap) {
        bestGap = gap;
        bestI = i;
      }
    }
    const left = segs[bestI];
    const right = segs[bestI + 1];
    const leftTimes = segmentTimes(left);
    const rightTimes = segmentTimes(right);
    segs.splice(bestI, 2, {
      ...left,
      content: `${left.content || ""} ${right.content || ""}`.trim(),
      start_s: leftTimes.start,
      end_s: rightTimes.end,
      start_time_seconds: leftTimes.start,
      end_time_seconds: rightTimes.end,
    });
  }
  return segs;
}

function assignSegmentsToTurns(turns, segments) {
  const assignments = turns.map(() => []);
  if (!segments.length || !turns.length) return assignments;

  const roleAware = segments.some((seg) =>
    ["assistant", "user"].includes(String(seg.role || ""))
  );

  if (roleAware) {
    const pools = { assistant: [], user: [] };
    for (const seg of segments) {
      const role = String(seg.role || "");
      if (role in pools) pools[role].push(seg);
    }
    const roleTargets = { assistant: 0, user: 0 };
    for (const turn of turns) {
      const role = turn.role === "user" ? "user" : "assistant";
      if (role in roleTargets) roleTargets[role] += 1;
    }
    for (const role of Object.keys(roleTargets)) {
      pools[role] = fitSegmentsToCount(pools[role] || [], roleTargets[role]);
    }
    const cursor = { assistant: 0, user: 0 };
    turns.forEach((turn, index) => {
      const role = turn.role === "user" ? "user" : "assistant";
      const pool = pools[role] || [];
      const idx = cursor[role];
      if (idx < pool.length) {
        assignments[index] = [pool[idx]];
        cursor[role] += 1;
      }
    });
    return assignments;
  }

  const fitted = fitSegmentsToCount(segments, turns.length);
  fitted.forEach((seg, index) => {
    if (assignments[index]) assignments[index] = [seg];
  });
  return assignments;
}

function buildWordTimingsFromSegments(segments) {
  const out = [];
  const sorted = [...segments].sort((a, b) => {
    const left = segmentTimes(a).start ?? 0;
    const right = segmentTimes(b).start ?? 0;
    return left - right;
  });
  for (const seg of sorted) {
    const { start, end } = segmentTimes(seg);
    if (start == null) continue;
    const content = String(seg.content || seg.transcript || "").trim();
    out.push(...buildWordTimings(content, start, end));
  }
  return out;
}

function remapWordTimings(text, reference, turnStart, turnEnd) {
  const words = tokenizeWords(text);
  if (!words.length) return [];
  if (!reference?.length) return buildWordTimings(text, turnStart, turnEnd);
  if (words.length === reference.length) {
    return words.map((word, index) => ({
      word,
      start: reference[index].start,
      end: reference[index].end,
    }));
  }
  const refStart = reference[0].start;
  const refEnd = reference[reference.length - 1].end;
  return buildWordTimings(text, refStart, refEnd);
}

function buildPhraseTimingsFromSegments(segments) {
  const sorted = [...segments].sort((a, b) => {
    const left = segmentTimes(a).start ?? 0;
    const right = segmentTimes(b).start ?? 0;
    return left - right;
  });
  const out = [];
  for (const seg of sorted) {
    const { start, end } = segmentTimes(seg);
    if (start == null) continue;
    const text = String(seg.content || seg.transcript || "").trim();
    if (!text) continue;
    out.push({ word: text, start, end });
  }
  return out;
}

function segmentsForTurnWindow(start, end, segments, fallback = [], role = null) {
  const pad = 0.15;
  if (
    start != null &&
    end != null &&
    Number.isFinite(start) &&
    Number.isFinite(end) &&
    segments.length
  ) {
    let overlapping = segments.filter((seg) => {
      const times = segmentTimes(seg);
      if (times.start == null) return false;
      const segEnd = times.end ?? times.start + 0.3;
      return times.start < end + pad && segEnd > start - pad;
    });
    if (role) {
      const roleHits = overlapping.filter((seg) => {
        const segRole = String(seg.role || "");
        return !segRole || segRole === "unknown" || segRole === role;
      });
      if (roleHits.length) overlapping = roleHits;
    }
    overlapping.sort(
      (a, b) => (segmentTimes(a).start ?? 0) - (segmentTimes(b).start ?? 0)
    );
    if (overlapping.length) return overlapping;
  }
  return fallback;
}

function boundsFromSegments(segments) {
  if (!segments?.length) return { start: null, end: null };
  const starts = [];
  const ends = [];
  for (const seg of segments) {
    const times = segmentTimes(seg);
    if (times.start != null) starts.push(times.start);
    if (times.end != null) ends.push(times.end);
  }
  return {
    start: starts.length ? Math.min(...starts) : null,
    end: ends.length ? Math.max(...ends) : null,
  };
}

function sttMessageBounds(sttMsg) {
  if (!sttMsg) return { start: null, end: null };
  const start = Number(
    sttMsg.start_s ?? sttMsg.start_time_seconds ?? sttMsg.start ?? NaN
  );
  const end = Number(sttMsg.end_s ?? sttMsg.end_time_seconds ?? sttMsg.end ?? NaN);
  return {
    start: Number.isFinite(start) ? roundTime(start) : null,
    end: Number.isFinite(end) ? roundTime(end) : null,
  };
}

function attachWordTimings(msg, segmentsForTurn = []) {
  const segmentWords = buildWordTimingsFromSegments(segmentsForTurn);
  const sttRef = segmentWords.length
    ? segmentWords
    : buildWordTimings(msg.sttContent, msg.start, msg.end);

  msg.segments = segmentsForTurn;
  msg.sttWordTimings = [];
  msg.originalWordTimings = msg.originalContent
    ? sttRef.length
      ? remapWordTimings(msg.originalContent, sttRef, msg.start, msg.end)
      : buildWordTimings(msg.originalContent, msg.start, msg.end)
    : [];
  msg.wordTimings = msg.originalWordTimings.length
    ? remapWordTimings(msg.content, msg.originalWordTimings, msg.start, msg.end)
    : buildWordTimings(msg.content, msg.start, msg.end);
}

function rebuildPlaybackWordIndex() {
  const entries = [];
  state.draft.forEach((msg, turnIndex) => {
    for (const item of msg.originalWordTimings || []) {
      entries.push({ ...item, turnIndex, source: "original" });
    }
  });
  entries.sort((a, b) => a.start - b.start);
  state.playbackWords = entries;
}

function renderWordTrackHtml(wordTimings, source = "final", turnIndex = null) {
  if (!wordTimings?.length) {
    return `<div class="word-sync-track empty" data-source="${source}"><span class="word-sync-empty">No timing</span></div>`;
  }
  return `<div class="word-sync-track" data-source="${source}">${wordTimings
    .map(
      (item, wordIndex) =>
        `<button type="button" class="sync-word" data-turn="${turnIndex ?? ""}" data-word-idx="${wordIndex}" data-start="${item.start}" data-end="${item.end}" title="${formatTimeInput(item.start)}–${formatTimeInput(item.end)}">${escapeHtml(item.word)}</button>`
    )
    .join("")}</div>`;
}

function renderSyncedColumn(text, wordTimings, source, turnIndex = null) {
  if (wordTimings?.length) {
    return renderWordTrackHtml(wordTimings, source, turnIndex);
  }
  const cleaned = String(text || "").trim();
  return cleaned
    ? `<div class="plain-text" data-source="${source}">${escapeHtml(cleaned)}</div>`
    : `<div class="plain-text muted" data-source="${source}">—</div>`;
}

function defaultTimingForInsertedTurn(afterIndex) {
  const prev = state.draft[afterIndex];
  const next = state.draft[afterIndex + 1];
  const playhead = getPlaybackTime();
  const duration = getPlaybackDuration();

  if (Number.isFinite(playhead) && playhead >= 0) {
    const start = roundTime(playhead);
    const end = roundTime(Math.min(duration || start + 2, start + 2));
    return { start, end: end > start ? end : roundTime(start + 0.5) };
  }

  const prevEnd = prev?.end ?? prev?.start;
  if (prevEnd != null && Number.isFinite(prevEnd)) {
    const start = roundTime(prevEnd + 0.05);
    return { start, end: roundTime(start + 2) };
  }

  const nextStart = next?.start;
  if (nextStart != null && Number.isFinite(nextStart)) {
    const end = roundTime(nextStart);
    return { start: roundTime(Math.max(0, end - 2)), end };
  }

  return { start: 0, end: 2 };
}

function statusLabel(status) {
  if (status === "verified") return "Fully verified";
  if (status === "verified_once") return "Verified once";
  if (status === "edited") return "Final saved";
  if (status === "unfit") return "Unfit";
  return "Final not saved";
}

function queueStatusBadge(status) {
  if (status === "verified") return { className: "verified", label: "Verified" };
  if (status === "verified_once") return { className: "verified-once", label: "Verified once" };
  if (status === "edited") return { className: "edited", label: "Saved" };
  if (status === "unfit") return { className: "unfit", label: "Unfit" };
  return { className: "pending", label: "Draft" };
}

function updateSelectionChrome() {
  const count = state.selectedCallIds.size;
  if (els.selectionCount) {
    els.selectionCount.textContent = `${count} selected`;
  }
  if (els.exportSelectedCount) {
    els.exportSelectedCount.textContent = String(count);
  }
}

function updateDatasetChrome() {
  els.datasetTabs.querySelectorAll(".dataset-tab").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.dataset === state.dataset);
  });
  const label = DATASET_LABELS[state.dataset] || state.dataset;
  els.subtitle.textContent = `${label} · Voice AI Evaluation Platform`;
  els.uploadHint.textContent = `Upload into ${label}`;
}

async function loadStats(options = {}) {
  const data = await fetchJSON(apiUrl("/api/stats"));
  state.lastStats = data;
  const total = data.total || 0;
  const verified = data.verified || 0;
  const verifiedOnce = data.verifiedOnce || 0;
  const edited = data.edited || 0;
  const pending = data.pending || 0;
  const unfit = data.unfit || 0;
  const reviewed = edited + verifiedOnce + verified;
  const completion = total ? Math.round((verified / total) * 1000) / 10 : 0;
  const attention = pending;

  els.stats.innerHTML = `
    <div class="metric-card">
      <span class="metric-label">Total Conversations</span>
      <span class="metric-value">${total}</span>
    </div>
    <div class="metric-card accent-reviewed">
      <span class="metric-label">Reviewed</span>
      <span class="metric-value">${reviewed}</span>
    </div>
    <div class="metric-card accent-verified-once">
      <span class="metric-label">Verified once</span>
      <span class="metric-value">${verifiedOnce}</span>
    </div>
    <div class="metric-card accent-verified">
      <span class="metric-label">Fully verified</span>
      <span class="metric-value">${verified}</span>
    </div>
    <div class="metric-card accent-pending">
      <span class="metric-label">Pending</span>
      <span class="metric-value">${pending}</span>
    </div>
    <div class="metric-card accent-unfit">
      <span class="metric-label">Unfit</span>
      <span class="metric-value">${unfit}</span>
    </div>
    <div class="metric-card accent-completion">
      <span class="metric-label">Completion %</span>
      <span class="metric-value">${completion}%</span>
    </div>
  `;

  if (els.queueStats) {
    els.queueStats.innerHTML = `
      <div class="queue-stat pending">
        <span class="queue-stat-value">${pending}</span>
        <span class="queue-stat-label">Pending</span>
      </div>
      <div class="queue-stat reviewed">
        <span class="queue-stat-value">${edited}</span>
        <span class="queue-stat-label">Saved</span>
      </div>
      <div class="queue-stat verified-once">
        <span class="queue-stat-value">${verifiedOnce}</span>
        <span class="queue-stat-label">Once</span>
      </div>
      <div class="queue-stat verified">
        <span class="queue-stat-value">${verified}</span>
        <span class="queue-stat-label">Verified</span>
      </div>
      <div class="queue-stat unfit">
        <span class="queue-stat-value">${unfit}</span>
        <span class="queue-stat-label">Unfit</span>
      </div>
    `;
  }

  if (els.notifDot) {
    els.notifDot.classList.toggle("hidden", pending <= 0);
    els.notifDot.title = `${pending} pending`;
  }

  const countEl = document.getElementById(`count-${state.dataset}`);
  if (countEl) countEl.textContent = data.total;

  renderSttProgress(data.sttProgress, data.sttGenerated, data.total);
  renderLabelProgress(data.labelProgress, data.labeled, data.total);
  updateSttButton(Boolean(data.sttProgress?.running));
  updateLabelButton(Boolean(data.labelProgress?.running));

  if (
    options.refreshCalls &&
    (data.sttProgress?.running || data.labelProgress?.running)
  ) {
    await loadCalls();
    if (data.labelProgress?.running) {
      await loadLabelSuggestions();
    }
    if (state.selectedId) {
      try {
        const call = await fetchJSON(apiUrl(`/api/calls/${state.selectedId}`));
        const wasDirty = isDraftDirty();
        state.currentCall = call;
        if (!wasDirty && !call.edited) {
          buildDraft(call);
          renderTranscript({ preserveEdits: false });
          refreshSavedSnapshot();
        }
        updateMeta();
        if (!state.labelDraft.dirty) {
          loadLabelDraftFromCall(call);
          renderLabelPanel();
        }
      } catch {
        /* ignore */
      }
    }
  }
}

let sttRunning = false;
let labelRunning = false;

function updateLabelButton(running) {
  labelRunning = running;
  if (!els.startLabelBtn) return;
  els.startLabelBtn.disabled = false;
  if (running) {
    els.startLabelBtn.className = "btn danger";
    els.startLabelBtn.textContent = "Stop labeling";
  } else {
    els.startLabelBtn.className = "btn secondary";
    els.startLabelBtn.textContent = "Auto-label calls";
  }
}

function renderLabelProgress(progress, labeled, total) {
  if (!els.labelProgressPanel || !els.labelProgressFill || !els.labelProgressMeta) return;
  const saved = progress?.savedTotal ?? labeled ?? 0;
  const targetTotal = progress?.total || total || 0;
  const percent =
    progress?.percent ??
    (targetTotal ? Math.round((saved / targetTotal) * 1000) / 10 : 0);
  const running = Boolean(progress?.running);

  els.labelProgressFill.style.width = `${Math.min(100, percent)}%`;
  els.labelProgressPanel.classList.toggle("running", running);
  els.labelProgressPanel.classList.toggle(
    "complete",
    !running && saved >= targetTotal && targetTotal > 0
  );
  els.labelProgressMeta.textContent =
    targetTotal > 0
      ? `${saved}/${targetTotal} labeled (${percent}%)${running ? " · running" : ""}`
      : "Run auto-label on original transcripts";
}

function formatLabel(value) {
  return (value || "")
    .split("_")
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

async function loadLabelSuggestions() {
  try {
    state.labelSuggestions = await fetchJSON(apiUrl("/api/label/suggestions"));
    populateDomainFilter();
    renderLabelSuggestionLists();
  } catch {
    state.labelSuggestions = { domains: [], subdomains: [], byDomain: {} };
  }
}

function renderLabelSuggestionLists() {
  const { domains, subdomains, byDomain } = state.labelSuggestions;
  if (els.labelDomainSuggestions) {
    els.labelDomainSuggestions.innerHTML = (domains || [])
      .map((domain) => `<option value="${escapeHtml(domain)}"></option>`)
      .join("");
  }
  const domain = (els.labelDomainInput?.value || state.labelDraft.domain || "").trim();
  const subs = domain && byDomain?.[domain] ? byDomain[domain] : subdomains || [];
  if (els.labelSubdomainSuggestions) {
    els.labelSubdomainSuggestions.innerHTML = subs
      .map((sub) => `<option value="${escapeHtml(sub)}"></option>`)
      .join("");
  }
}

function populateDomainFilter() {
  if (!els.domainFilter) return;
  const current = state.domain;
  const domains = state.labelSuggestions?.domains || [];
  const options = ['<option value="">All domains</option>']
    .concat(
      domains.map(
        (domain) =>
          `<option value="${domain}" ${domain === current ? "selected" : ""}>${formatLabel(domain)}</option>`
      )
    )
    .join("");
  els.domainFilter.innerHTML = options;
  populateSubdomainFilter();
}

function populateSubdomainFilter() {
  if (!els.subdomainFilter) return;
  const domain = state.domain;
  const current = state.subdomain;
  const byDomain = state.labelSuggestions?.byDomain || {};
  const subs = domain ? byDomain[domain] || [] : state.labelSuggestions?.subdomains || [];
  let options = '<option value="">All subdomains</option>';
  options += subs
    .map(
      (sub) =>
        `<option value="${sub}" ${sub === current ? "selected" : ""}>${formatLabel(sub)}</option>`
    )
    .join("");
  els.subdomainFilter.innerHTML = options;
  els.subdomainFilter.disabled = false;
}

function loadLabelDraftFromCall(call) {
  const label = call?.label;
  state.labelDraft = {
    domain: label?.domain || "",
    subdomain: label?.subdomain || "",
    dirty: false,
  };
  if (els.labelDomainInput) els.labelDomainInput.value = state.labelDraft.domain;
  if (els.labelSubdomainInput) els.labelSubdomainInput.value = state.labelDraft.subdomain;
  renderLabelSuggestionLists();
}

function renderLabelPanel() {
  if (!els.labelPanel) return;
  const label = state.currentCall?.label;
  const auto = label?.auto;
  const status = label?.status || "unlabeled";

  if (els.labelStatusChip) {
    els.labelStatusChip.textContent =
      status === "unlabeled"
        ? "Unlabeled"
        : status === "auto"
          ? "AI-labeled"
          : "Human-edited";
    els.labelStatusChip.className = `label-status-chip ${status === "unlabeled" ? "" : status}`;
  }

  if (els.labelDomainInput && !state.labelDraft.dirty) {
    els.labelDomainInput.value = state.labelDraft.domain || label?.domain || "";
  }
  if (els.labelSubdomainInput && !state.labelDraft.dirty) {
    els.labelSubdomainInput.value = state.labelDraft.subdomain || label?.subdomain || "";
  }
  renderLabelSuggestionLists();

  if (els.labelAutoHint) {
    if (auto?.domain) {
      const conf = auto.subdomainConfidence ?? auto.domainConfidence;
      const pct = conf != null ? ` (${Math.round(conf * 100)}%)` : "";
      els.labelAutoHint.textContent = `AI suggestion: ${formatLabel(auto.domain)} · ${formatLabel(auto.subdomain)}${pct}${auto.rationale ? ` — ${auto.rationale}` : ""}`;
    } else {
      els.labelAutoHint.textContent =
        "Labels come from Gemini on the original transcript. Set GEMINI_API_KEY in .env, then run Auto-label.";
    }
  }

  if (els.saveLabelBtn) {
    els.saveLabelBtn.textContent = state.labelDraft.dirty ? "Save label *" : "Save label";
  }
}

function collectLabelPayload() {
  return {
    domain: (els.labelDomainInput?.value || "").trim(),
    subdomain: (els.labelSubdomainInput?.value || "").trim(),
    isCustom: true,
  };
}

async function saveLabel() {
  if (!state.currentCall) return;
  const payload = collectLabelPayload();
  if (!payload.domain || !payload.subdomain) {
    showToast("Domain and subdomain are required", "error");
    return;
  }
  try {
    const result = await fetchJSON(apiUrl(`/api/calls/${state.currentCall.id}/label`), {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    state.currentCall.label = result.label;
    loadLabelDraftFromCall(state.currentCall);
    renderLabelPanel();
    await loadLabelSuggestions();
    loadCalls().catch(() => {});
    showToast("Label saved", "success");
  } catch (err) {
    showToast(err.message, "error");
  }
}

async function resetLabelToAuto() {
  const auto = state.currentCall?.label?.auto;
  if (!auto?.domain) {
    showToast("No AI suggestion to restore", "error");
    return;
  }
  state.labelDraft = {
    domain: auto.domain,
    subdomain: auto.subdomain || "unknown",
    dirty: true,
  };
  if (els.labelDomainInput) els.labelDomainInput.value = state.labelDraft.domain;
  if (els.labelSubdomainInput) els.labelSubdomainInput.value = state.labelDraft.subdomain;
  renderLabelPanel();
}

async function clearLabel() {
  if (!state.currentCall) return;
  try {
    await fetchJSON(apiUrl(`/api/calls/${state.currentCall.id}/label`), {
      method: "DELETE",
    });
    state.currentCall.label = null;
    loadLabelDraftFromCall(state.currentCall);
    renderLabelPanel();
    await loadLabelSuggestions();
    loadCalls().catch(() => {});
    showToast("Label cleared", "success");
  } catch (err) {
    showToast(err.message, "error");
  }
}

async function rerunAutoLabel() {
  if (!state.currentCall) return;
  try {
    const result = await fetchJSON(apiUrl(`/api/calls/${state.currentCall.id}/label/auto`), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    state.currentCall.label = result.label;
    loadLabelDraftFromCall(state.currentCall);
    renderLabelPanel();
    await loadLabelSuggestions();
    loadCalls().catch(() => {});
    showToast("AI label updated", "success");
  } catch (err) {
    showToast(err.message, "error");
  }
}

async function startAutoLabeling() {
  try {
    updateLabelButton(true);
    const result = await fetchJSON(apiUrl("/api/label/start"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ resume: true, workers: 3 }),
    });
    showToast(
      `Auto-labeling started · ${result.pending} pending · ${result.skipped} skipped`
    );
    await loadStats();
  } catch (err) {
    updateLabelButton(false);
    showToast(err.message);
  }
}

async function stopAutoLabeling() {
  try {
    const result = await fetchJSON(apiUrl("/api/label/stop"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    showToast(result.wasStale ? "Cleared stale label status" : "Auto-labeling stopped");
    await loadStats();
  } catch (err) {
    showToast(err.message);
  }
}

function updateSttButton(running) {
  sttRunning = running;
  if (!els.startSttBtn) return;
  els.startSttBtn.disabled = false;
  if (running) {
    els.startSttBtn.className = "btn danger";
    els.startSttBtn.innerHTML =
      `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><rect x="6" y="6" width="12" height="12" rx="1"/></svg> Stop Sarvam STT`;
  } else {
    els.startSttBtn.className = "btn accent";
    els.startSttBtn.innerHTML =
      `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M12 2a3 3 0 0 1 3 3v6a3 3 0 0 1-6 0V5a3 3 0 0 1 3-3z"/><path d="M19 10a7 7 0 0 1-14 0M12 17v5"/></svg> Start Sarvam STT`;
  }
}

function renderSttProgress(progress, sttGenerated, total) {
  const saved = progress?.savedTotal ?? sttGenerated ?? 0;
  const targetTotal = progress?.total || total || 0;
  const percent =
    progress?.percent ??
    (targetTotal ? Math.round((saved / targetTotal) * 1000) / 10 : 0);
  const running = Boolean(progress?.running);

  els.progressFill.style.width = `${Math.min(100, percent)}%`;
  els.progressPanel.classList.toggle("running", running);
  els.progressPanel.classList.toggle(
    "complete",
    !running && saved >= targetTotal && targetTotal > 0
  );
  els.progressMeta.textContent =
    targetTotal > 0
      ? `${saved}/${targetTotal} STT ready (${percent}%)${running ? " · running" : ""}`
      : state.canManageSarvamStt
        ? "Upload calls, then start Sarvam STT"
        : "Upload calls to review transcripts";
}

function renderPagination(data) {
  els.pagination.innerHTML = `
    <button type="button" id="prevPage" ${data.page <= 1 ? "disabled" : ""}>Previous</button>
    <span>Page ${data.page} of ${data.total_pages}</span>
    <button type="button" id="nextPage" ${data.page >= data.total_pages ? "disabled" : ""}>Next</button>
  `;
  document.getElementById("prevPage").onclick = () => {
    state.page -= 1;
    loadCalls();
  };
  document.getElementById("nextPage").onclick = () => {
    state.page += 1;
    loadCalls();
  };
}

async function loadCalls() {
  els.callList.innerHTML = `
    <li class="skeleton-list" aria-hidden="true">
      <div class="skeleton-card"></div>
      <div class="skeleton-card"></div>
      <div class="skeleton-card"></div>
    </li>
  `;

  const data = await fetchJSON(
    apiUrl("/api/calls", {
      page: state.page,
      per_page: state.perPage,
      search: state.search,
      status: state.status,
      domain: state.domain,
      subdomain: state.subdomain,
      label_status: state.labelStatus,
    })
  );

  if (!data.items.length) {
    els.callList.innerHTML = `<li class="empty-list">No calls yet. Upload a JSON file.</li>`;
    renderPagination(data);
    updateSelectAllPageState([]);
    updateSelectionChrome();
    return;
  }

  const items = sortCallItems(data.items);
  state.lastCallPageIds = items.map((item) => item.id);

  els.callList.innerHTML = items
    .map((item) => {
      const sttBadge = item.hasStt
        ? '<span class="badge stt">Sarvam</span>'
        : '<span class="badge stt-pending">No Sarvam</span>';
      const duration = callDurationLabel(item);
      const durationBadge = duration
        ? `<span class="badge duration">${duration}</span>`
        : "";
      const labelBadge = item.domain
        ? `<span class="badge label-chip ${item.isCustom ? "custom" : ""}">${escapeHtml(formatLabel(item.domain))} · ${escapeHtml(formatLabel(item.subdomain))}</span>`
        : `<span class="badge stt-pending">Unlabeled</span>`;
      const progress = queueProgressPercent(item);
      const statusBadge = queueStatusBadge(item.status);
      const checked = state.selectedCallIds.has(item.id) ? "checked" : "";
      return `
      <li>
        <div class="call-item ${item.id === state.selectedId ? "active" : ""}" data-id="${item.id}">
          <label class="call-select" title="Select for export">
            <input type="checkbox" class="call-select-input" data-id="${item.id}" ${checked} />
          </label>
          <button type="button" class="call-item-main">
          <div class="call-item-top">
            <span class="call-item-number">#${item.number}</span>
            <span class="call-item-id">${item.id}</span>
            <span class="badge-wrap">
              ${sttBadge}
              <span class="badge ${item.status}">${statusLabel(item.status)}</span>
            </span>
          </div>
          <div class="call-item-meta">
            ${durationBadge}
            ${labelBadge}
            <span class="badge ${statusBadge.className}">${statusBadge.label}</span>
          </div>
          <div class="call-preview">${escapeHtml(item.preview)}</div>
          <div class="call-item-progress" aria-hidden="true"><span style="width:${progress}%"></span></div>
          </button>
        </div>
      </li>
    `;
    })
    .join("");

  els.callList.querySelectorAll(".call-item-main").forEach((btn) => {
    btn.onclick = () => selectCall(btn.closest(".call-item").dataset.id);
  });
  els.callList.querySelectorAll(".call-select-input").forEach((input) => {
    input.addEventListener("click", (event) => event.stopPropagation());
    input.addEventListener("change", () => {
      const callId = input.dataset.id;
      if (input.checked) state.selectedCallIds.add(callId);
      else state.selectedCallIds.delete(callId);
      updateSelectionChrome();
      updateSelectAllPageState(state.lastCallPageIds);
    });
  });

  updateSelectAllPageState(state.lastCallPageIds);
  updateSelectionChrome();
  renderPagination(data);
}

function updateSelectAllPageState(pageIds) {
  if (!els.selectAllPage) return;
  const ids = pageIds || [];
  if (!ids.length) {
    els.selectAllPage.checked = false;
    els.selectAllPage.indeterminate = false;
    return;
  }
  const selectedOnPage = ids.filter((id) => state.selectedCallIds.has(id)).length;
  els.selectAllPage.checked = selectedOnPage === ids.length;
  els.selectAllPage.indeterminate = selectedOnPage > 0 && selectedOnPage < ids.length;
}

function buildDraft(call) {
  let finals = [...(call.final_messages || [])];
  const originals = call.messages || [];
  const stt = call.stt_messages || [];
  const timings = call.timings || [];
  const turnSegments = call.timing_segments || [];
  const sttSegments = call.stt_segments || [];
  const hasSttTiming = Boolean(call.hasStt && (stt.length || sttSegments.length || turnSegments.length));

  // A partial save (e.g. one short test turn) must not hide the rest of the call.
  if (finals.length && finals.length < originals.length) {
    finals = finals.concat(
      originals.slice(finals.length).map((msg) => ({
        ...msg,
        content: msg.content ?? "",
      }))
    );
  }
  if (!finals.length) {
    finals = originals.map((msg) => ({ ...msg, content: msg.content ?? "" }));
  }

  // Pair Original / Sarvam by role order so added/deleted Final turns don't
  // shift columns and hide transcript text.
  const originalPools = { assistant: [], user: [] };
  const sttPools = { assistant: [], user: [] };
  for (const msg of originals) {
    const role = msg.role === "user" ? "user" : "assistant";
    originalPools[role].push(msg);
  }
  for (const msg of stt) {
    const role = msg.role === "user" ? "user" : "assistant";
    sttPools[role].push(msg);
  }
  const originalCursor = { assistant: 0, user: 0 };
  const sttCursor = { assistant: 0, user: 0 };

  const originalTimings = timingsFromCreatedAt(originals);
  const timingByOriginalId = {};
  originals.forEach((msg, index) => {
    if (msg._id) timingByOriginalId[msg._id] = originalTimings[index];
  });

  state.sttSegments = sttSegments;
  state.turnSegments = turnSegments;

  state.draft = finals.map((msg, index) => {
    const role = msg.role === "user" ? "user" : "assistant";
    const orig = originalPools[role][originalCursor[role]];
    if (orig) originalCursor[role] += 1;
    const sttMsg = sttPools[role][sttCursor[role]];
    if (sttMsg) sttCursor[role] += 1;
    const content = msg.content ?? "";
    const origTiming = orig?._id ? timingByOriginalId[orig._id] : null;
    const matchedBounds = boundsFromSegments(turnSegments[index] || []);
    const sttBounds = matchedBounds.start != null ? matchedBounds : sttMessageBounds(sttMsg);
    const savedStart = timings[index]?.start;
    const savedEnd = timings[index]?.end;
    const hasSavedBounds =
      savedStart != null &&
      savedEnd != null &&
      Number.isFinite(Number(savedStart)) &&
      Number.isFinite(Number(savedEnd));
    const savedMatchesStt =
      !hasSttTiming ||
      sttBounds.start == null ||
      Math.abs(Number(savedStart) - sttBounds.start) < 1.5;
    const useSaved = hasSavedBounds && savedMatchesStt;
    // If this Final turn is empty/near-empty but Original has text, seed from Original
    // so a corrupt short save doesn't blank the editor.
    const seeded =
      String(content).trim().length < 3 && orig?.content
        ? orig.content
        : content;
    return {
      _id: msg._id || orig?._id || `draft-${index + 1}`,
      role,
      type: "message",
      createdAt: msg.createdAt || orig?.createdAt || "",
      content: seeded,
      originalContent: orig?.content ?? "",
      sttContent: sttMsg?.content ?? "",
      start: useSaved
        ? roundTime(Number(savedStart))
        : hasSttTiming
          ? sttBounds.start ?? origTiming?.start ?? null
          : origTiming?.start ?? sttBounds.start ?? null,
      end: useSaved
        ? roundTime(Number(savedEnd))
        : hasSttTiming
          ? sttBounds.end ?? origTiming?.end ?? null
          : origTiming?.end ?? sttBounds.end ?? null,
      added: !orig,
    };
  });
  state.draft.forEach((msg, index) => {
    const segmentsForTurn = turnSegments[index]?.length
      ? turnSegments[index]
      : segmentsForTurnWindow(
          msg.start,
          msg.end,
          sttSegments,
          assignSegmentsToTurns([{ role: msg.role }], sttSegments)[0] || [],
          msg.role
        );
    attachWordTimings(msg, segmentsForTurn);
  });
  rebuildPlaybackWordIndex();
}

function syncDraftFromDom() {
  const cards = [...els.transcriptGrid.querySelectorAll(".message-card")];
  cards.forEach((card) => {
    const index = Number(card.dataset.index);
    if (!state.draft[index]) return;
    const roleSelect = card.querySelector(".role-select");
    const textarea = card.querySelector(".final-input");
    const startInput = card.querySelector(".timing-start");
    const endInput = card.querySelector(".timing-end");
    if (roleSelect) state.draft[index].role = roleSelect.value;
    if (textarea) state.draft[index].content = textarea.value;
    if (startInput) state.draft[index].start = parseTimeInput(startInput.value);
    if (endInput) state.draft[index].end = parseTimeInput(endInput.value);
  });
}

function hideSuggestions() {
  els.suggestPopup.classList.add("hidden");
  els.suggestPopup.innerHTML = "";
  els.suggestPopup.classList.remove("translit-popup");
  state.activeSuggestIndex = -1;
  state.suggestKind = "phrase";
}

function positionPopup(textarea) {
  const rect = textarea.getBoundingClientRect();
  els.suggestPopup.classList.remove("hidden");
  els.suggestPopup.style.left = `${Math.min(rect.left, window.innerWidth - 320)}px`;
  els.suggestPopup.style.top = `${rect.bottom + 4}px`;
  state.activeSuggestIndex = 0;
  state.activeTextarea = textarea;
}

function showSuggestions(textarea, suggestions) {
  if (!suggestions.length) {
    hideSuggestions();
    return;
  }

  state.suggestKind = "phrase";
  els.suggestPopup.classList.remove("translit-popup");
  els.suggestPopup.innerHTML = suggestions
    .map(
      (s, i) => `
      <button type="button" class="suggest-item ${i === 0 ? "active" : ""}" data-value="${escapeHtml(s.phrase)}" data-index="${i}">
        <span class="suggest-main">${escapeHtml(s.phrase)}</span>
        <span class="suggest-count">${escapeHtml(s.source || "history")} · ${s.count}</span>
      </button>`
    )
    .join("");

  positionPopup(textarea);
  bindSuggestClicks();
}

function showTransliterationOptions(textarea, latinWord, candidates) {
  const options = [];
  const seen = new Set();
  for (const candidate of candidates) {
    if (candidate && !seen.has(candidate)) {
      seen.add(candidate);
      options.push({ value: candidate, hint: "देवनागरी" });
    }
  }
  // Always allow keeping the Latin word
  if (!seen.has(latinWord)) {
    options.push({ value: latinWord, hint: "keep Latin" });
  }
  if (!options.length) {
    hideSuggestions();
    return;
  }

  state.suggestKind = "translit";
  els.suggestPopup.classList.add("translit-popup");
  els.suggestPopup.innerHTML =
    `<div class="suggest-heading">${escapeHtml(latinWord)} → choose script</div>` +
    options
      .map(
        (opt, i) => `
      <button type="button" class="suggest-item ${i === 0 ? "active" : ""}" data-value="${escapeHtml(opt.value)}" data-index="${i}">
        <span class="suggest-script">${escapeHtml(opt.value)}</span>
        <span class="suggest-count">${escapeHtml(opt.hint)}</span>
      </button>`
      )
      .join("");

  positionPopup(textarea);
  bindSuggestClicks();
}

function bindSuggestClicks() {
  els.suggestPopup.querySelectorAll(".suggest-item").forEach((btn) => {
    btn.onmousedown = (event) => {
      event.preventDefault();
      applyActiveValue(btn.dataset.value);
    };
  });
}

function applyActiveValue(value) {
  if (state.suggestKind === "translit") {
    applyWordReplacement(value);
  } else {
    applySuggestion(value);
  }
}

function replaceWordBeforeCursor(textarea, replacement, { addSpace }) {
  const value = textarea.value;
  const cursor = textarea.selectionStart;
  const before = value.slice(0, cursor);
  const after = value.slice(cursor);
  const match = before.match(/(\S+)$/);
  const start = match ? cursor - match[1].length : cursor;
  const trailing = addSpace && !after.startsWith(" ") ? " " : "";
  textarea.value = value.slice(0, start) + replacement + trailing + after;
  const nextPos = start + replacement.length + trailing.length;
  textarea.setSelectionRange(nextPos, nextPos);
  const index = Number(textarea.dataset.index);
  if (state.draft[index]) state.draft[index].content = textarea.value;
  hideSuggestions();
  textarea.focus();
}

function applySuggestion(phrase) {
  const textarea = state.activeTextarea;
  if (!textarea) return;
  replaceWordBeforeCursor(textarea, phrase, { addSpace: true });
  textarea.dispatchEvent(new Event("input", { bubbles: true }));
}

function applyWordReplacement(word) {
  const textarea = state.activeTextarea;
  if (!textarea) return;
  replaceWordBeforeCursor(textarea, word, { addSpace: false });
}

async function fetchSuggestions(query) {
  if (!query || query.length < 2) {
    hideSuggestions();
    return;
  }
  try {
    const data = await fetchJSON(apiUrl("/api/phrases", { q: query, limit: 8 }));
    if (state.activeTextarea) {
      showSuggestions(state.activeTextarea, data.suggestions || []);
    }
  } catch {
    hideSuggestions();
  }
}

function currentWordBeforeCursor(textarea) {
  const before = textarea.value.slice(0, textarea.selectionStart);
  const match = before.match(/(\S+)$/);
  return match ? match[1] : "";
}

function parseGoogleInputTools(data) {
  const candidates = [];
  if (Array.isArray(data) && data.length >= 2 && data[0] === "SUCCESS") {
    const block = data[1][0];
    if (block && Array.isArray(block[1])) {
      for (const item of block[1]) {
        if (item) candidates.push(String(item));
      }
    }
  }
  return candidates;
}

async function fetchTransliterationNetwork(latin) {
  // Prefer direct Google call (skips Flask hop). Fall back to our API if CORS blocks it.
  const params = new URLSearchParams({
    text: latin,
    itc: "hi-t-i0-und",
    num: "5",
    cp: "0",
    cs: "1",
    ie: "utf-8",
    oe: "utf-8",
    app: "demopage",
  });
  try {
    const res = await fetch(`https://inputtools.google.com/request?${params}`, {
      method: "GET",
      mode: "cors",
    });
    if (res.ok) {
      const candidates = parseGoogleInputTools(await res.json());
      if (candidates.length) return candidates;
    }
  } catch {
    /* fall through to backend proxy */
  }

  const data = await fetchJSON("/api/transliterate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text: latin }),
  });
  const candidates = Array.isArray(data.candidates) ? data.candidates : [];
  if (!candidates.length && data.text) candidates.push(data.text);
  return candidates;
}

function transliterateWord(latin) {
  const key = latin.toLowerCase();
  if (state.translitCache.has(key)) {
    return Promise.resolve(state.translitCache.get(key));
  }
  if (state.translitInflight.has(key)) {
    return state.translitInflight.get(key);
  }

  const pending = fetchTransliterationNetwork(latin)
    .then((candidates) => {
      state.translitCache.set(key, candidates);
      state.translitInflight.delete(key);
      // Cap cache size
      if (state.translitCache.size > 500) {
        const first = state.translitCache.keys().next().value;
        state.translitCache.delete(first);
      }
      return candidates;
    })
    .catch((err) => {
      state.translitInflight.delete(key);
      throw err;
    });

  state.translitInflight.set(key, pending);
  return pending;
}

function prefetchTransliteration(word) {
  if (!latinLikelyHindi(word)) return;
  transliterateWord(word).catch(() => {});
}

function latinLikelyHindi(word) {
  if (!word) return false;
  if (/[\u0900-\u097F]/.test(word)) return false;
  if (!/^[A-Za-z']+$/.test(word)) return false;
  return word.length >= 2;
}

async function handleFinalKeydown(event) {
  const textarea = event.target;
  if (!textarea.classList.contains("final-input")) return;

  const items = [...els.suggestPopup.querySelectorAll(".suggest-item")];
  if (!els.suggestPopup.classList.contains("hidden") && items.length) {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      state.activeSuggestIndex = (state.activeSuggestIndex + 1) % items.length;
      items.forEach((el, i) => el.classList.toggle("active", i === state.activeSuggestIndex));
      return;
    }
    if (event.key === "ArrowUp") {
      event.preventDefault();
      state.activeSuggestIndex =
        (state.activeSuggestIndex - 1 + items.length) % items.length;
      items.forEach((el, i) => el.classList.toggle("active", i === state.activeSuggestIndex));
      return;
    }
    if (event.key === "Enter" && state.activeSuggestIndex >= 0) {
      event.preventDefault();
      applyActiveValue(items[state.activeSuggestIndex].dataset.value);
      return;
    }
    if (event.key === "Escape") {
      hideSuggestions();
      return;
    }
  }

  if (event.key === "Tab") {
    const word = currentWordBeforeCursor(textarea);
    if (!latinLikelyHindi(word)) return;
    event.preventDefault();
    state.activeTextarea = textarea;
    try {
      // Use cache / in-flight prefetch when possible so Tab feels instant
      const candidates = await transliterateWord(word);
      if (!candidates.length) {
        showToast("No Devanagari suggestion found");
        return;
      }
      showTransliterationOptions(textarea, word, candidates);
    } catch (err) {
      showToast(err.message || "Transliteration failed");
    }
  }
}

function realignTimestampsFromStt() {
  const turnSegments = state.turnSegments?.length
    ? state.turnSegments
    : state.currentCall?.timing_segments || [];
  if (!turnSegments.length || !state.draft.length) {
    showToast("No STT timing data for this call");
    return;
  }

  state.draft.forEach((msg, index) => {
    const bounds = boundsFromSegments(turnSegments[index] || []);
    if (bounds.start != null) msg.start = bounds.start;
    if (bounds.end != null) msg.end = bounds.end;
    attachWordTimings(msg, turnSegments[index] || []);
  });

  const duration = getPlaybackDuration();
  if (duration > 0) {
    alignDraftTimingsToAudio(duration);
  }

  rebuildPlaybackWordIndex();
  renderTranscript({ preserveEdits: false });
  syncHighlight();
  showToast("Timestamps re-matched from STT", "success");
}

function refreshTurnWordTimings(index) {
  const msg = state.draft[index];
  if (!msg) return;
  const turnSegments = state.turnSegments?.length
    ? state.turnSegments
    : state.currentCall?.timing_segments || [];
  const segmentsForTurn = turnSegments[index]?.length
    ? turnSegments[index]
    : segmentsForTurnWindow(
        msg.start,
        msg.end,
        state.sttSegments || [],
        assignSegmentsToTurns([{ role: msg.role }], state.sttSegments || [])[0] || [],
        msg.role
      );
  attachWordTimings(msg, segmentsForTurn);
  refreshWordTracksForTurn(index);
  rebuildPlaybackWordIndex();
}

function handleFinalInput(event) {
  const textarea = event.target;
  if (!textarea.classList.contains("final-input")) return;
  state.activeTextarea = textarea;
  const index = Number(textarea.dataset.index);
  if (state.draft[index]) state.draft[index].content = textarea.value;

  clearTimeout(state.wordRefreshTimer);
  state.wordRefreshTimer = setTimeout(() => {
    if (!state.draft[index]) return;
    refreshTurnWordTimings(index);
    if (state.audioSyncEnabled) {
      syncHighlight();
    }
  }, 250);

  const word = currentWordBeforeCursor(textarea);

  clearTimeout(state.phraseTimer);
  state.phraseTimer = setTimeout(() => fetchSuggestions(word.toLowerCase()), 180);

  // Prefetch Devanagari while typing so Tab is ready immediately
  clearTimeout(state.translitTimer);
  state.translitTimer = setTimeout(() => prefetchTransliteration(word), 60);

  updateMeta();
}

function alignDraftTimingsToAudio(duration) {
  if (!Number.isFinite(duration) || duration < 1 || !state.draft.length) return false;

  const points = [];
  state.draft.forEach((msg) => {
    if (Number.isFinite(msg.start)) points.push(msg.start);
    if (Number.isFinite(msg.end)) points.push(msg.end);
  });
  if (points.length < 2) return false;

  const minT = Math.min(...points);
  const maxT = Math.max(...points);
  const span = maxT - minT;
  if (span < 0.5) return false;

  const targetEnd = duration;
  const scale = targetEnd / span;
  const shift = -minT * scale;

  const alreadyAligned =
    Math.abs(scale - 1) < 0.02 && minT < 0.25 && Math.abs(maxT - duration) < 1;
  if (alreadyAligned) return false;

  const scaleSegment = (seg) => {
    const times = segmentTimes(seg);
    if (times.start == null) return seg;
    const start = roundTime(times.start * scale + shift);
    const end = roundTime((times.end ?? times.start) * scale + shift);
    return {
      ...seg,
      start_s: start,
      end_s: end,
      start_time_seconds: start,
      end_time_seconds: end,
    };
  };
  state.sttSegments = (state.sttSegments || []).map(scaleSegment);

  state.draft.forEach((msg, index) => {
    if (Number.isFinite(msg.start)) msg.start = roundTime(msg.start * scale + shift);
    if (Number.isFinite(msg.end)) msg.end = roundTime(msg.end * scale + shift);
    refreshTurnWordTimings(index);
  });
  return true;
}

function destroyWaveform() {
  if (state.highlightTimer) {
    cancelAnimationFrame(state.highlightTimer);
    state.highlightTimer = null;
  }
  if (state.wavesurfer) {
    state.wavesurfer.destroy();
    state.wavesurfer = null;
  }
  if (els.player) {
    els.player.pause();
    els.player.removeAttribute("src");
    els.player.load();
    els.player.classList.add("hidden-audio");
    els.player.ontimeupdate = null;
    els.player.onplay = null;
    els.player.onpause = null;
  }
  state.lastActiveTurn = -1;
  els.waveform.innerHTML = "";
  els.playPauseBtn.textContent = "▶";
  updateTimeDisplays(0, 0);
}

function getPlaybackTime() {
  if (state.wavesurfer) return state.wavesurfer.getCurrentTime() || 0;
  if (els.player && !els.player.classList.contains("hidden-audio")) {
    return els.player.currentTime || 0;
  }
  return 0;
}

function getPlaybackDuration() {
  if (state.wavesurfer) return state.wavesurfer.getDuration() || 0;
  if (els.player && !els.player.classList.contains("hidden-audio")) {
    return els.player.duration || 0;
  }
  return 0;
}

function isPlaybackPlaying() {
  if (state.wavesurfer) return state.wavesurfer.isPlaying();
  if (els.player && !els.player.classList.contains("hidden-audio")) {
    return !els.player.paused && !els.player.ended;
  }
  return false;
}

function scrollActiveTurnIntoView(index) {
  if (!state.audioSyncEnabled || index < 0) return;
  const block = els.transcriptGrid.querySelector(`.turn-block[data-index="${index}"]`);
  if (!block) return;

  // Scroll only inside the workspace so the site header/metrics never leave view.
  const scroller = els.workspace || block.closest(".workspace");
  if (!scroller) return;

  const sticky = scroller.querySelector(".sticky-top");
  const stickyH = sticky ? sticky.offsetHeight : 0;
  const blockTop = block.offsetTop;
  const visible = Math.max(120, scroller.clientHeight - stickyH);
  const target = Math.max(0, blockTop - stickyH - visible * 0.28);
  const maxScroll = Math.max(0, scroller.scrollHeight - scroller.clientHeight);
  scroller.scrollTo({
    top: Math.min(target, maxScroll),
    behavior: "smooth",
  });
}

function syncHighlight() {
  if (state.highlightTimer) {
    cancelAnimationFrame(state.highlightTimer);
    state.highlightTimer = null;
  }

  const t = getPlaybackTime();
  const duration = getPlaybackDuration();
  updateTimeDisplays(t, duration);

  if (!state.audioSyncEnabled) {
    clearActiveWordHighlight();
    els.transcriptGrid?.querySelectorAll(".turn-block").forEach((block) => {
      block.classList.remove("active-turn");
    });
    if (isPlaybackPlaying()) {
      state.highlightTimer = requestAnimationFrame(syncHighlight);
    }
    return;
  }

  const active = updateWordHighlights(t);

  els.transcriptGrid.querySelectorAll(".turn-block").forEach((block) => {
    const index = Number(block.dataset.index);
    block.classList.toggle("active-turn", index === active);
  });

  if (active !== state.lastActiveTurn) {
    state.lastActiveTurn = active;
    scrollActiveTurnIntoView(active);
  }

  if (isPlaybackPlaying()) {
    state.highlightTimer = requestAnimationFrame(syncHighlight);
  }
}

function initWaveform(url) {
  destroyWaveform();
  if (!url) {
    els.waveform.innerHTML = `<div class="waveform-empty">No audio URL for this call</div>`;
    return;
  }

  state.wavesurfer = WaveSurfer.create({
    container: els.waveform,
    url,
    height: 48,
    waveColor: "#cbd5e1",
    progressColor: "#c45c8a",
    cursorColor: "#9d3f6b",
    cursorWidth: 2,
    barWidth: 2,
    barGap: 2,
    barRadius: 2,
    normalize: true,
    interact: true,
  });

  const rate = Number(els.speedSelect.value) || 1;
  state.wavesurfer.setPlaybackRate(rate);
  const volume = Number(els.volumeSlider?.value);
  if (Number.isFinite(volume)) state.wavesurfer.setVolume(volume);

  state.wavesurfer.on("ready", () => {
    const duration = state.wavesurfer.getDuration() || 0;
    updateTimeDisplays(0, duration);
    if (alignDraftTimingsToAudio(duration)) {
      renderTranscript();
    }
    updateMeta();
    syncHighlight();
  });

  state.wavesurfer.on("play", () => {
    els.playPauseBtn.textContent = "⏸";
    syncHighlight();
  });

  state.wavesurfer.on("pause", () => {
    els.playPauseBtn.textContent = "▶";
    syncHighlight();
  });

  state.wavesurfer.on("finish", () => {
    els.playPauseBtn.textContent = "▶";
    syncHighlight();
  });

  state.wavesurfer.on("interaction", () => {
    syncHighlight();
  });

  state.wavesurfer.on("timeupdate", () => {
    syncHighlight();
  });

  state.wavesurfer.on("error", () => {
    if (state.wavesurfer) {
      try {
        state.wavesurfer.destroy();
      } catch (_) {
        /* ignore */
      }
      state.wavesurfer = null;
    }
    els.waveform.innerHTML = `<div class="waveform-empty">Could not load waveform (CORS or expired URL). Using fallback audio player.</div>`;
    els.player.src = url;
    els.player.classList.remove("hidden-audio");
    els.player.playbackRate = rate;
    if (Number.isFinite(volume)) els.player.volume = volume;
    els.player.ontimeupdate = () => syncHighlight();
    els.player.onplay = () => {
      els.playPauseBtn.textContent = "⏸";
      syncHighlight();
    };
    els.player.onpause = () => {
      els.playPauseBtn.textContent = "▶";
      syncHighlight();
    };
    els.player.onloadedmetadata = () => {
      const duration = els.player.duration || 0;
      updateTimeDisplays(0, duration);
      if (alignDraftTimingsToAudio(duration)) {
        renderTranscript();
      }
      syncHighlight();
    };
  });
}

function seekToWord(start) {
  if (!Number.isFinite(start)) return;
  const duration = getPlaybackDuration() || 1;
  const target = Math.min(Math.max(0, start), Math.max(0, duration - 0.05));
  if (state.wavesurfer) {
    state.wavesurfer.seekTo(Math.min(0.999, target / duration));
    if (!state.wavesurfer.isPlaying()) state.wavesurfer.play();
  } else if (els.player && !els.player.classList.contains("hidden-audio")) {
    els.player.currentTime = target;
    if (els.player.paused) els.player.play().catch(() => {});
  } else {
    return;
  }
  syncHighlight();
}

function seekToTurn(index) {
  const msg = state.draft[index];
  if (!msg || msg.start == null) return;
  const duration = getPlaybackDuration() || 1;
  const target = Math.min(Math.max(0, msg.start), Math.max(0, duration - 0.05));

  if (state.wavesurfer) {
    state.wavesurfer.seekTo(Math.min(0.999, target / duration));
    if (!state.wavesurfer.isPlaying()) state.wavesurfer.play();
  } else if (els.player && !els.player.classList.contains("hidden-audio")) {
    els.player.currentTime = target;
    if (els.player.paused) els.player.play().catch(() => {});
  } else {
    return;
  }
  syncHighlight();
}

function renderTranscript({ preserveEdits = true } = {}) {
  // Keep in-progress Final edits when re-rendering the same call (e.g. audio ready).
  // Skip when draft was just rebuilt from the server (select / reset / save).
  if (preserveEdits) {
    syncDraftFromDom();
  }
  hideSuggestions();

  els.transcriptGrid.innerHTML = state.draft
      .map((msg, index) => {
        const originalContent = msg.originalContent?.trim()
          ? msg.originalContent
          : "—";
        const sttContent = msg.sttContent?.trim()
          ? msg.sttContent
          : msg.added
            ? "—"
            : "Not generated yet";
        const startValue = formatTimeInput(msg.start);
        const endValue = formatTimeInput(msg.end);
        const avatarLetter = msg.role === "user" ? "U" : "A";
        return `
        <div class="turn-block" data-index="${index}">
          <article class="message-card" data-index="${index}">
            <div class="message-header">
              <div class="message-speaker">
                <span class="avatar avatar-md ${msg.role}" aria-hidden="true">${avatarLetter}</span>
                <label class="role-label">
                  <span class="sr-only">Role</span>
                  <select class="role-select ${msg.role}" data-index="${index}">
                    <option value="assistant" ${msg.role === "assistant" ? "selected" : ""}>ASSISTANT</option>
                    <option value="user" ${msg.role === "user" ? "selected" : ""}>USER</option>
                  </select>
                </label>
              </div>
              <div class="message-header-actions">
                <div class="timing-editor" title="Edit turn timestamps (m:ss.sss)">
                  <label class="sr-only" for="timing-start-${index}">Start time</label>
                  <input
                    id="timing-start-${index}"
                    type="text"
                    class="timing-input timing-start"
                    data-index="${index}"
                    value="${escapeHtml(startValue)}"
                    placeholder="0:00.000"
                    inputmode="decimal"
                    aria-label="Start time"
                  />
                  <span class="timing-sep">–</span>
                  <label class="sr-only" for="timing-end-${index}">End time</label>
                  <input
                    id="timing-end-${index}"
                    type="text"
                    class="timing-input timing-end"
                    data-index="${index}"
                    value="${escapeHtml(endValue)}"
                    placeholder="end (m:ss.sss)"
                    inputmode="decimal"
                    aria-label="End time"
                  />
                  <button type="button" class="timing-action timing-use-playhead" data-index="${index}" title="Set start to current playback time" aria-label="Set start to playhead">⏱</button>
                  <button type="button" class="timing-action timing-seek" data-seek="${index}" title="Seek audio to start time" aria-label="Seek audio to turn">▶</button>
                </div>
                <span class="message-type">${msg.added ? "added turn" : "transcript"}</span>
                <button type="button" class="icon-action collapse-turn-btn" data-index="${index}" title="Collapse turn" aria-label="Collapse turn">
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M6 9l6 6 6-6"/></svg>
                </button>
                <button type="button" class="icon-action danger delete-turn-btn" data-index="${index}" title="Delete this turn" aria-label="Delete turn" ${state.draft.length <= 1 ? "disabled" : ""}>
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6"/></svg>
                </button>
              </div>
            </div>
            <div class="message-body">
              <div class="col-card original">
                <div class="column-label">Original</div>
                ${renderSyncedColumn(originalContent, msg.originalWordTimings, "original", index)}
              </div>
              <div class="col-card sarvam">
                <div class="column-label">Sarvam</div>
                ${renderSyncedColumn(sttContent, msg.sttWordTimings, "sarvam", index)}
              </div>
              <div class="col-card final final-col">
                <div class="column-label">
                  <span>Final</span>
                  <span class="char-count" data-index="${index}">${(msg.content || "").length} chars</span>
                </div>
                <div class="final-word-host">${renderWordTrackHtml(msg.wordTimings, "final", index)}</div>
                <textarea class="final-input" data-index="${index}" rows="4" placeholder="Edit the final transcript…">${escapeHtml(msg.content)}</textarea>
              </div>
            </div>
          </article>
          <button type="button" class="add-turn-btn" data-after="${index}" title="Add turn after this">+</button>
        </div>
      `;
      })
      .join("");

  els.transcriptGrid.querySelectorAll(".role-select").forEach((select) => {
    select.onchange = () => {
      const index = Number(select.dataset.index);
      state.draft[index].role = select.value;
      select.classList.remove("user", "assistant");
      select.classList.add(select.value);
      const avatar = select.closest(".message-speaker")?.querySelector(".avatar");
      if (avatar) {
        avatar.classList.remove("user", "assistant");
        avatar.classList.add(select.value);
        avatar.textContent = select.value === "user" ? "U" : "A";
      }
      updateMeta();
    };
  });

  els.transcriptGrid.querySelectorAll(".delete-turn-btn").forEach((btn) => {
    btn.onclick = () => {
      if (state.draft.length <= 1) {
        showToast("Keep at least one turn");
        return;
      }
      syncDraftFromDom();
      state.draft.splice(Number(btn.dataset.index), 1);
      renderTranscript({ preserveEdits: false });
      updateMeta();
    };
  });

  els.transcriptGrid.querySelectorAll(".collapse-turn-btn").forEach((btn) => {
    btn.onclick = () => {
      const block = btn.closest(".turn-block");
      if (!block) return;
      const collapsed = block.classList.toggle("collapsed");
      btn.setAttribute("aria-label", collapsed ? "Expand turn" : "Collapse turn");
      btn.title = collapsed ? "Expand turn" : "Collapse turn";
      btn.innerHTML = collapsed
        ? `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M6 15l6-6 6 6"/></svg>`
        : `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M6 9l6 6 6-6"/></svg>`;
    };
  });

  els.transcriptGrid.querySelectorAll(".add-turn-btn").forEach((btn) => {
    btn.onclick = () => {
      syncDraftFromDom();
      const after = Number(btn.dataset.after);
      const prev = state.draft[after];
      const { start, end } = defaultTimingForInsertedTurn(after);
      state.draft.splice(after + 1, 0, {
        _id: `added-${Date.now()}-${after + 1}`,
        role: prev?.role === "assistant" ? "user" : "assistant",
        type: "message",
        createdAt: "",
        content: "",
        originalContent: "",
        sttContent: "",
        start,
        end,
        added: true,
      });
      renderTranscript({ preserveEdits: false });
      updateMeta();
    };
  });

  els.transcriptGrid.querySelectorAll(".timing-use-playhead").forEach((btn) => {
    btn.onclick = () => {
      const index = Number(btn.dataset.index);
      const playhead = getPlaybackTime();
      if (!Number.isFinite(playhead) || playhead < 0) {
        showToast("Play audio first to capture the current time");
        return;
      }
      const start = roundTime(playhead);
      const duration = getPlaybackDuration();
      const end = roundTime(Math.min(duration || start + 2, start + 2));
      state.draft[index].start = start;
      state.draft[index].end = end > start ? end : roundTime(start + 0.5);
      refreshTurnWordTimings(index);
      renderTranscript({ preserveEdits: false });
      updateMeta();
    };
  });

  els.transcriptGrid.querySelectorAll(".timing-seek").forEach((btn) => {
    btn.onclick = () => seekToTurn(Number(btn.dataset.seek));
  });

  els.transcriptGrid.querySelectorAll(".timing-input").forEach((input) => {
    input.addEventListener("change", () => {
      const index = Number(input.dataset.index);
      if (!state.draft[index]) return;
      if (input.classList.contains("timing-start")) {
        state.draft[index].start = parseTimeInput(input.value);
      } else {
        state.draft[index].end = parseTimeInput(input.value);
      }
      refreshTurnWordTimings(index);
      updateMeta();
    });
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        input.blur();
      }
    });
  });

  els.transcriptGrid.querySelectorAll(".final-input").forEach((textarea) => {
    autoResizeTextarea(textarea);
    updateCharCount(textarea);
    textarea.addEventListener("keydown", handleFinalKeydown);
    textarea.addEventListener("input", (event) => {
      handleFinalInput(event);
      autoResizeTextarea(textarea);
      updateCharCount(textarea);
    });
    textarea.addEventListener("blur", () => {
      setTimeout(hideSuggestions, 150);
    });
  });

  // Re-measure after layout so long Final text isn't clipped.
  requestAnimationFrame(() => {
    els.transcriptGrid.querySelectorAll(".final-input").forEach(autoResizeTextarea);
    syncHighlight();
  });
}

function collectFinalMessages() {
  syncDraftFromDom();
  return state.draft.map((msg) => ({
    _id: msg._id,
    role: msg.role,
    type: "message",
    createdAt: msg.createdAt || "",
    content: msg.content,
  }));
}

function collectTimings() {
  syncDraftFromDom();
  return state.draft.map((msg) => ({
    start:
      msg.start != null && !Number.isNaN(msg.start) ? roundTime(Number(msg.start)) : null,
    end: msg.end != null && !Number.isNaN(msg.end) ? roundTime(Number(msg.end)) : null,
  }));
}

function snapshotDraftState() {
  return JSON.stringify({
    messages: collectFinalMessages(),
    timings: collectTimings(),
  });
}

function refreshSavedSnapshot() {
  state.savedSnapshot = snapshotDraftState();
}

function isDraftDirty() {
  if (!state.currentCall) return false;
  return snapshotDraftState() !== state.savedSnapshot;
}

function saveStatusInfo() {
  const call = state.currentCall;
  if (!call) {
    return { label: "", className: "", dirty: false, persisted: false };
  }

  const dirty = isDraftDirty();
  const persisted = Boolean(
    call.edited ||
      call.status === "edited" ||
      call.status === "verified_once" ||
      call.status === "verified" ||
      call.status === "unfit"
  );

  if (call.status === "unfit" && !dirty) {
    return { label: "Marked unfit", className: "unfit", dirty: false, persisted: true };
  }
  if (call.status === "verified" && !dirty) {
    return { label: "Fully verified", className: "verified", dirty: false, persisted: true };
  }
  if (call.status === "verified_once" && !dirty) {
    return { label: "Verified once · awaiting 2nd", className: "verified-once", dirty: false, persisted: true };
  }
  if (dirty) {
    return {
      label: persisted ? "Unsaved changes" : "Final not saved",
      className: persisted ? "unsaved" : "not-saved",
      dirty: true,
      persisted,
    };
  }
  if (call.status === "edited") {
    return {
      label: "Final saved · awaiting verify",
      className: "saved",
      dirty: false,
      persisted: true,
    };
  }
  return { label: "Final not saved", className: "not-saved", dirty: false, persisted: false };
}

function updateSaveStatus() {
  const call = state.currentCall;
  if (!call) {
    els.saveStatusBadge.classList.add("hidden");
    return;
  }

  const info = saveStatusInfo();
  els.saveStatusBadge.textContent = info.label;
  els.saveStatusBadge.className = `badge-status ${info.className}`;
  els.saveStatusBadge.classList.remove("hidden");

  if (info.dirty) {
    els.saveBtn.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><path d="M17 21v-8H7v8M7 3v5h8"/></svg> Save changes`;
    els.saveBtn.classList.add("needs-save");
  } else if (info.persisted) {
    els.saveBtn.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M20 6L9 17l-5-5"/></svg> Final saved`;
    els.saveBtn.classList.remove("needs-save");
  } else {
    els.saveBtn.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><path d="M17 21v-8H7v8M7 3v5h8"/></svg> Save final`;
    els.saveBtn.classList.remove("needs-save");
  }
}

function updateMeta() {
  const call = state.currentCall;
  if (!call) return;
  const info = saveStatusInfo();
  const duration =
    state.wavesurfer?.getDuration?.() ||
    (state.draft.length
      ? Math.max(
          ...state.draft.map((m) => (m.end != null ? Number(m.end) : 0)),
          0
        )
      : 0);
  const providers = [];
  if (call.hasStt) providers.push("Sarvam");

  const chips = [
    `<span class="chip"><strong>${formatTime(duration)}</strong> duration</span>`,
    `<span class="chip"><strong>${state.draft.length}</strong> turns</span>`,
    `<span class="chip"><strong>${escapeHtml(detectLanguageLabel())}</strong> language</span>`,
    `<span class="chip"><strong>${call.editedBy || state.currentUser || "—"}</strong> agent</span>`,
    `<span class="chip"><strong>${providers.length ? providers.join(" · ") : "None"}</strong> provider</span>`,
    `<span class="chip"><strong>${info.label}</strong></span>`,
  ];

  if (call.editedBy) {
    chips.push(`<span class="chip muted">edited by ${escapeHtml(call.editedBy)}</span>`);
  }
  if (call.verifiedOnceBy) {
    chips.push(`<span class="chip muted">verified once by ${escapeHtml(call.verifiedOnceBy)}</span>`);
  }
  if (call.verifiedBy) {
    chips.push(`<span class="chip muted">verified by ${escapeHtml(call.verifiedBy)}</span>`);
  }
  if (call.status === "unfit" && call.unfitBy) {
    chips.push(`<span class="chip muted">unfit by ${escapeHtml(call.unfitBy)}</span>`);
  }
  if (call.unfitReason) {
    chips.push(`<span class="chip muted">reason: ${escapeHtml(call.unfitReason)}</span>`);
  }
  if (call.updatedAt && info.persisted && !info.dirty) {
    chips.push(
      `<span class="chip muted">saved ${escapeHtml(new Date(call.updatedAt).toLocaleString())}</span>`
    );
  } else if (info.dirty && info.persisted) {
    chips.push(`<span class="chip muted">you have unsaved edits</span>`);
  }

  els.callMeta.innerHTML = chips.join("");
  const status = call.status;
  const isFullyVerified = status === "verified";
  const isVerifiedOnce = status === "verified_once";
  const isSaver =
    Boolean(call.editedBy) &&
    call.editedBy.trim().toLowerCase() === getReviewer().trim().toLowerCase();
  const isFirstVerifier =
    Boolean(call.verifiedOnceBy) &&
    call.verifiedOnceBy.trim().toLowerCase() === getReviewer().trim().toLowerCase();
  const canVerifyOnce = status === "edited" && !info.dirty && !isSaver;
  const canVerifyFinal = isVerifiedOnce && !info.dirty && !isSaver && !isFirstVerifier;
  const canUnverify = (isFullyVerified || isVerifiedOnce) && !info.dirty && !isSaver;
  els.verifyBtn.disabled = !(canVerifyOnce || canVerifyFinal || canUnverify);
  els.verifyBtn.title = isSaver
    ? "Another reviewer must verify or unverify this save"
    : isFullyVerified
      ? "Remove second verification"
      : isVerifiedOnce
        ? isFirstVerifier
          ? "A different reviewer must complete the second verification"
          : "Complete second verification"
        : "Add first verification";
  if (isFullyVerified) {
    els.verifyBtn.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M18 6L6 18M6 6l12 12"/></svg> Unverify`;
  } else if (isVerifiedOnce) {
    els.verifyBtn.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M20 6L9 17l-5-5"/></svg> Verify final`;
  } else {
    els.verifyBtn.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M20 6L9 17l-5-5"/></svg> Verify once`;
  }
  els.verifyBtn.classList.toggle("secondary", isFullyVerified || isVerifiedOnce);
  els.verifyBtn.classList.toggle("verify", !isFullyVerified && !isVerifiedOnce);
  if (els.unfitBtn) {
    const isUnfit = call.status === "unfit";
    els.unfitBtn.textContent = isUnfit ? "Clear unfit" : "Mark unfit";
    els.unfitBtn.classList.toggle("danger", !isUnfit);
    els.unfitBtn.classList.toggle("secondary", isUnfit);
  }
  updateSaveStatus();
}

function updateNavButtons() {
  const call = state.currentCall;
  els.prevCallBtn.disabled = !call?.prevId;
  els.nextCallBtn.disabled = !call?.nextId;
}

async function selectCall(callId) {
  state.selectedId = callId;
  // Refresh list selection styling without a full skeleton reload.
  els.callList.querySelectorAll(".call-item").forEach((row) => {
    row.classList.toggle("active", row.dataset.id === callId);
  });

  const call = await fetchJSON(apiUrl(`/api/calls/${callId}`));
  state.currentCall = call;
  buildDraft(call);

  els.emptyState.classList.add("hidden");
  els.callDetail.classList.remove("hidden");
  els.callId.textContent =
    call.number != null ? `#${call.number} · ${call.id}` : call.id;
  els.player.classList.add("hidden-audio");
  els.resetBtn.textContent = "Reset to original";

  initWaveform(call.public_url || "");
  loadLabelDraftFromCall(call);
  renderLabelPanel();
  renderTranscript({ preserveEdits: false });
  refreshSavedSnapshot();
  updateMeta();
  updateNavButtons();
  // Keep top chrome visible — reset workspace scroll when opening a recording.
  if (els.workspace) {
    els.workspace.scrollTo({ top: 0, behavior: "auto" });
  }
  window.scrollTo({ top: 0, behavior: "auto" });
}

async function saveFinal() {
  if (!state.currentCall) return;
  if (!getReviewer()) {
    showToast("Please log in again");
    window.location.href = "/login";
    return;
  }
  try {
    const messages = collectFinalMessages();
    const timings = collectTimings();
    const result = await fetchJSON(apiUrl(`/api/calls/${state.currentCall.id}/correct`), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ messages, timings }),
    });
    state.currentCall.edited = true;
    state.currentCall.status = "edited";
    state.currentCall.updatedAt = result.updatedAt;
    state.currentCall.editedBy = result.editedBy;
    state.currentCall.verifiedOnceBy = "";
    state.currentCall.verifiedOnceAt = null;
    state.currentCall.verifiedBy = "";
    state.currentCall.verifiedAt = null;
    state.currentCall.unfitBy = "";
    state.currentCall.unfitAt = null;
    state.currentCall.unfitReason = "";
    state.currentCall.final_messages = result.messages || messages;
    state.currentCall.timings = timings;
    buildDraft(state.currentCall);
    renderTranscript({ preserveEdits: false });
    refreshSavedSnapshot();
    updateMeta();
    // Don't block the UI on list/stats refresh after save.
    loadStats().catch(() => {});
    loadCalls().catch(() => {});
    showToast(`Final saved by ${result.editedBy}`, "success");
  } catch (err) {
    showToast(err.message, "error");
  }
}

async function verifyFinal() {
  if (!state.currentCall) return;
  if (!getReviewer()) {
    showToast("Please log in again");
    window.location.href = "/login";
    return;
  }
  const clearing = state.currentCall.status === "verified" || state.currentCall.status === "verified_once";
  try {
    const result = await fetchJSON(apiUrl(`/api/calls/${state.currentCall.id}/verify`), {
      method: clearing ? "DELETE" : "POST",
      headers: { "Content-Type": "application/json" },
      body: clearing ? undefined : JSON.stringify({}),
    });
    state.currentCall.status = result.status || state.currentCall.status;
    state.currentCall.verifiedOnceBy = result.verifiedOnceBy || "";
    state.currentCall.verifiedOnceAt = result.verifiedOnceAt || null;
    state.currentCall.verifiedBy = result.verifiedBy || "";
    state.currentCall.verifiedAt = result.verifiedAt || null;
    updateMeta();
    await loadStats();
    await loadCalls();
    const toastLabel =
      result.status === "verified"
        ? `Fully verified by ${result.verifiedBy}`
        : result.status === "verified_once"
          ? `Verified once by ${result.verifiedOnceBy}`
          : clearing
            ? "Verification stepped back"
            : "Verified";
    showToast(toastLabel, "success");
  } catch (err) {
    showToast(err.message, "error");
  }
}

async function toggleUnfit() {
  if (!state.currentCall) return;
  if (!getReviewer()) {
    showToast("Please log in again");
    window.location.href = "/login";
    return;
  }

  const clearing = state.currentCall.status === "unfit";
  if (!clearing) {
    const ok = window.confirm(
      "Mark this call as unfit for the golden set? It will be excluded from verified export."
    );
    if (!ok) return;
  }

  try {
    const result = await fetchJSON(apiUrl(`/api/calls/${state.currentCall.id}/unfit`), {
      method: clearing ? "DELETE" : "POST",
      headers: { "Content-Type": "application/json" },
      body: clearing ? undefined : JSON.stringify({}),
    });
    state.currentCall.status = result.status || (clearing ? "pending" : "unfit");
    state.currentCall.unfitBy = result.unfitBy || "";
    state.currentCall.unfitAt = result.unfitAt || null;
    state.currentCall.unfitReason = result.unfitReason || "";
    if (clearing) {
      state.currentCall.edited = result.status === "edited" || result.status === "verified_once" || result.status === "verified";
    } else {
      state.currentCall.edited = true;
      state.currentCall.verifiedOnceBy = "";
      state.currentCall.verifiedOnceAt = null;
      state.currentCall.verifiedBy = "";
      state.currentCall.verifiedAt = null;
    }
    updateMeta();
    await loadStats();
    await loadCalls();
    showToast(clearing ? "Unfit cleared" : "Marked as unfit", "success");
  } catch (err) {
    showToast(err.message, "error");
  }
}

async function resetFinal() {
  if (!state.currentCall) return;
  if (!confirm("Reset final transcript to original for this call?")) return;
  try {
    const result = await fetchJSON(
      apiUrl(`/api/calls/${state.currentCall.id}/correct`),
      { method: "DELETE" }
    );
    state.currentCall.final_messages = result.final_messages;
    state.currentCall.edited = false;
    state.currentCall.status = "pending";
    state.currentCall.updatedAt = null;
    state.currentCall.editedBy = "";
    state.currentCall.verifiedOnceBy = "";
    state.currentCall.verifiedOnceAt = null;
    state.currentCall.verifiedBy = "";
    state.currentCall.verifiedAt = null;
    buildDraft(state.currentCall);
    renderTranscript({ preserveEdits: false });
    refreshSavedSnapshot();
    updateMeta();
    loadStats().catch(() => {});
    loadCalls().catch(() => {});
    showToast("Reset to original");
  } catch (err) {
    showToast(err.message);
  }
}

async function switchDataset(dataset) {
  if (dataset === state.dataset) return;
  state.dataset = dataset;
  state.page = 1;
  state.search = "";
  state.status = "all";
  state.domain = "";
  state.subdomain = "";
  state.labelStatus = "all";
  state.sort = "number";
  state.selectedId = null;
  state.selectedCallIds = new Set();
  state.currentCall = null;
  state.draft = [];
  els.search.value = "";
  els.statusFilter.value = "all";
  if (els.domainFilter) els.domainFilter.value = "";
  if (els.subdomainFilter) els.subdomainFilter.value = "";
  if (els.labelStatusFilter) els.labelStatusFilter.value = "all";
  if (els.sortSelect) els.sortSelect.value = "number";
  els.emptyState.classList.remove("hidden");
  els.callDetail.classList.add("hidden");
  destroyWaveform();
  updateDatasetChrome();
  await loadLabelSuggestions();
  await loadStats();
  await loadCalls();
}

async function handleUpload(file) {
  if (!file) return;
  const form = new FormData();
  form.append("file", file);
  try {
    els.uploadHint.textContent = "Uploading…";
    const res = await fetch(apiUrl("/api/upload"), { method: "POST", body: form });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Upload failed");
    showToast(`Imported ${data.imported} calls into ${DATASET_LABELS[state.dataset]}`);
    await loadStats();
    await loadCalls();
    updateDatasetChrome();
    if (
      state.canManageSarvamStt &&
      confirm(`Start Sarvam STT for ${data.imported} uploaded calls?`)
    ) {
      await startSarvamStt();
    }
  } catch (err) {
    showToast(err.message);
  } finally {
    els.uploadInput.value = "";
    updateDatasetChrome();
  }
}

async function startSarvamStt() {
  try {
    updateSttButton(true);
    const result = await fetchJSON(apiUrl("/api/stt/start"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ resume: true, workers: 3 }),
    });
    showToast(
      `Sarvam STT started · ${result.pending} pending · ${result.skipped} skipped`
    );
    await loadStats();
  } catch (err) {
    updateSttButton(false);
    showToast(err.message);
  }
}

async function stopSarvamStt() {
  try {
    const result = await fetchJSON(apiUrl("/api/stt/stop"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    showToast(result.wasStale ? "Cleared stale Sarvam STT status" : "Sarvam STT stopped");
    await loadStats();
  } catch (err) {
    showToast(err.message);
  }
}

async function buildExportPayload() {
  const scope =
    document.querySelector('input[name="exportScope"]:checked')?.value || "selected";
  const status = els.exportStatusFilter?.value || "all";
  const payload = {
    status,
    search: "",
    domain: "",
    subdomain: "",
    label_status: "all",
    call_ids: null,
  };
  if (scope === "selected") {
    payload.call_ids = [...state.selectedCallIds];
  } else if (scope === "filtered") {
    payload.search = state.search;
    payload.domain = state.domain;
    payload.subdomain = state.subdomain;
    payload.label_status = state.labelStatus;
  }
  return payload;
}

async function refreshExportCounts() {
  if (!els.exportModal) return;
  const status = els.exportStatusFilter?.value || "all";
  updateSelectionChrome();

  const filteredPayload = {
    status,
    search: state.search,
    domain: state.domain,
    subdomain: state.subdomain,
    label_status: state.labelStatus,
  };
  const allPayload = {
    status,
    search: "",
    domain: "",
    subdomain: "",
    label_status: "all",
  };
  try {
    const [filtered, all] = await Promise.all([
      fetchJSON(apiUrl("/api/export/preview"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(filteredPayload),
      }),
      fetchJSON(apiUrl("/api/export/preview"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(allPayload),
      }),
    ]);
    if (els.exportFilteredCount) {
      els.exportFilteredCount.textContent = String(filtered.count || 0);
    }
    if (els.exportAllCount) {
      els.exportAllCount.textContent = String(all.count || 0);
    }
  } catch {
    if (els.exportFilteredCount) els.exportFilteredCount.textContent = "0";
    if (els.exportAllCount) els.exportAllCount.textContent = "0";
  }
}

function openExportModal() {
  if (!els.exportModal) return;
  if (els.exportStatusFilter) {
    els.exportStatusFilter.value = state.status === "all" ? "all" : state.status;
  }
  const selectedRadio = document.querySelector('input[name="exportScope"][value="selected"]');
  const filteredRadio = document.querySelector('input[name="exportScope"][value="filtered"]');
  if (state.selectedCallIds.size > 0 && selectedRadio) {
    selectedRadio.checked = true;
  } else if (filteredRadio) {
    filteredRadio.checked = true;
  }
  els.exportModal.classList.remove("hidden");
  refreshExportCounts();
}

function closeExportModal() {
  els.exportModal?.classList.add("hidden");
}

async function exportTranscripts() {
  try {
    const payload = await buildExportPayload();
    const scope =
      document.querySelector('input[name="exportScope"]:checked')?.value || "selected";
    if (scope === "selected" && (!payload.call_ids || !payload.call_ids.length)) {
      throw new Error("Select at least one call to export");
    }
    const res = await fetch(apiUrl("/api/export"), {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      throw new Error(data.error || "Export failed");
    }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    const statusSlug = (payload.status || "export").replace(/[^a-z0-9_]+/gi, "_");
    a.download = `${state.dataset}_${statusSlug}_transcripts.json`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    closeExportModal();
    showToast("Downloaded transcripts", "success");
  } catch (err) {
    showToast(err.message, "error");
  }
}

els.datasetTabs.querySelectorAll(".dataset-tab").forEach((btn) => {
  btn.addEventListener("click", () => switchDataset(btn.dataset.dataset));
});

els.search.addEventListener("input", () => {
  clearTimeout(state.searchTimer);
  state.searchTimer = setTimeout(() => {
    state.search = els.search.value.trim();
    state.page = 1;
    loadCalls();
  }, 250);
});

els.statusFilter.addEventListener("change", () => {
  state.status = els.statusFilter.value;
  state.page = 1;
  loadCalls();
});

if (els.sortSelect) {
  els.sortSelect.addEventListener("change", () => {
    state.sort = els.sortSelect.value;
    loadCalls();
  });
}

if (els.volumeSlider) {
  els.volumeSlider.addEventListener("input", () => {
    const volume = Number(els.volumeSlider.value);
    if (state.wavesurfer && Number.isFinite(volume)) {
      state.wavesurfer.setVolume(volume);
    }
    if (Number.isFinite(volume)) {
      els.player.volume = volume;
    }
  });
}

if (els.settingsBtn && els.settingsMenu) {
  els.settingsBtn.addEventListener("click", (event) => {
    event.stopPropagation();
    const open = els.settingsMenu.classList.toggle("hidden") === false;
    els.settingsBtn.setAttribute("aria-expanded", open ? "true" : "false");
  });
  document.addEventListener("click", (event) => {
    if (
      !els.settingsMenu.classList.contains("hidden") &&
      !els.settingsMenu.contains(event.target) &&
      event.target !== els.settingsBtn
    ) {
      els.settingsMenu.classList.add("hidden");
      els.settingsBtn.setAttribute("aria-expanded", "false");
    }
  });
}

if (els.notifBtn) {
  els.notifBtn.addEventListener("click", () => {
    const pending = state.lastStats?.pending ?? 0;
    showToast(
      pending > 0
        ? `${pending} conversation${pending === 1 ? "" : "s"} still pending review`
        : "No pending conversations"
    );
    if (pending > 0) {
      els.statusFilter.value = "pending";
      state.status = "pending";
      state.page = 1;
      loadCalls();
    }
  });
}

els.prevCallBtn.addEventListener("click", () => {
  if (state.currentCall?.prevId) selectCall(state.currentCall.prevId);
});

els.nextCallBtn.addEventListener("click", () => {
  if (state.currentCall?.nextId) selectCall(state.currentCall.nextId);
});

els.saveBtn.addEventListener("click", saveFinal);
els.resetBtn.addEventListener("click", resetFinal);
els.verifyBtn.addEventListener("click", verifyFinal);
if (els.unfitBtn) els.unfitBtn.addEventListener("click", toggleUnfit);

els.playPauseBtn.addEventListener("click", () => {
  if (state.wavesurfer) {
    state.wavesurfer.playPause();
    return;
  }
  if (els.player && !els.player.classList.contains("hidden-audio")) {
    if (els.player.paused) els.player.play().catch(() => {});
    else els.player.pause();
  }
});

function isEditingText(target) {
  if (!target) return false;
  const tag = target.tagName;
  return (
    tag === "INPUT" ||
    tag === "TEXTAREA" ||
    tag === "SELECT" ||
    target.isContentEditable
  );
}

function seekAudioBy(seconds) {
  const duration = getPlaybackDuration() || 0;
  if (!duration) return;
  const next = Math.min(
    Math.max(0, getPlaybackTime() + seconds),
    Math.max(0, duration - 0.05)
  );
  if (state.wavesurfer) {
    state.wavesurfer.seekTo(next / duration);
  } else if (els.player && !els.player.classList.contains("hidden-audio")) {
    els.player.currentTime = next;
  } else {
    return;
  }
  syncHighlight();
}

els.transcriptGrid.addEventListener("click", (event) => {
  const wordBtn = event.target.closest(".sync-word");
  if (!wordBtn || !els.transcriptGrid.contains(wordBtn)) return;
  event.preventDefault();
  seekToWord(Number(wordBtn.dataset.start));
});

els.speedSelect.addEventListener("change", () => {
  const rate = Number(els.speedSelect.value) || 1;
  if (state.wavesurfer) state.wavesurfer.setPlaybackRate(rate);
  els.player.playbackRate = rate;
});

if (els.audioSyncToggle) {
  els.audioSyncToggle.addEventListener("click", () => {
    setAudioSyncEnabled(!state.audioSyncEnabled);
  });
}
if (els.realignTimestampsBtn) {
  els.realignTimestampsBtn.addEventListener("click", realignTimestampsFromStt);
}
updateAudioSyncChrome();

els.uploadInput.addEventListener("change", () => {
  const file = els.uploadInput.files?.[0];
  handleUpload(file);
});

if (els.startSttBtn) {
  els.startSttBtn.addEventListener("click", () => {
    if (sttRunning) stopSarvamStt();
    else startSarvamStt();
  });
}
if (els.startLabelBtn) {
  els.startLabelBtn.addEventListener("click", () => {
    if (labelRunning) stopAutoLabeling();
    else startAutoLabeling();
  });
}

if (els.labelDomainInput) {
  els.labelDomainInput.addEventListener("input", () => {
    state.labelDraft.dirty = true;
    state.labelDraft.domain = els.labelDomainInput.value;
    renderLabelSuggestionLists();
  });
}
if (els.labelSubdomainInput) {
  els.labelSubdomainInput.addEventListener("input", () => {
    state.labelDraft.dirty = true;
    state.labelDraft.subdomain = els.labelSubdomainInput.value;
  });
}
els.saveLabelBtn?.addEventListener("click", saveLabel);
els.resetLabelBtn?.addEventListener("click", resetLabelToAuto);
els.clearLabelBtn?.addEventListener("click", clearLabel);
els.rerunLabelBtn?.addEventListener("click", rerunAutoLabel);

if (els.domainFilter) {
  els.domainFilter.addEventListener("change", () => {
    state.domain = els.domainFilter.value;
    state.subdomain = "";
    state.page = 1;
    populateSubdomainFilter();
    loadCalls();
  });
}
if (els.subdomainFilter) {
  els.subdomainFilter.addEventListener("change", () => {
    state.subdomain = els.subdomainFilter.value;
    state.page = 1;
    loadCalls();
  });
}
if (els.labelStatusFilter) {
  els.labelStatusFilter.addEventListener("change", () => {
    state.labelStatus = els.labelStatusFilter.value;
    state.page = 1;
    loadCalls();
  });
}

els.exportDataBtn?.addEventListener("click", openExportModal);
els.closeExportModal?.addEventListener("click", closeExportModal);
els.cancelExportBtn?.addEventListener("click", closeExportModal);
els.confirmExportBtn?.addEventListener("click", exportTranscripts);
els.exportStatusFilter?.addEventListener("change", refreshExportCounts);
document.querySelectorAll('input[name="exportScope"]').forEach((input) => {
  input.addEventListener("change", refreshExportCounts);
});
els.exportModal?.addEventListener("click", (event) => {
  if (event.target === els.exportModal) closeExportModal();
});
if (els.selectAllPage) {
  els.selectAllPage.addEventListener("change", () => {
    const ids = state.lastCallPageIds || [];
    if (els.selectAllPage.checked) {
      ids.forEach((id) => state.selectedCallIds.add(id));
    } else {
      ids.forEach((id) => state.selectedCallIds.delete(id));
    }
    loadCalls();
  });
}

document.addEventListener("keydown", (event) => {
  if ((event.metaKey || event.ctrlKey) && event.key === "s") {
    event.preventDefault();
    saveFinal();
    return;
  }

  const hasAudio =
    state.wavesurfer ||
    (els.player && !els.player.classList.contains("hidden-audio") && els.player.src);
  if (!hasAudio) return;
  if (event.metaKey || event.ctrlKey || event.altKey) return;

  if (event.code === "Space" || event.key === " ") {
    // Allow normal spaces while typing in the final editor / inputs
    if (isEditingText(event.target)) return;
    event.preventDefault();
    if (state.wavesurfer) {
      state.wavesurfer.playPause();
    } else if (els.player.paused) {
      els.player.play().catch(() => {});
    } else {
      els.player.pause();
    }
    return;
  }

  if (event.key === "ArrowLeft") {
    event.preventDefault();
    seekAudioBy(-10);
    return;
  }

  if (event.key === "ArrowRight") {
    event.preventDefault();
    seekAudioBy(10);
  }
});

updateDatasetChrome();
loadLabelSuggestions();
loadStats();
loadCalls();

setInterval(() => {
  loadStats({ refreshCalls: true });
}, 20000);
