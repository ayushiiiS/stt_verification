const DATASET_LABELS = {
  indiamart: "IndiaMART",
  abhfl: "ABHFL",
  amber: "Amber",
};

const state = {
  dataset: "indiamart",
  page: 1,
  perPage: 50,
  search: "",
  status: "all",
  sort: "number",
  selectedId: null,
  currentCall: null,
  draft: [],
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
  currentUser: "",
  lastStats: null,
};

const els = {
  stats: document.getElementById("stats"),
  queueStats: document.getElementById("queueStats"),
  subtitle: document.getElementById("subtitle"),
  datasetTabs: document.getElementById("datasetTabs"),
  progressPanel: document.getElementById("progressPanel"),
  progressFill: document.getElementById("progressFill"),
  progressMeta: document.getElementById("progressMeta"),
  search: document.getElementById("search"),
  statusFilter: document.getElementById("statusFilter"),
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
  exportVerifiedBtn: document.getElementById("exportVerifiedBtn"),
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
  if (item.status === "edited") return 66;
  if (item.status === "unfit") return 33;
  if (item.hasStt) return 33;
  return 0;
}

function sortCallItems(items) {
  const sorted = [...items];
  const rank = { pending: 0, edited: 1, verified: 2, unfit: -1 };
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
  const s = Math.floor(seconds % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}

function statusLabel(status) {
  if (status === "verified") return "Final verified";
  if (status === "edited") return "Final saved";
  if (status === "unfit") return "Unfit";
  return "Final not saved";
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
  const edited = data.edited || 0;
  const pending = data.pending || 0;
  const unfit = data.unfit || 0;
  const reviewed = edited + verified;
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
    <div class="metric-card accent-verified">
      <span class="metric-label">Verified</span>
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
        <span class="queue-stat-label">Reviewed</span>
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
  updateSttButton(Boolean(data.sttProgress?.running));

  if (options.refreshCalls && data.sttProgress?.running) {
    await loadCalls();
    if (state.selectedId) {
      try {
        const call = await fetchJSON(apiUrl(`/api/calls/${state.selectedId}`));
        const wasDirty = isDraftDirty();
        state.currentCall = call;
        if (!wasDirty && !call.edited) {
          buildDraft(call);
          renderTranscript();
          refreshSavedSnapshot();
        }
        updateMeta();
      } catch {
        /* ignore */
      }
    }
  }
}

function updateSttButton(running) {
  if (!els.startSttBtn) return;
  els.startSttBtn.disabled = running;
  els.startSttBtn.innerHTML = running
    ? `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M12 2a3 3 0 0 1 3 3v6a3 3 0 0 1-6 0V5a3 3 0 0 1 3-3z"/><path d="M19 10a7 7 0 0 1-14 0M12 17v5"/></svg> Sarvam STT running…`
    : `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M12 2a3 3 0 0 1 3 3v6a3 3 0 0 1-6 0V5a3 3 0 0 1 3-3z"/><path d="M19 10a7 7 0 0 1-14 0M12 17v5"/></svg> Start Sarvam STT`;
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
      : "Upload calls, then start Sarvam STT";
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
    })
  );

  if (!data.items.length) {
    els.callList.innerHTML = `<li class="empty-list">No calls yet. Upload a JSON file.</li>`;
    renderPagination(data);
    return;
  }

  const items = sortCallItems(data.items);

  els.callList.innerHTML = items
    .map((item) => {
      const sttBadge = item.hasStt
        ? '<span class="badge stt">Sarvam</span>'
        : '<span class="badge stt-pending">No Sarvam</span>';
      const duration = callDurationLabel(item);
      const durationBadge = duration
        ? `<span class="badge duration">${duration}</span>`
        : "";
      const progress = queueProgressPercent(item);
      return `
      <li>
        <button type="button" class="call-item ${item.id === state.selectedId ? "active" : ""}" data-id="${item.id}">
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
            <span class="badge ${
              item.status === "verified"
                ? "verified"
                : item.status === "edited"
                  ? "edited"
                  : item.status === "unfit"
                    ? "unfit"
                    : "pending"
            }">${
              item.status === "verified"
                ? "Verified"
                : item.status === "edited"
                  ? "Saved"
                  : item.status === "unfit"
                    ? "Unfit"
                    : "Draft"
            }</span>
          </div>
          <div class="call-preview">${escapeHtml(item.preview)}</div>
          <div class="call-item-progress" aria-hidden="true"><span style="width:${progress}%"></span></div>
        </button>
      </li>
    `;
    })
    .join("");

  els.callList.querySelectorAll(".call-item").forEach((btn) => {
    btn.onclick = () => selectCall(btn.dataset.id);
  });

  renderPagination(data);
}

