const state = {
  page: 1,
  perPage: 50,
  search: "",
  status: "all",
  selectedId: null,
  currentCall: null,
  draft: [],
  searchTimer: null,
};

const els = {
  stats: document.getElementById("stats"),
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
  callMeta: document.getElementById("callMeta"),
  player: document.getElementById("player"),
  transcriptGrid: document.getElementById("transcriptGrid"),
  saveBtn: document.getElementById("saveBtn"),
  resetBtn: document.getElementById("resetBtn"),
  toast: document.getElementById("toast"),
};

function showToast(message) {
  els.toast.textContent = message;
  els.toast.classList.remove("hidden");
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => els.toast.classList.add("hidden"), 2500);
}

async function fetchJSON(url, options) {
  const res = await fetch(url, options);
  const data = await res.json();
  if (!res.ok) {
    throw new Error(data.error || "Request failed");
  }
  return data;
}

async function loadStats(options = {}) {
  const data = await fetchJSON("/api/stats");
  els.stats.innerHTML = `
    <span class="stat-pill">Total: ${data.total}</span>
    <span class="stat-pill">Final saved: ${data.edited}</span>
    <span class="stat-pill">Sarvam STT: ${data.sttGenerated}/${data.total}</span>
  `;
  renderSttProgress(data.sttProgress, data.sttGenerated, data.total);

  if (options.refreshCalls && data.sttProgress?.running) {
    await loadCalls();
    if (state.selectedId) {
      const call = await fetchJSON(`/api/calls/${state.selectedId}`);
      state.currentCall = call;
      if (!state.currentCall.edited) {
        buildDraft(call);
        renderTranscript();
      }
      updateMeta();
    }
  }
}

