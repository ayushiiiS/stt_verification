const state = {
  page: 1,
  perPage: 50,
  search: "",
  status: "all",
  selectedId: null,
  currentCall: null,
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
      renderTranscript(call);
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

function roleLabel(msg) {
  if (msg.type === "language_switch" && msg.switchNo) {
    return `Switch ${msg.switchNo}`;
  }
  return msg.role;
}

function renderTranscript(call) {
  const messages = call.messages;
  const stt = call.stt_messages || [];
  const finalMessages = call.final_messages;

  els.transcriptGrid.innerHTML = messages
    .map((msg, index) => {
      const roleClass =
        msg.role === "user" || msg.role === "assistant"
          ? msg.role
          : msg.type === "language_switch"
            ? "switch"
            : "";
      const finalContent = finalMessages[index]?.content ?? "";
      const sttContent = stt[index]?.content ?? "Not generated yet";

      return `
        <article class="message-card" data-index="${index}">
          <div class="message-header">
            <span class="role ${roleClass}">${escapeHtml(roleLabel(msg))}</span>
            <span class="message-type">${msg.type === "language_switch" ? "language switch" : "transcript"}</span>
          </div>
          <div>
            <div class="column-label">Sarvam STT</div>
            <div class="stt-text">${escapeHtml(sttContent)}</div>
          </div>
          <div>
            <div class="column-label">Final</div>
            <textarea class="final-input" data-index="${index}">${escapeHtml(finalContent)}</textarea>
          </div>
        </article>
      `;
    })
    .join("");
}

function updateMeta() {
  const call = state.currentCall;
  if (!call) return;
  const parts = [`${call.messages.length} messages`];
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

  els.emptyState.classList.add("hidden");
  els.callDetail.classList.remove("hidden");
  els.callId.textContent = call.number != null ? `#${call.number} · ${call.id}` : call.id;
  els.player.src = call.public_url || "";
  els.player.load();
  els.resetBtn.textContent = call.hasStt ? "Reset to Sarvam" : "Reset to default";

  renderTranscript(call);
  updateMeta();
}

function collectFinalMessages() {
  const textareas = [...els.transcriptGrid.querySelectorAll(".final-input")];
  return textareas.map((textarea, index) => ({
    ...state.currentCall.messages[index],
    content: textarea.value,
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
    renderTranscript(state.currentCall);
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