function buildDraft(call) {
  const finals = call.final_messages || [];
  const originals = call.messages || [];
  const stt = call.stt_messages || [];
  const timings = call.timings || [];

  state.draft = finals.map((msg, index) => ({
    _id: msg._id || originals[index]?._id || `draft-${index + 1}`,
    role: msg.role === "user" ? "user" : "assistant",
    type: "message",
    createdAt: msg.createdAt || originals[index]?.createdAt || "",
    content: msg.content ?? "",
    originalContent: originals[index]?.content ?? "",
    sttContent: stt[index]?.content ?? "",
    start: timings[index]?.start ?? null,
    end: timings[index]?.end ?? null,
    added: index >= originals.length,
  }));
}

function syncDraftFromDom() {
  const cards = [...els.transcriptGrid.querySelectorAll(".message-card")];
  cards.forEach((card) => {
    const index = Number(card.dataset.index);
    if (!state.draft[index]) return;
    const roleSelect = card.querySelector(".role-select");
    const textarea = card.querySelector(".final-input");
    if (roleSelect) state.draft[index].role = roleSelect.value;
    if (textarea) state.draft[index].content = textarea.value;
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

function handleFinalInput(event) {
  const textarea = event.target;
  if (!textarea.classList.contains("final-input")) return;
  state.activeTextarea = textarea;
  const index = Number(textarea.dataset.index);
  if (state.draft[index]) state.draft[index].content = textarea.value;

  const word = currentWordBeforeCursor(textarea);

  clearTimeout(state.phraseTimer);
  state.phraseTimer = setTimeout(() => fetchSuggestions(word.toLowerCase()), 180);

  // Prefetch Devanagari while typing so Tab is ready immediately
  clearTimeout(state.translitTimer);
  state.translitTimer = setTimeout(() => prefetchTransliteration(word), 60);

  updateMeta();
}

function alignDraftTimingsToAudio(duration) {
  if (!Number.isFinite(duration) || duration < 2 || !state.draft.length) return false;
  const ends = state.draft
    .map((m) => (m.end != null ? m.end : m.start))
    .filter((v) => v != null && Number.isFinite(v));
  if (!ends.length) return false;
  const lastEnd = Math.max(...ends);
  if (lastEnd < 2) return false;
  const ratio = duration / lastEnd;
  // Only gentle rescale — corrects small clock drift vs recording length.
  if (ratio < 0.85 || ratio > 1.2) return false;
  state.draft.forEach((msg) => {
    if (msg.start != null) msg.start = Number((msg.start * ratio).toFixed(3));
    if (msg.end != null) msg.end = Number((msg.end * ratio).toFixed(3));
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
  if (index < 0) return;
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

  // Sticky highlight: active turn is the latest whose start has been reached.
  // Falls back to start→end windows when starts are missing.
  let active = -1;
  state.draft.forEach((msg, index) => {
    if (msg.start == null || Number.isNaN(msg.start)) return;
    if (t >= msg.start - 0.08) active = index;
  });
  if (active < 0) {
    state.draft.forEach((msg, index) => {
      if (msg.start == null || Number.isNaN(msg.start)) return;
      const end =
        msg.end != null && !Number.isNaN(msg.end) ? msg.end : msg.start + 1.2;
      if (t >= msg.start - 0.02 && t < end + 0.02) active = index;
    });
  }

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

function renderTranscript() {
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
        const timeLabel =
          msg.start != null && !Number.isNaN(msg.start)
            ? msg.end != null && !Number.isNaN(msg.end)
              ? `${formatTime(msg.start)}–${formatTime(msg.end)}`
              : formatTime(msg.start)
            : "";
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
                ${
                  timeLabel
                    ? `<button type="button" class="timing-chip" data-seek="${index}" title="Seek audio to this turn">${timeLabel}</button>`
                    : ""
                }
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
                <div class="original-text">${escapeHtml(originalContent)}</div>
              </div>
              <div class="col-card sarvam">
                <div class="column-label">Sarvam</div>
                <div class="stt-text">${escapeHtml(sttContent)}</div>
              </div>
              <div class="col-card final final-col">
                <div class="column-label">
                  <span>Final</span>
                  <span class="char-count" data-index="${index}">${(msg.content || "").length} chars</span>
                </div>
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
      renderTranscript();
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
      state.draft.splice(after + 1, 0, {
        _id: `added-${Date.now()}-${after + 1}`,
        role: prev?.role === "assistant" ? "user" : "assistant",
        type: "message",
        createdAt: "",
        content: "",
        originalContent: "",
        sttContent: "",
        start: null,
        end: null,
        added: true,
      });
      renderTranscript();
      updateMeta();
    };
  });

  els.transcriptGrid.querySelectorAll(".timing-chip").forEach((btn) => {
    btn.onclick = () => seekToTurn(Number(btn.dataset.seek));
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

function snapshotFinalMessages(messages) {
  return JSON.stringify(messages);
}

function refreshSavedSnapshot() {
  state.savedSnapshot = snapshotFinalMessages(collectFinalMessages());
}

function isDraftDirty() {
  if (!state.currentCall) return false;
  return snapshotFinalMessages(collectFinalMessages()) !== state.savedSnapshot;
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
      call.status === "verified" ||
      call.status === "unfit"
  );

  if (call.status === "unfit" && !dirty) {
    return { label: "Marked unfit", className: "unfit", dirty: false, persisted: true };
  }
  if (call.status === "verified" && !dirty) {
    return { label: "Final verified", className: "verified", dirty: false, persisted: true };
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
  const isVerified = call.status === "verified";
  const canVerify = call.status === "edited" && !info.dirty;
  const canUnverify = isVerified && !info.dirty;
  els.verifyBtn.disabled = !(canVerify || canUnverify);
  els.verifyBtn.innerHTML = isVerified
    ? `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M18 6L6 18M6 6l12 12"/></svg> Unverify`
    : `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M20 6L9 17l-5-5"/></svg> Verify`;
  els.verifyBtn.classList.toggle("secondary", isVerified);
  els.verifyBtn.classList.toggle("verify", !isVerified);
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
  loadCalls();

  const call = await fetchJSON(apiUrl(`/api/calls/${callId}`));
  state.currentCall = call;
  buildDraft(call);

  els.emptyState.classList.add("hidden");
  els.callDetail.classList.remove("hidden");
  els.callId.textContent =
    call.number != null ? `#${call.number} · ${call.id}` : call.id;
  els.player.classList.add("hidden-audio");
  els.resetBtn.textContent = call.hasStt ? "Reset to Sarvam" : "Reset to original";

  initWaveform(call.public_url || "");
  renderTranscript();
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
    const result = await fetchJSON(apiUrl(`/api/calls/${state.currentCall.id}/correct`), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ messages }),
    });
    state.currentCall.edited = true;
    state.currentCall.status = "edited";
    state.currentCall.updatedAt = result.updatedAt;
    state.currentCall.editedBy = result.editedBy;
    state.currentCall.verifiedBy = "";
    state.currentCall.verifiedAt = null;
    state.currentCall.unfitBy = "";
    state.currentCall.unfitAt = null;
    state.currentCall.unfitReason = "";
    state.currentCall.final_messages = messages;
    refreshSavedSnapshot();
    updateMeta();
    await loadStats();
    await loadCalls();
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
  const clearing = state.currentCall.status === "verified";
  try {
    const result = await fetchJSON(apiUrl(`/api/calls/${state.currentCall.id}/verify`), {
      method: clearing ? "DELETE" : "POST",
      headers: { "Content-Type": "application/json" },
      body: clearing ? undefined : JSON.stringify({}),
    });
    state.currentCall.status = result.status || (clearing ? "edited" : "verified");
    state.currentCall.verifiedBy = result.verifiedBy || "";
    state.currentCall.verifiedAt = result.verifiedAt || null;
    updateMeta();
    await loadStats();
    await loadCalls();
    showToast(
      clearing
        ? "Verification cleared"
        : `Verified by ${result.verifiedBy}`,
      "success"
    );
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
      state.currentCall.edited = result.status === "edited" || result.status === "verified";
    } else {
      state.currentCall.edited = true;
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
  const label = state.currentCall.hasStt ? "Sarvam transcript" : "original transcript";
  if (!confirm(`Reset final transcript to ${label} for this call?`)) return;
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
    state.currentCall.verifiedBy = "";
    buildDraft(state.currentCall);
    renderTranscript();
    refreshSavedSnapshot();
    updateMeta();
    await loadStats();
    await loadCalls();
    showToast(`Reset to ${label}`);
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
  state.sort = "number";
  state.selectedId = null;
  state.currentCall = null;
  state.draft = [];
  els.search.value = "";
  els.statusFilter.value = "all";
  if (els.sortSelect) els.sortSelect.value = "number";
  els.emptyState.classList.remove("hidden");
  els.callDetail.classList.add("hidden");
  destroyWaveform();
  updateDatasetChrome();
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
    if (confirm(`Start Sarvam STT for ${data.imported} uploaded calls?`)) {
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

async function exportVerified() {
  try {
    const res = await fetch(apiUrl("/api/export/verified"));
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      throw new Error(data.error || "Export failed");
    }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${state.dataset}_verified_transcripts.json`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    showToast("Downloaded verified transcripts");
  } catch (err) {
    showToast(err.message);
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

els.speedSelect.addEventListener("change", () => {
  const rate = Number(els.speedSelect.value) || 1;
  if (state.wavesurfer) state.wavesurfer.setPlaybackRate(rate);
  els.player.playbackRate = rate;
});

els.uploadInput.addEventListener("change", () => {
  const file = els.uploadInput.files?.[0];
  handleUpload(file);
});

els.startSttBtn.addEventListener("click", startSarvamStt);
els.exportVerifiedBtn.addEventListener("click", exportVerified);

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
loadStats();
loadCalls();

setInterval(() => {
  loadStats({ refreshCalls: true });
}, 8000);