function renderSttProgress(progress, sttGenerated, total) {
  const saved = progress?.savedTotal ?? sttGenerated ?? 0;
  const targetTotal = progress?.total || total || 0;
  const percent = progress?.percent ?? (targetTotal ? Math.round((saved / targetTotal) * 1000) / 10 : 0);
  const running = Boolean(progress?.running);
  const failed = progress?.failed ?? 0;

  els.progressFill.style.width = `${Math.min(100, percent)}%`;
  els.progressPanel.classList.toggle("running", running);
  els.progressPanel.classList.toggle("complete", !running && saved >= targetTotal && targetTotal > 0);

  const parts = [`${saved}/${targetTotal} transcribed (${percent}%)`];
  if (running) {
    parts.push(`${progress.completed ?? 0} done this run`);
    if (progress.workers) parts.push(`${progress.workers} workers`);
  }
  if (failed) parts.push(`${failed} failed`);
  if (progress?.updatedAt) {
    parts.push(`updated ${new Date(progress.updatedAt).toLocaleTimeString()}`);
  }
  els.progressMeta.textContent = parts.join(" · ");
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
  const params = new URLSearchParams({
    page: state.page,
    per_page: state.perPage,
    search: state.search,
    status: state.status,
  });
  const data = await fetchJSON(`/api/calls?${params}`);
  els.callList.innerHTML = data.items
    .map((item) => {
      const sttBadge = item.hasStt
        ? '<span class="badge stt">Sarvam</span>'
        : '<span class="badge stt-pending">No STT</span>';
      return `
      <li>
        <button type="button" class="call-item ${item.id === state.selectedId ? "active" : ""}" data-id="${item.id}">
          <div class="call-item-top">
            <span class="call-item-number">#${item.number}</span>
            <span class="call-item-id">${item.id}</span>
            <span class="badge-wrap">
              ${sttBadge}
              <span class="badge ${item.edited ? "edited" : "pending"}">${item.edited ? "Final saved" : "Pending"}</span>
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

function escapeHtml(text) {
  return String(text)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function buildDraft(call) {
  const finals = call.final_messages || [];
  const stt = call.stt_messages || [];
  const originals = call.messages || [];

  state.draft = finals.map((msg, index) => {
    const sttContent =
      index < stt.length
        ? stt[index]?.content ?? ""
        : index < originals.length
          ? ""
          : "";
    return {
      _id: msg._id || originals[index]?._id || `draft-${index + 1}`,
      role: msg.role === "user" ? "user" : "assistant",
      type: "message",
      createdAt: msg.createdAt || originals[index]?.createdAt || "",
      content: msg.content ?? "",
      sttContent: sttContent || (index < stt.length ? stt[index]?.content ?? "" : ""),
      added: index >= originals.length,
    };
  });

  // Prefer STT text for display when available on original-length rows
  state.draft.forEach((row, index) => {
    if (index < stt.length) {
      row.sttContent = stt[index]?.content ?? "";
    } else {
      row.sttContent = "";
    }
  });
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

function renderTranscript() {
  els.transcriptGrid.innerHTML = state.draft
    .map((msg, index) => {
      const sttContent = msg.sttContent?.trim()
        ? msg.sttContent
        : msg.added
          ? "—"
          : "Not generated yet";
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
                <span class="message-type">${msg.added ? "added turn" : "transcript"}</span>
                <button type="button" class="delete-turn-btn" data-index="${index}" title="Delete this turn" ${state.draft.length <= 1 ? "disabled" : ""}>Delete</button>
              </div>
            </div>
            <div>
              <div class="column-label">Sarvam STT</div>
              <div class="stt-text">${escapeHtml(sttContent)}</div>
            </div>
            <div>
              <div class="column-label">Final</div>
              <textarea class="final-input" data-index="${index}">${escapeHtml(msg.content)}</textarea>
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
    };
  });

  els.transcriptGrid.querySelectorAll(".delete-turn-btn").forEach((btn) => {
    btn.onclick = () => {
      if (state.draft.length <= 1) {
        showToast("Keep at least one turn");
        return;
      }
      syncDraftFromDom();
      const index = Number(btn.dataset.index);
      state.draft.splice(index, 1);
      renderTranscript();
      updateMeta();
    };
  });

  els.transcriptGrid.querySelectorAll(".add-turn-btn").forEach((btn) => {
    btn.onclick = () => {
      syncDraftFromDom();
      const after = Number(btn.dataset.after);
      const prev = state.draft[after];
      const nextRole = prev?.role === "assistant" ? "user" : "assistant";
      state.draft.splice(after + 1, 0, {
        _id: `added-${Date.now()}-${after + 1}`,
        role: nextRole,
        type: "message",
        createdAt: "",
        content: "",
        sttContent: "",
        added: true,
      });
      renderTranscript();
      updateMeta();
    };
  });
}

function updateMeta() {
  const call = state.currentCall;
  if (!call) return;
  const parts = [`${state.draft.length} turns`];
  parts.push(call.hasStt ? "Sarvam STT ready" : "Sarvam STT pending");
  if (call.edited && call.updatedAt) {
    parts.push(`saved ${new Date(call.updatedAt).toLocaleString()}`);
  } else {
    parts.push("final not saved yet");
  }
  els.callMeta.textContent = parts.join(" · ");
}

async function selectCall(callId) {
  state.selectedId = callId;
  loadCalls();

  const call = await fetchJSON(`/api/calls/${callId}`);
  state.currentCall = call;
  buildDraft(call);

  els.emptyState.classList.add("hidden");
  els.callDetail.classList.remove("hidden");
  els.callId.textContent = call.number != null ? `#${call.number} · ${call.id}` : call.id;
  els.player.src = call.public_url || "";
  els.player.load();
  els.resetBtn.textContent = call.hasStt ? "Reset to Sarvam" : "Reset to default";

  renderTranscript();
  updateMeta();
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

async function saveFinal() {
  if (!state.currentCall) return;
  try {
    const messages = collectFinalMessages();
    const result = await fetchJSON(`/api/calls/${state.currentCall.id}/correct`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ messages }),
    });
    state.currentCall.edited = true;
    state.currentCall.updatedAt = result.updatedAt;
    state.currentCall.final_messages = messages;
    updateMeta();
    await loadStats();
    await loadCalls();
    showToast("Final transcript saved");
  } catch (err) {
    showToast(err.message);
  }
}

async function resetFinal() {
  if (!state.currentCall) return;
  const label = state.currentCall.hasStt ? "Sarvam transcript" : "default transcript";
  if (!confirm(`Reset final transcript to ${label} for this call?`)) return;
  try {
    const result = await fetchJSON(`/api/calls/${state.currentCall.id}/correct`, { method: "DELETE" });
    state.currentCall.final_messages = result.final_messages;
    state.currentCall.edited = false;
    state.currentCall.updatedAt = null;
    buildDraft(state.currentCall);
    renderTranscript();
    updateMeta();
    await loadStats();
    await loadCalls();
    showToast(`Reset to ${label}`);
  } catch (err) {
    showToast(err.message);
  }
}

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

els.saveBtn.addEventListener("click", saveFinal);
els.resetBtn.addEventListener("click", resetFinal);

document.addEventListener("keydown", (event) => {
  if ((event.metaKey || event.ctrlKey) && event.key === "s") {
    event.preventDefault();
    saveFinal();
  }
});

loadStats();
loadCalls();

setInterval(() => {
  loadStats({ refreshCalls: true });
}, 5000);
