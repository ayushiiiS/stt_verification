const DATASET_LABELS = {
  indiamart: "IndiaMART",
  spinny: "Spinny",
  amc: "AMC",
  abhfl: "ABHFL",
  amber: "Amber",
};

const state = {
  dataset: "indiamart",
  page: 1,
  perPage: 50,
  search: "",
  status: "all",
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
};

const els = {
  stats: document.getElementById("stats"),
  subtitle: document.getElementById("subtitle"),
  datasetTabs: document.getElementById("datasetTabs"),
  progressPanel: document.getElementById("progressPanel"),
  progressFill: document.getElementById("progressFill"),
  progressMeta: document.getElementById("progressMeta"),
  search: document.getElementById("search"),
  statusFilter: document.getElementById("statusFilter"),
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
  speedSelect: document.getElementById("speedSelect"),
  transcriptGrid: document.getElementById("transcriptGrid"),
  prevCallBtn: document.getElementById("prevCallBtn"),
  nextCallBtn: document.getElementById("nextCallBtn"),
  saveBtn: document.getElementById("saveBtn"),
  resetBtn: document.getElementById("resetBtn"),
  verifyBtn: document.getElementById("verifyBtn"),
  uploadInput: document.getElementById("uploadInput"),
  uploadHint: document.getElementById("uploadHint"),
  startSttBtn: document.getElementById("startSttBtn"),
  exportVerifiedBtn: document.getElementById("exportVerifiedBtn"),
  currentUser: document.getElementById("currentUser"),
  suggestPopup: document.getElementById("suggestPopup"),
  toast: document.getElementById("toast"),
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

function showToast(message) {
  els.toast.textContent = message;
  els.toast.classList.remove("hidden");
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => els.toast.classList.add("hidden"), 2800);
}

async function fetchJSON(url, options) {
  const res = await fetch(url, options);
  if (res.status === 401) {
    window.location.href = "/login";
    throw new Error("Login required");
  }
  const data = await res.json();
  if (!res.ok) {
    throw new Error(data.error || "Request failed");
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
  return "Final not saved";
}

function updateDatasetChrome() {
  els.datasetTabs.querySelectorAll(".dataset-tab").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.dataset === state.dataset);
  });
  const label = DATASET_LABELS[state.dataset] || state.dataset;
  els.subtitle.textContent = `${label} · upload JSON, edit finals, second-user verify`;
  els.uploadHint.textContent = `Upload into ${label}`;
}

async function loadStats(options = {}) {
  const data = await fetchJSON(apiUrl("/api/stats"));
  els.stats.innerHTML = `
    <span class="stat-pill total">Total ${data.total}</span>
    <span class="stat-pill stt">STT ${data.sttGenerated}/${data.total}</span>
    <span class="stat-pill pending">Not saved ${data.pending}</span>
    <span class="stat-pill saved">Final saved ${data.edited}</span>
    <span class="stat-pill verified">Verified ${data.verified}</span>
  `;
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
  els.startSttBtn.textContent = running ? "Sarvam STT running…" : "Start Sarvam STT";
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

  els.callList.innerHTML = data.items
    .map((item) => {
      const sttBadge = item.hasStt
        ? '<span class="badge stt">STT</span>'
        : '<span class="badge stt-pending">No STT</span>';
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
          <div class="call-preview">${escapeHtml(item.preview)}</div>
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

function destroyWaveform() {
  if (state.highlightTimer) {
    cancelAnimationFrame(state.highlightTimer);
    state.highlightTimer = null;
  }
  if (state.wavesurfer) {
    state.wavesurfer.destroy();
    state.wavesurfer = null;
  }
  state.lastActiveTurn = -1;
  els.waveform.innerHTML = "";
  els.playPauseBtn.textContent = "▶";
  els.timeDisplay.textContent = "0:00 / 0:00";
}

function scrollActiveTurnIntoView(index) {
  if (index < 0) return;
  const block = els.transcriptGrid.querySelector(`.turn-block[data-index="${index}"]`);
  if (!block) return;
  block.scrollIntoView({
    behavior: "smooth",
    block: "center",
    inline: "nearest",
  });
}

function syncHighlight() {
  if (!state.wavesurfer) return;
  const t = state.wavesurfer.getCurrentTime();
  const duration = state.wavesurfer.getDuration() || 0;
  els.timeDisplay.textContent = `${formatTime(t)} / ${formatTime(duration)}`;

  let active = -1;
  state.draft.forEach((msg, index) => {
    if (msg.start == null || msg.end == null) return;
    if (t >= msg.start && t < msg.end + 0.05) active = index;
  });

  els.transcriptGrid.querySelectorAll(".turn-block").forEach((block) => {
    const index = Number(block.dataset.index);
    block.classList.toggle("active-turn", index === active);
  });

  if (active !== state.lastActiveTurn) {
    state.lastActiveTurn = active;
    scrollActiveTurnIntoView(active);
  }

  if (state.wavesurfer.isPlaying()) {
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
    height: 72,
    waveColor: "#94a3b8",
    progressColor: "#2563eb",
    cursorColor: "#1d4ed8",
    cursorWidth: 2,
    barWidth: 2,
    barGap: 1,
    barRadius: 2,
    normalize: true,
    interact: true,
  });

  const rate = Number(els.speedSelect.value) || 1;
  state.wavesurfer.setPlaybackRate(rate);

  state.wavesurfer.on("ready", () => {
    els.timeDisplay.textContent = `0:00 / ${formatTime(state.wavesurfer.getDuration())}`;
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
  });

  state.wavesurfer.on("interaction", () => {
    syncHighlight();
  });

  state.wavesurfer.on("error", () => {
    els.waveform.innerHTML = `<div class="waveform-empty">Could not load waveform (CORS or expired URL). Audio may still play below.</div>`;
    els.player.src = url;
    els.player.classList.remove("hidden-audio");
  });
}

function seekToTurn(index) {
  const msg = state.draft[index];
  if (!msg || msg.start == null || !state.wavesurfer) return;
  const duration = state.wavesurfer.getDuration() || 1;
  state.wavesurfer.seekTo(Math.min(0.999, Math.max(0, msg.start / duration)));
  if (!state.wavesurfer.isPlaying()) state.wavesurfer.play();
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
        msg.start != null && msg.end != null
          ? `${formatTime(msg.start)}–${formatTime(msg.end)}`
          : "no timing";
      return `
        <div class="turn-block" data-index="${index}">
          <article class="message-card" data-index="${index}">
            <div class="message-header">
              <label class="role-label">
                <span class="sr-only">Role</span>
                <select class="role-select ${msg.role}" data-index="${index}">
                  <option value="assistant" ${msg.role === "assistant" ? "selected" : ""}>assistant</option>
                  <option value="user" ${msg.role === "user" ? "selected" : ""}>user</option>
                </select>
              </label>
              <div class="message-header-actions">
                <button type="button" class="timing-chip" data-seek="${index}" title="Seek audio to this turn">${timeLabel}</button>
                <span class="message-type">${msg.added ? "added turn" : "transcript"}</span>
                <button type="button" class="delete-turn-btn" data-index="${index}" title="Delete this turn" ${state.draft.length <= 1 ? "disabled" : ""}>Delete</button>
              </div>
            </div>
            <div>
              <div class="column-label">Original</div>
              <div class="original-text">${escapeHtml(originalContent)}</div>
            </div>
            <div>
              <div class="column-label">Sarvam STT</div>
              <div class="stt-text">${escapeHtml(sttContent)}</div>
            </div>
            <div class="final-col">
              <div class="column-label">Final</div>
              <textarea class="final-input" data-index="${index}" rows="3">${escapeHtml(msg.content)}</textarea>
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
    textarea.addEventListener("keydown", handleFinalKeydown);
    textarea.addEventListener("input", handleFinalInput);
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
  const persisted = Boolean(call.edited || call.status === "edited" || call.status === "verified");

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
  els.saveStatusBadge.className = `save-status ${info.className}`;
  els.saveStatusBadge.classList.remove("hidden");

  if (info.dirty) {
    els.saveBtn.textContent = "Save changes";
    els.saveBtn.classList.add("needs-save");
  } else if (info.persisted) {
    els.saveBtn.textContent = "Final saved";
    els.saveBtn.classList.remove("needs-save");
  } else {
    els.saveBtn.textContent = "Save final";
    els.saveBtn.classList.remove("needs-save");
  }
}

function updateMeta() {
  const call = state.currentCall;
  if (!call) return;
  const info = saveStatusInfo();
  const parts = [`${state.draft.length} turns`, info.label];
  if (call.editedBy) parts.push(`edited by ${call.editedBy}`);
  if (call.verifiedBy) parts.push(`verified by ${call.verifiedBy}`);
  if (call.updatedAt && info.persisted && !info.dirty) {
    parts.push(`saved ${new Date(call.updatedAt).toLocaleString()}`);
  } else if (info.dirty && info.persisted) {
    parts.push("you have unsaved edits");
  }
  els.callMeta.textContent = parts.join(" · ");
  els.verifyBtn.disabled = call.status !== "edited" || info.dirty;
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
    state.currentCall.final_messages = messages;
    refreshSavedSnapshot();
    updateMeta();
    await loadStats();
    await loadCalls();
    showToast(`Final saved by ${result.editedBy} — needs another user to verify`);
  } catch (err) {
    showToast(err.message);
  }
}

async function verifyFinal() {
  if (!state.currentCall) return;
  if (!getReviewer()) {
    showToast("Please log in again");
    window.location.href = "/login";
    return;
  }
  try {
    const result = await fetchJSON(apiUrl(`/api/calls/${state.currentCall.id}/verify`), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    state.currentCall.status = "verified";
    state.currentCall.verifiedBy = result.verifiedBy;
    state.currentCall.verifiedAt = result.verifiedAt;
    updateMeta();
    await loadStats();
    await loadCalls();
    showToast(`Verified by ${result.verifiedBy}`);
  } catch (err) {
    showToast(err.message);
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
  state.selectedId = null;
  state.currentCall = null;
  state.draft = [];
  els.search.value = "";
  els.statusFilter.value = "all";
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

els.prevCallBtn.addEventListener("click", () => {
  if (state.currentCall?.prevId) selectCall(state.currentCall.prevId);
});

els.nextCallBtn.addEventListener("click", () => {
  if (state.currentCall?.nextId) selectCall(state.currentCall.nextId);
});

els.saveBtn.addEventListener("click", saveFinal);
els.resetBtn.addEventListener("click", resetFinal);
els.verifyBtn.addEventListener("click", verifyFinal);

els.playPauseBtn.addEventListener("click", () => {
  if (!state.wavesurfer) return;
  state.wavesurfer.playPause();
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
  if (!state.wavesurfer) return;
  const duration = state.wavesurfer.getDuration() || 0;
  if (!duration) return;
  const next = Math.min(
    Math.max(0, state.wavesurfer.getCurrentTime() + seconds),
    Math.max(0, duration - 0.05)
  );
  state.wavesurfer.seekTo(next / duration);
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

  if (!state.wavesurfer) return;
  if (event.metaKey || event.ctrlKey || event.altKey) return;

  if (event.code === "Space" || event.key === " ") {
    // Allow normal spaces while typing in the final editor / inputs
    if (isEditingText(event.target)) return;
    event.preventDefault();
    state.wavesurfer.playPause();
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
