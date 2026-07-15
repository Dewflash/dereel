const state = { assets: [], visibleAssets: [], projects: [], jobs: [], publishingJobs: [], publishAttempts: {}, accounts: [], automations: [], automationRuns: [], assetReviews: {}, contentReviews: {}, selectedAutomationId: "", platforms: [], diagnostics: {}, providerResults: [], selectedProviderIds: new Set(), assetFilter: "all", assetQuery: "", assetSort: "recent", selectedAssets: new Set(), inspectedAssetId: "", selectedProject: null, publishDraft: { projectId: "", accountIds: {} }, automationDraft: { accountIds: {} }, editingAutomationId: "", editingAccountId: "", publishScheduleJobId: "" };
let refreshInFlight = null;
let refreshQueued = false;

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const escapeHtml = (value = "") => String(value).replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[char]);
const formatDuration = (seconds) => {
  if (!seconds) return "--";
  const value = Math.round(Number(seconds));
  return `${Math.floor(value / 60)}:${String(value % 60).padStart(2, "0")}`;
};
const formatBytes = (bytes) => {
  if (!bytes) return "--";
  const units = ["B", "KB", "MB", "GB"];
  let size = Number(bytes); let index = 0;
  while (size >= 1024 && index < units.length - 1) { size /= 1024; index += 1; }
  return `${size.toFixed(size >= 10 || index === 0 ? 0 : 1)} ${units[index]}`;
};
const statusClass = (value = "") => value.toLowerCase().replace(/[^a-z]+/g, "-");
const statusPill = (value) => `<span class="status-pill ${statusClass(value)}">${escapeHtml(value === "drafted" ? "draft handoff" : String(value).replaceAll("_", " "))}</span>`;

async function api(path, options = {}) {
  const response = await fetch(path, { headers: { ...(options.body instanceof FormData ? {} : { "Content-Type": "application/json" }), ...(options.headers || {}) }, ...options });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error || `Request failed (${response.status})`);
  return payload;
}

function toast(message, isError = false) {
  const node = document.createElement("div");
  node.className = `toast${isError ? " error" : ""}`;
  node.textContent = message;
  $("#toast-stack").append(node);
  setTimeout(() => node.remove(), 4500);
}

function showView(view) {
  $$(".view").forEach((node) => node.classList.toggle("active", node.id === `view-${view}`));
  $$(".nav-item").forEach((node) => node.classList.toggle("active", node.dataset.view === view));
  const title = view.charAt(0).toUpperCase() + view.slice(1);
  $("#page-title").textContent = title;
  history.replaceState(null, "", `#${view}`);
  if (view === "scout") renderAssets();
  if (view === "automations") renderAutomations();
  if (view === "forge") renderProjects();
  if (view === "outputs") renderOutputs();
  if (view === "publisher") renderPublisher();
}

async function refresh() {
  if (refreshInFlight) {
    refreshQueued = true;
    return refreshInFlight;
  }
  refreshInFlight = (async () => {
    do {
      refreshQueued = false;
      await refreshWorkspace();
    } while (refreshQueued);
  })();
  try {
    return await refreshInFlight;
  } finally {
    refreshInFlight = null;
  }
}

async function refreshWorkspace() {
  const [dashboard, allAssets, filteredAssets, projects, jobs, accounts, publishing, automations, platforms, diagnostics] = await Promise.all([
    api("/api/dashboard"), api("/api/assets?view=all"), api(`/api/assets?view=${state.assetFilter}`), api("/api/projects"), api("/api/jobs"), api("/api/publishing/accounts"), api("/api/publishing/jobs"), api("/api/automations"), api("/api/publishing/platforms"), api("/api/diagnostics"),
  ]);
  state.assets = allAssets.assets || [];
  state.visibleAssets = filteredAssets.assets || [];
  state.projects = projects.projects || [];
  state.jobs = jobs.jobs || [];
  state.accounts = accounts.accounts || [];
  state.publishingJobs = publishing.jobs || [];
  state.automations = automations.automations || [];
  if (state.selectedAutomationId && state.automations.some((item) => item.id === state.selectedAutomationId)) {
    try {
      state.automationRuns = (await api(`/api/automations/${state.selectedAutomationId}/runs`)).runs || [];
    } catch (_error) {
      state.automationRuns = [];
    }
  } else {
    state.selectedAutomationId = "";
    state.automationRuns = [];
  }
  state.platforms = platforms.platforms || [];
  state.diagnostics = diagnostics || {};
  renderDashboard(dashboard, state.diagnostics);
  renderAssets();
  renderAudioSelect();
  renderProjects();
  renderOutputs();
  renderPublisher();
  renderAutomations();
  const active = state.jobs.find((job) => ["queued", "running"].includes(job.status));
  $("#latest-job-status").textContent = active ? `${active.message} ${Math.round(active.progress * 100)}%` : "No active jobs";
}

function renderDashboard(data, diagnostics = {}) {
  const counts = data.counts || {};
  const metrics = [["ASSET LIBRARY", counts.assets || 0, "total sources"], ["PRODUCTION READY", counts.ready_assets || 0, "cleared media"], ["PROJECTS", counts.projects || 0, "edit briefs"], ["RENDERED", counts.ready_projects || 0, "ready exports"], ["OUTBOX", counts.publish_queue || 0, "queued packages"]];
  $("#metric-grid").innerHTML = metrics.map(([label, value, caption]) => `<div class="metric"><span class="eyebrow">${label}</span><strong>${value}</strong><span>${caption}</span></div>`).join("");
  const projects = data.recent_projects || [];
  $("#recent-projects").innerHTML = projects.length ? projects.map(projectRow).join("") : empty("No projects yet", "Create a brief in Forge to start an edit.");
  const jobs = data.jobs || [];
  $("#job-list").innerHTML = jobs.length ? jobs.map((job) => {
    const target = job.project_id || job.automation_id || "Workspace job";
    const attempt = Number(job.attempt || 1) > 1 ? ` · attempt ${Number(job.attempt)}` : "";
    const retry = job.status === "failed" && ["render", "automation"].includes(job.kind) ? `<button class="row-action" data-retry-job="${escapeHtml(job.id)}" title="Retry this failed attempt">↻ Retry</button>` : "";
    return `<div class="job-row"><div class="row-icon">${job.kind === "render" ? "▶" : "•"}</div><div class="row-main"><strong>${escapeHtml(job.message || job.kind)}</strong><small>${escapeHtml(target)}${attempt}${job.error ? ` · ${escapeHtml(job.error)}` : ""}</small></div>${statusPill(job.status)}${retry}</div>`;
  }).join("") : empty("No jobs yet", "Render and analysis jobs will appear here.");
  renderDiagnostics(diagnostics);
  $("#health-label").textContent = "ready";
}

function renderDiagnostics(diagnostics) {
  const node = $("#diagnostics-list");
  if (!node) return;
  const media = diagnostics.media || {};
  const checks = [
    ["FFmpeg", Boolean(media.ffmpeg), media.ffmpeg_path || "Binary not found"],
    ["FFprobe", Boolean(media.ffprobe), media.ffprobe_path || "Binary not found"],
    ["Text overlay", Boolean(media.text_overlay), media.drawtext ? "FFmpeg drawtext available" : media.pillow ? "Pillow overlay fallback available" : "Optional; captions still render"],
    ...(diagnostics.providers || []).map((provider) => [provider.label, Boolean(provider.configured), provider.configured ? "API key detected" : "API key not configured"]),
  ];
  node.innerHTML = checks.map(([label, ok, detail]) => `<div class="diagnostic-row"><div class="row-main"><strong>${escapeHtml(label)}</strong><small>${escapeHtml(detail)}</small></div>${statusPill(ok ? "ready" : "not_configured")}</div>`).join("");
}

function projectRow(project) {
  return `<div class="project-row"><div class="row-icon">✦</div><div class="row-main"><strong>${escapeHtml(project.name)}</strong><small>${escapeHtml(project.theme)} · ${formatDuration(project.duration_target)}</small></div>${statusPill(project.status)}<button class="row-action" data-project="${project.id}">Open</button><button class="row-action danger-action" data-delete-project="${project.id}">Delete</button></div>`;
}

function empty(title, detail) { return `<div class="empty-state"><span>·</span><strong>${escapeHtml(title)}</strong><p>${escapeHtml(detail)}</p></div>`; }

function libraryAssets() {
  const query = state.assetQuery.trim().toLowerCase();
  const filtered = state.visibleAssets.filter((asset) => {
    if (!query) return true;
    return [asset.id, asset.automation_id, asset.title, asset.platform, asset.source_url, ...(asset.tags || []), ...(asset.review_flags || [])].join(" ").toLowerCase().includes(query);
  });
  return filtered.sort((left, right) => {
    if (state.assetSort === "shortest") return Number(left.duration || Infinity) - Number(right.duration || Infinity);
    if (state.assetSort === "longest") return Number(right.duration || 0) - Number(left.duration || 0);
    if (state.assetSort === "name") return String(left.title || "").localeCompare(String(right.title || ""));
    if (state.assetSort === "review") return (right.review_flags || []).length - (left.review_flags || []).length;
    return String(right.created_at || "").localeCompare(String(left.created_at || ""));
  });
}

function renderAssets() {
  const assets = libraryAssets();
  $("#asset-search").value = state.assetQuery;
  $$(".filter-tab").forEach((item) => item.classList.toggle("active", item.dataset.filter === state.assetFilter));
  const selectable = new Set(state.visibleAssets.filter(isSelectableAsset).map((asset) => asset.id));
  state.selectedAssets = new Set([...state.selectedAssets].filter((id) => selectable.has(id)));
  const count = assets.length === state.visibleAssets.length ? `${assets.length}` : `${assets.length} of ${state.visibleAssets.length}`;
  $("#asset-count").textContent = `${count} asset${assets.length === 1 ? "" : "s"}`;
  const emptyDetail = state.assetFilter === "references" ? "Save a source URL to Scout." : state.assetFilter === "content_review" ? "No flagged local media is waiting for a content decision." : "Import a cleared video to populate the board.";
  $("#asset-grid").innerHTML = assets.length ? assets.map(assetCard).join("") : empty("Nothing here yet", emptyDetail);
  renderReviewToolbar();
  renderAssetInspector();
}

function isReviewableAsset(asset) {
  return asset.kind === "media" && Boolean(asset.local_path) && ["pending_review", "private_reference"].includes(asset.rights_status);
}

function renderReviewToolbar() {
  const count = state.selectedAssets.size;
  const selected = [...state.selectedAssets].map((id) => state.assets.find((asset) => asset.id === id)).filter(Boolean);
  const rightsCount = selected.filter(isReviewableAsset).length;
  const contentCount = selected.filter(isContentReviewableAsset).length;
  const mixed = rightsCount > 0 && contentCount > 0;
  $("#selected-asset-count").textContent = mixed ? count + " selected · choose one review type" : count + " selected";
  $$('[data-bulk-action]').forEach((button) => { button.disabled = rightsCount === 0 || mixed; });
  $$('[data-bulk-content-action]').forEach((button) => { button.disabled = contentCount === 0 || mixed; });
}

function renderProviderResults(results = null) {
  if (Array.isArray(results)) state.providerResults = results;
  results = state.providerResults;
  const visibleKeys = new Set(results.map((item) => `${item.provider}:${item.provider_id}`));
  state.selectedProviderIds = new Set([...state.selectedProviderIds].filter((key) => visibleKeys.has(key)));
  if (!results.length) { $("#provider-results").innerHTML = ""; return; }
  const selectedCount = state.selectedProviderIds.size;
  const controls = `<div class="provider-batch-actions"><span class="count-label">${selectedCount} selected</span><button class="mini-button" data-action="select-provider-results">Select all</button><button class="mini-button" data-action="clear-provider-results">Clear</button><button class="mini-button" data-action="provider-import-selected" ${selectedCount ? "" : "disabled"}>Import selected</button></div>`;
  const cards = results.map((item) => {
    const key = `${item.provider}:${item.provider_id}`;
    return `<article class="provider-result"><label class="provider-select"><input type="checkbox" data-provider-select="${escapeHtml(key)}" ${state.selectedProviderIds.has(key) ? "checked" : ""} /><span>Select</span></label><strong title="${escapeHtml(item.title)}">${escapeHtml(item.title)}</strong><small>${escapeHtml(item.provider)} · ${formatDuration(item.duration)} · ${item.width || "?"}x${item.height || "?"}</small><div class="asset-actions"><button class="mini-button" data-provider-save="${escapeHtml(item.provider_id)}">Save reference</button><button class="mini-button" data-provider-import="${escapeHtml(item.provider_id)}">Import for review</button></div></article>`;
  }).join("");
  $("#provider-results").innerHTML = controls + cards;
}

function isContentReviewableAsset(asset) {
  return asset.kind === "media" && Boolean(asset.local_path) && asset.content_risk?.status === "flagged";
}

function isSelectableAsset(asset) {
  return isReviewableAsset(asset) || isContentReviewableAsset(asset);
}

function assetCard(asset) {
  const preview = asset.local_path ? (String(asset.mime_type || "").startsWith("audio/") ? "♫" : `<video src="/api/assets/${asset.id}/stream" muted preload="metadata"></video>`) : "↗";
  const selector = isSelectableAsset(asset) ? `<label class="asset-select" title="Select for batch review"><input type="checkbox" data-asset-select="${asset.id}" ${state.selectedAssets.has(asset.id) ? "checked" : ""} /></label>` : "";
  const tags = (asset.tags || []).slice(0, 4).map((tag) => `<span class="tag">${escapeHtml(tag)}</span>`).join("");
  const canApprove = isReviewableAsset(asset);
  const healthBlocked = ["missing", "invalid", "changed"].includes(asset.health?.status);
  const contentBlocked = asset.content_risk?.status === "flagged";
  const canAdd = asset.kind === "media" && ["owned_distribution", "licensed_distribution"].includes(asset.rights_status) && ["ready", "approved"].includes(asset.status) && !healthBlocked && !contentBlocked;
  const providerImport = ["pexels", "pixabay"].includes(String(asset.platform || "").toLowerCase());
  const action = canApprove ? providerImport ? `<button class="mini-button" data-approve-licensed="${asset.id}">Approve license</button>` : `<button class="mini-button" data-approve-owned="${asset.id}">Declare owned</button>` : contentBlocked ? `<button class="mini-button" data-inspect-asset="${asset.id}">Review risk</button>` : canAdd ? `<button class="mini-button" data-add-asset="${asset.id}">Add to edit</button>` : `<span class="count-label">Reference only</span>`;
  const sourceAction = /^https?:\/\//i.test(asset.source_url || "") ? `<a class="mini-button mini-link" href="${escapeHtml(asset.source_url)}" target="_blank" rel="noreferrer">Open source</a>` : "";
  const flags = (asset.review_flags || []).map((flag) => `<span class="risk-flag">${escapeHtml(flag.replaceAll("-", " "))}</span>`).join("");
  const health = asset.health || {};
  const healthPill = health.status && !["unchecked", "reference"].includes(health.status) ? statusPill(health.status) : "";
  return `<article class="asset-card"><div class="asset-preview">${selector}${preview}</div><div class="asset-content"><strong title="${escapeHtml(asset.title)}">${escapeHtml(asset.title)}</strong><div class="asset-meta"><span>${asset.kind === "reference" ? escapeHtml(asset.platform || "reference") : `${formatDuration(asset.duration)} · ${formatBytes(asset.file_size)}`}</span><span>${statusPill(asset.rights_status)}${healthPill}</span></div><div class="asset-tags">${tags}${flags}</div><div class="asset-actions">${action}${sourceAction}<button class="mini-button" data-inspect-asset="${asset.id}">Inspect</button><button class="mini-button danger" data-reject-asset="${asset.id}">Reject</button></div></div></article>`;
}

function assetContentRiskDetail(asset) {
  const risk = asset.content_risk || { status: "clear", basis: "metadata", categories: [], matches: [] };
  const detail = risk.matches?.length ? `${risk.categories.join(", ")} · ${risk.matches.join(", ")}` : "No metadata risk terms detected";
  return `<div class="asset-detail"><span>Content scan</span><div>${statusPill(risk.status)}<small class="health-detail">${escapeHtml(detail)} · metadata only</small></div></div>`;
}

function assetReviewHistory(asset) {
  const reviews = state.assetReviews[asset.id];
  const loaded = Array.isArray(reviews);
  const toggle = `<button class="mini-button" type="button" data-review-history="${escapeHtml(asset.id)}">${loaded ? "Hide review history" : "Review history"}</button>`;
  if (!loaded) return `<div class="asset-review-history-control">${toggle}</div>`;
  const rows = reviews.length ? reviews.map((review) => `<div class="review-history-row"><div><strong>${statusPill(review.action)}</strong><small>${escapeHtml(review.created_at)}</small></div><div><span>${statusPill(review.previous_rights_status)} -&gt; ${statusPill(review.next_rights_status)}</span>${review.note ? `<small>${escapeHtml(review.note)}</small>` : ""}</div></div>`).join("") : `<span class="count-label">No decisions recorded</span>`;
  return `<div class="asset-review-history"><div class="asset-review-history-header"><div><span class="eyebrow">DECISION LOG</span><strong>Rights review history</strong></div>${toggle}</div>${rows}</div>`;
}

function assetReviewControls(asset) {
  if (asset.kind !== "media" || !asset.local_path || asset.rights_status === "blocked") return "";
  const reviewable = isReviewableAsset(asset);
  const providerImport = ["pexels", "pixabay"].includes(String(asset.platform || "").toLowerCase());
  const approveAction = providerImport ? "approve_licensed" : "approve_owned";
  const approveLabel = providerImport ? "Approve license" : "Declare owned";
  const approveButton = reviewable ? `<button class="button secondary" type="button" data-review-asset="${escapeHtml(asset.id)}" data-review-action="${approveAction}">${approveLabel}</button>` : "";
  const rejectButton = `<button class="button danger" type="button" data-review-asset="${escapeHtml(asset.id)}" data-review-action="reject">Reject asset</button>`;
  return `<div class="asset-review-controls"><div><span class="eyebrow">DECISION</span><strong>Record a curation decision</strong></div><label for="asset-review-note">Decision note<textarea id="asset-review-note" maxlength="300" rows="2" placeholder="Optional context for this decision"></textarea></label><div class="asset-review-actions">${approveButton}${rejectButton}</div></div>`;
}

function assetContentReviewControls(asset) {
  if (asset.kind !== "media" || !asset.local_path || asset.content_risk?.status !== "flagged") return "";
  const risk = asset.content_risk || {};
  const categories = (risk.categories || []).join(", ") || "metadata terms";
  return `<div class="asset-review-controls"><div><span class="eyebrow">CONTENT SAFETY</span><strong>Metadata risk requires a decision</strong><small class="form-note">Flagged categories: ${escapeHtml(categories)}. This scan uses descriptive records only; it does not inspect video frames.</small></div><label for="asset-content-review-note">Content note<textarea id="asset-content-review-note" maxlength="300" rows="2" placeholder="Why is this safe, or why should it stay blocked?"></textarea></label><div class="asset-review-actions"><button class="button secondary" type="button" data-content-review="${escapeHtml(asset.id)}" data-content-verdict="safe">Mark safe</button><button class="button danger" type="button" data-content-review="${escapeHtml(asset.id)}" data-content-verdict="flagged">Keep flagged</button></div></div>`;
}

function assetContentReviewHistory(asset) {
  const reviews = state.contentReviews[asset.id];
  const loaded = Array.isArray(reviews);
  const toggle = `<button class="mini-button" type="button" data-content-review-history="${escapeHtml(asset.id)}">${loaded ? "Hide content history" : "Content history"}</button>`;
  if (!loaded) return `<div class="asset-review-history-control">${toggle}</div>`;
  const rows = reviews.length ? reviews.map((review) => `<div class="review-history-row"><div><strong>${statusPill(review.verdict)}</strong><small>${escapeHtml(review.created_at)}</small></div><div><span>${escapeHtml((review.categories || []).join(", ") || "No categories")}</span>${review.matches?.length ? `<small>${escapeHtml(review.matches.join(", "))}</small>` : ""}${review.note ? `<small>${escapeHtml(review.note)}</small>` : ""}</div></div>`).join("") : `<span class="count-label">No content decisions recorded</span>`;
  return `<div class="asset-review-history"><div class="asset-review-history-header"><div><span class="eyebrow">DECISION LOG</span><strong>Content review history</strong></div>${toggle}</div>${rows}</div>`;
}

function renderAssetInspector() {
  const node = $("#asset-inspector");
  if (!node) return;
  const asset = state.assets.find((item) => item.id === state.inspectedAssetId);
  if (!asset) { node.hidden = true; node.innerHTML = ""; return; }
  const provenance = asset.provenance || {};
  const sourceUrl = /^https?:\/\//i.test(asset.source_url || "") ? asset.source_url : "";
  const fingerprint = provenance.fingerprint ? `${provenance.fingerprint.slice(0, 20)}...` : "Not analyzed";
  const flags = (asset.review_flags || []).map((flag) => `<span class="risk-flag">${escapeHtml(flag.replaceAll("-", " "))}</span>`).join("") || `<span class="count-label">No review flags</span>`;
  const health = asset.health || { status: "unchecked", problems: [] };
  const healthDetail = health.problems?.length ? health.problems.join("; ") : health.status === "passed" ? `Checked ${health.checked_at || ""}` : health.status === "unchecked" ? "Not checked yet" : "Reference record";
  node.hidden = false;
  node.innerHTML = `<div class="asset-inspector-header"><div><span class="eyebrow accent">SOURCE RECORD</span><h3>${escapeHtml(asset.title)}</h3><p>Inspect the origin, health, and curation metadata without changing the source file.</p></div><button class="text-button" type="button" data-action="close-asset-inspector">Close</button></div><div class="inspector-grid"><div class="asset-detail-list"><div class="asset-detail"><span>Origin</span><strong>${escapeHtml(provenance.source_type || "unknown")}</strong></div><div class="asset-detail"><span>Platform</span><strong>${escapeHtml(provenance.platform || "local")}</strong></div><div class="asset-detail"><span>Automation</span><strong>${escapeHtml(provenance.automation_id || "Manual import")}</strong></div><div class="asset-detail"><span>Fingerprint</span><code title="${escapeHtml(provenance.fingerprint || "")}">${escapeHtml(fingerprint)}</code></div><div class="asset-detail"><span>Local file</span><code>${escapeHtml(provenance.local_path || "Reference only")}</code></div><div class="asset-detail"><span>Source URL</span>${sourceUrl ? `<a class="inspector-link" href="${escapeHtml(sourceUrl)}" target="_blank" rel="noreferrer">${escapeHtml(sourceUrl)}</a>` : `<strong>${escapeHtml(asset.source_url || "No source URL")}</strong>`}</div><div class="asset-detail"><span>Media health</span><div>${statusPill(health.status)}<small class="health-detail">${escapeHtml(healthDetail)}</small></div></div><div class="asset-flags"><span>Review flags</span><div>${flags}</div></div></div><form class="inspector-form" id="asset-inspector-form" data-asset-id="${escapeHtml(asset.id)}"><label>Title<input name="title" value="${escapeHtml(asset.title)}" maxlength="160" required /></label><label>Tags<input name="tags" value="${escapeHtml((asset.tags || []).join(", "))}" placeholder="cute, funny, reaction" /></label><label>Source URL<input name="source_url" type="url" value="${escapeHtml(asset.source_url || "")}" placeholder="https://..." /></label><button class="button secondary full" type="submit">Save curation</button><button class="button secondary full" type="button" data-health-asset="${escapeHtml(asset.id)}">Recheck media</button><button class="button danger full" type="button" data-delete-asset="${escapeHtml(asset.id)}">Delete source record</button><p class="form-note">Rights decisions stay in the review queue. This form only edits descriptive metadata.</p></form></div></div>${assetReviewControls(asset)}${assetReviewHistory(asset)}`;
  node.insertAdjacentHTML("beforeend", assetContentReviewControls(asset) + assetContentReviewHistory(asset));
  $(".asset-detail-list", node)?.insertAdjacentHTML("beforeend", assetContentRiskDetail(asset));
}

function productionAudioAssets() {
  return state.assets.filter((asset) => String(asset.mime_type || "").startsWith("audio/") && ["owned_distribution", "licensed_distribution"].includes(asset.rights_status) && ["ready", "approved"].includes(asset.status));
}

function audioOptions(selectedId, emptyLabel) {
  return `<option value="">${escapeHtml(emptyLabel)}</option>${productionAudioAssets().map((asset) => `<option value="${escapeHtml(asset.id)}" ${asset.id === selectedId ? "selected" : ""}>${escapeHtml(asset.title)} · ${formatDuration(asset.duration)}</option>`).join("")}`;
}

function renderAudioSelect() {
  const selects = [$("#music-asset-select"), $("#sfx-asset-select"), $("#voiceover-asset-select"), $("#automation-music-select"), $("#automation-sfx-select"), $("#automation-voiceover-select")].filter(Boolean);
  if (!selects.length) return;
  const emptyLabels = { "music-asset-select": "No music bed", "sfx-asset-select": "No sound effect", "voiceover-asset-select": "No voiceover", "automation-music-select": "No music bed", "automation-sfx-select": "No sound effect", "automation-voiceover-select": "No voiceover" };
  selects.forEach((select) => {
    const current = select.value;
    select.innerHTML = audioOptions(current, emptyLabels[select.id] || "No audio");
    if (productionAudioAssets().some((asset) => asset.id === current)) select.value = current;
  });
}

function renderProjects() {
  $("#project-count").textContent = `${state.projects.length} project${state.projects.length === 1 ? "" : "s"}`;
  $("#project-list").innerHTML = state.projects.length ? state.projects.map(projectRow).join("") : empty("No edit briefs", "Create a project to begin planning a cut.");
  if (state.selectedProject) {
    const fresh = state.projects.find((project) => project.id === state.selectedProject.id);
    if (fresh) { state.selectedProject = fresh; renderInspector(fresh); }
  }
}

function renderInspector(project) {
  state.selectedProject = project;
  const clips = project.assets || [];
  const audio = project.audio || [];
  const manifest = project.manifest || {};
  const settings = project.settings || {};
  const track = (role) => audio.find((item) => item.role === role) || {};
  const music = track("music");
  const sfx = track("sfx");
  const voiceover = track("voiceover");
  const clipMarkup = clips.map((clip, index) => { const clipOut = clip.out_point ?? clip.duration; const canMoveUp = index > 0; const canMoveDown = index < clips.length - 1; return `<div class="timeline-clip ${clip.enabled ? "" : "disabled"}" data-project-id="${project.id}" data-clip-id="${clip.asset_id}"><b>${String(index + 1).padStart(2, "0")}</b><strong title="${escapeHtml(clip.title)}">${escapeHtml(clip.title)}</strong><div class="clip-controls"><label>In<input class="clip-in" type="number" min="0" max="${clip.duration}" step="0.1" value="${clip.in_point}" /></label><label>Out<input class="clip-out" type="number" min="0.1" max="${clip.duration}" step="0.1" value="${clipOut}" /></label></div><small>${formatDuration(clipOut - clip.in_point)} · ${escapeHtml(clip.selected_by)}</small>${clip.note ? `<small class="clip-note">${escapeHtml(clip.note)}</small>` : ""}<div class="clip-actions"><button class="mini-button" data-save-clip="${clip.asset_id}">Save</button><button class="mini-button" data-toggle-clip="${clip.asset_id}" data-enabled="${clip.enabled ? "0" : "1"}">${clip.enabled ? "Disable" : "Enable"}</button><button class="mini-button" data-move-clip="${clip.asset_id}" data-direction="up" title="Move clip earlier" ${canMoveUp ? "" : "disabled"}>↑</button><button class="mini-button" data-move-clip="${clip.asset_id}" data-direction="down" title="Move clip later" ${canMoveDown ? "" : "disabled"}>↓</button><button class="mini-button danger" data-remove-clip="${clip.asset_id}" title="Remove clip">×</button></div></div>`; }).join("");
  const audioSummary = audio.length ? audio.map((track) => `${escapeHtml(track.role)}: ${escapeHtml(track.title)} @ ${track.start_time}s`).join(" · ") : "No added audio tracks";
  const settingsMarkup = `<form class="project-settings" id="project-settings-form" data-project-id="${escapeHtml(project.id)}"><div class="panel-heading"><div><span class="eyebrow">EDIT BRIEF</span><h3>Project settings</h3></div><span class="count-label">${escapeHtml(project.status)}</span></div><div class="form-row"><label>Name<input name="name" value="${escapeHtml(project.name)}" maxlength="120" required /></label><label>Theme<input name="theme" value="${escapeHtml(project.theme)}" maxlength="180" required /></label></div><div class="form-row"><label>Target seconds<input name="duration_target" type="number" min="8" max="180" step="1" value="${escapeHtml(project.duration_target)}" /></label><label>Style<select name="style"><option value="kinetic" ${project.style === "kinetic" ? "selected" : ""}>Kinetic</option><option value="clean" ${project.style === "clean" ? "selected" : ""}>Clean captions</option><option value="narrated" ${project.style === "narrated" ? "selected" : ""}>Narrated</option></select></label></div><div class="form-row"><label>On-screen title<input name="title" value="${escapeHtml(project.title || project.name)}" maxlength="100" /></label><label>Caption / subtitle<input name="caption" value="${escapeHtml(project.caption || "")}" maxlength="500" /></label></div><div class="form-row treatment-options"><label class="check-row"><input name="add_title_card" type="checkbox" ${settings.add_title_card !== false ? "checked" : ""} />Apply title treatment</label><label class="check-row"><input name="write_captions" type="checkbox" ${settings.write_captions !== false ? "checked" : ""} />Write captions sidecar</label></div><div class="project-audio-grid"><label>Music bed<select name="music_asset_id">${audioOptions(music.asset_id || "", "No music bed")}</select></label><label>Music volume<input name="music_volume" type="range" min="0" max="1" step="0.05" value="${escapeHtml(music.volume ?? 0.16)}" /></label><label>Sound effect<select name="sfx_asset_id">${audioOptions(sfx.asset_id || "", "No sound effect")}</select></label><label>Effect start seconds<input name="sfx_start_time" type="number" min="0" max="3600" step="0.1" value="${escapeHtml(sfx.start_time ?? 0)}" /></label><label>Effect volume<input name="sfx_volume" type="range" min="0" max="1" step="0.05" value="${escapeHtml(sfx.volume ?? 0.55)}" /></label><label>Voiceover<select name="voiceover_asset_id">${audioOptions(voiceover.asset_id || "", "No voiceover")}</select></label><label>Voiceover start seconds<input name="voiceover_start_time" type="number" min="0" max="3600" step="0.1" value="${escapeHtml(voiceover.start_time ?? 0)}" /></label><label>Voiceover volume<input name="voiceover_volume" type="range" min="0" max="1" step="0.05" value="${escapeHtml(voiceover.volume ?? 1)}" /></label></div><button class="button secondary" type="submit">Save project settings</button><p class="form-note">Saving invalidates previous exports; render again before publishing.</p></form>`;
  $("#project-inspector").innerHTML = `<div class="inspector-header"><div><span class="eyebrow accent">EDIT INSPECTOR</span><h3>${escapeHtml(project.name)}</h3><p>${escapeHtml(project.theme)} · ${formatDuration(project.duration_target)} target · ${clips.length} clips</p></div><div class="inspector-actions"><button class="button secondary" data-plan-project="${project.id}">✦ Plan edit</button><button class="button primary" data-render-project="${project.id}">▶ Render</button><button class="button danger" data-delete-project="${project.id}">Delete</button></div></div>${settingsMarkup}<div class="timeline"><div class="panel-heading"><div><span class="eyebrow">TIMELINE</span><h3>Clips</h3></div><span class="count-label">${clips.length} clips</span></div>${clips.length ? clipMarkup : empty("No clips selected", "Plan automatically or add clips from Scout.")}</div><div class="manifest"><strong>Planner</strong> ${escapeHtml(manifest.planner || "Manual")}${manifest.selected_duration ? ` · ${formatDuration(manifest.selected_duration)} selected` : ""}<div class="progress-track"><i style="width:${project.status === "ready" ? 100 : 0}%"></i></div></div><div class="sound-summary"><strong>Sound</strong><span>${audioSummary}</span></div>`;
}

function renderOutputs() {
  const ready = state.projects.filter((project) => project.status === "ready" && project.output_dir);
  $("#output-list").innerHTML = ready.length ? ready.map((project) => { const validation = project.manifest?.validation || {}; const validationStatus = validation.status || "unverified"; const outputCount = Object.keys(validation.outputs || {}).length; const validateAction = validationStatus === "passed" ? "" : `<button class="button secondary" data-validate-output="${project.id}">Validate package</button>`; return `<article class="output-card"><div class="output-video"><video controls preload="metadata" src="/api/projects/${project.id}/output/final.mp4"></video></div><div class="output-info"><div class="output-status"><span class="eyebrow accent">${escapeHtml(project.style)} / 9:16</span>${statusPill(validationStatus)}</div><h3>${escapeHtml(project.title || project.name)}</h3><p>${escapeHtml(project.caption || project.theme)}</p><div class="output-files"><span class="file-chip">final.mp4</span><span class="file-chip">captions.srt</span><span class="file-chip">youtube.mp4</span><span class="file-chip">instagram.mp4</span><span class="file-chip">tiktok.mp4</span></div><p class="output-health">${outputCount ? `${outputCount} outputs validated` : "Output validation predates this check"}</p><div class="output-actions">${validateAction}<button class="button primary" data-publish-project="${project.id}">↗ Prepare publish</button><button class="button secondary" data-copy-path="${escapeHtml(project.output_dir)}">Copy package path</button></div></div></article>`; }).join("") : empty("No rendered outputs", "Your first finished edit will appear here.");
}

function renderPublishAccountPickers() {
  const container = $("#publish-account-picks");
  const form = $("#publish-form");
  if (!container || !form) return;
  const currentSelections = {};
  $$(".publish-account-select", container).forEach((select) => { currentSelections[select.dataset.platform] = select.value; });
  state.publishDraft.accountIds = { ...(state.publishDraft.accountIds || {}), ...currentSelections };
  const selectedPlatforms = $$("input[name='platform']:checked", form).map((input) => input.value);
  const selectedSet = new Set(selectedPlatforms);
  Object.keys(state.publishDraft.accountIds).forEach((platform) => { if (!selectedSet.has(platform)) delete state.publishDraft.accountIds[platform]; });
  container.innerHTML = selectedPlatforms.map((platform) => {
    const accounts = state.accounts.filter((account) => account.platform === platform);
    const selectedId = accounts.some((account) => account.id === state.publishDraft.accountIds[platform]) ? state.publishDraft.accountIds[platform] : "";
    if (selectedId) state.publishDraft.accountIds[platform] = selectedId;
    else delete state.publishDraft.accountIds[platform];
    const label = state.platforms.find((item) => item.id === platform)?.label || platform;
    const options = accounts.length ? [`<option value="">Auto-select first configured account</option>`, ...accounts.map((account) => {
      const readiness = account.readiness?.ready ? "ready" : "needs setup";
      return `<option value="${escapeHtml(account.id)}" ${account.id === selectedId ? "selected" : ""}>${escapeHtml(account.label)} · ${escapeHtml(readiness)}</option>`;
    })].join("") : `<option value="">No account configured</option>`;
    return `<label>${escapeHtml(label)} account<select class="publish-account-select" data-platform="${escapeHtml(platform)}" name="account_id_${escapeHtml(platform)}" ${accounts.length ? "" : "disabled"}>${options}</select></label>`;
  }).join("");
}

function renderPublisher() {
  const projectSelect = $("#publish-project");
  if (projectSelect) {
    const readyProjects = state.projects.filter((project) => project.status === "ready" && project.output_dir);
    const currentProject = state.publishDraft.projectId || projectSelect.value;
    projectSelect.innerHTML = readyProjects.length ? readyProjects.map((project) => `<option value="${escapeHtml(project.id)}">${escapeHtml(project.title || project.name)}</option>`).join("") : `<option value="">No rendered projects</option>`;
    const selectedProject = readyProjects.find((project) => project.id === currentProject) || readyProjects[0];
    state.publishDraft.projectId = selectedProject?.id || "";
    projectSelect.value = state.publishDraft.projectId;
  }
  renderPlatformReadiness();
  $("#account-list").innerHTML = state.accounts.length ? state.accounts.map((account) => {
    const tokenEnv = account.config?.token_env || "token not configured";
    const readiness = account.readiness || {};
    const check = account.connection_check || {};
    const baseDetail = readiness.ready ? (account.status === "connected" ? `token detected · ${tokenEnv}` : `set ${tokenEnv}`) : (readiness.issues || [`set ${tokenEnv}`]).join(" · ");
    const detail = !readiness.ready ? baseDetail : check.status === "passed" ? `verified as ${check.identity || account.platform}` : check.status === "failed" ? `check failed · ${check.message || "unknown error"}` : check.status === "blocked" ? `check blocked · ${check.message || baseDetail}` : baseDetail;
    const status = readiness.ready && check.status === "passed" ? "verified" : readiness.ready && check.status === "failed" ? "check_failed" : readiness.ready ? account.status : "needs_configuration";
    return `<div class="account-row"><div class="row-icon">◎</div><div class="row-main"><strong>${escapeHtml(account.label)}</strong><small>${escapeHtml(account.platform)} · ${escapeHtml(detail)}</small></div>${statusPill(status)}<button class="row-action" data-check-account="${escapeHtml(account.id)}">Check</button><button class="row-action" data-edit-account="${escapeHtml(account.id)}">Edit</button><button class="row-action danger-action" data-delete-account="${escapeHtml(account.id)}">Delete</button></div>`;
  }).join("") : empty("No channels added", "Add a destination to prepare your outbox.");
  renderPublishAccountPickers();
  $("#publish-count").textContent = `${state.publishingJobs.filter((job) => ["queued", "ready"].includes(job.status)).length} queued`;
  $("#publish-list").innerHTML = state.publishingJobs.length ? state.publishingJobs.map((job) => {
    const sourceCount = Number(job.payload?.provenance?.asset_ids?.length || 0);
    const provenance = sourceCount ? ` · ${sourceCount} credited source${sourceCount === 1 ? "" : "s"}` : "";
    const processing = Boolean(job.processing);
    const account = state.accounts.find((item) => item.id === job.account_id);
    const accountDetail = account ? ` · ${account.label}` : " · no account selected";
    const attempts = state.publishAttempts[job.id];
    const history = attempts ? `<div class="publish-history">${attempts.length ? attempts.map((attempt) => `<div class="attempt-row"><div class="row-main"><strong>Attempt ${escapeHtml(attempt.attempt_number)}</strong><small>${escapeHtml(attempt.trigger)} · ${escapeHtml(attempt.started_at)}${attempt.finished_at ? ` · finished ${escapeHtml(attempt.finished_at)}` : ""}${attempt.remote_id ? ` · ${escapeHtml(attempt.remote_id)}` : ""}${attempt.error ? ` · ${escapeHtml(attempt.error)}` : ""}</small></div>${statusPill(attempt.status)}</div>`).join("") : empty("No attempts yet", "This outbox row has not reached a publisher adapter.")}</div>` : "";
    const historyButton = `<button class="row-action" data-publish-history="${job.id}">${attempts ? "Hide history" : "History"}</button>`;
    const editable = !processing && ["ready", "failed"].includes(job.status);
    const scheduleButton = editable ? `<button class="row-action" data-edit-schedule="${job.id}">Schedule</button>` : "";
    const cancelButton = editable ? `<button class="row-action danger-action" data-cancel-publish="${job.id}">Cancel</button>` : "";
    const markPosted = ["ready", "drafted"].includes(job.status) ? `<button class="row-action" data-mark-published="${job.id}">Mark posted</button>` : "";
    const action = processing ? "" : job.status === "ready" ? `<button class="row-action" data-run-publish="${job.id}">Publish</button>${markPosted}` : job.status === "drafted" ? markPosted : job.status === "failed" ? `<button class="row-action" data-run-publish="${job.id}">Retry</button>` : "";
    const scheduleEditor = state.publishScheduleJobId === job.id && editable ? `<div class="publish-schedule"><input class="publish-schedule-input" type="datetime-local" value="${escapeHtml(toDatetimeLocal(job.scheduled_for))}" /><button class="row-action" data-save-schedule="${job.id}">Save</button><button class="row-action" data-cancel-schedule="${job.id}">Close</button></div>` : "";
    return `<div class="publish-entry"><div class="publish-row"><div class="row-icon">↗</div><div class="row-main"><strong>${escapeHtml(job.payload?.title || job.platform)}</strong><small>${escapeHtml(job.platform)}${escapeHtml(accountDetail)} · ${job.scheduled_for ? `scheduled ${escapeHtml(job.scheduled_for)}` : "ready for account connection"}${provenance}${job.error ? ` · ${escapeHtml(job.error)}` : ""}</small></div>${processing ? statusPill("publishing") : statusPill(job.status)}${historyButton}${scheduleButton}${cancelButton}${action}</div>${scheduleEditor}${history}</div>`;
  }).join("") : empty("Outbox is empty", "Rendered projects can be prepared for distribution.");
}

function toDatetimeLocal(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const pad = (number) => String(number).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function renderPlatformReadiness() {
  const node = $("#platform-readiness");
  if (!node) return;
  node.innerHTML = state.platforms.length ? state.platforms.map((platform) => `<div class="platform-status"><div class="row-main"><strong>${escapeHtml(platform.label)}</strong><small>${escapeHtml(String(platform.mode || "").replaceAll("_", " "))} · ${escapeHtml(platform.token_env)}</small></div>${statusPill(platform.configured ? "configured" : "not_configured")}</div>`).join("") : empty("No platform adapters", "Platform capabilities are unavailable.");
}

function formatInterval(minutes) {
  const value = Number(minutes || 0);
  if (!value) return "manual";
  if (value < 60) return `every ${value} min`;
  const hours = value / 60;
  return Number.isInteger(hours) ? `every ${hours} hr` : `every ${value} min`;
}

function renderAutomationAccountPickers() {
  const container = $("#automation-account-picks");
  const form = $("#automation-form");
  if (!container || !form) return;
  const currentSelections = {};
  $$(".automation-account-select", container).forEach((select) => { currentSelections[select.dataset.platform] = select.value; });
  state.automationDraft.accountIds = { ...(state.automationDraft.accountIds || {}), ...currentSelections };
  const selectedPlatforms = $$('input[name="platforms"]:checked', form).map((input) => input.value);
  const selectedSet = new Set(selectedPlatforms);
  Object.keys(state.automationDraft.accountIds).forEach((platform) => { if (!selectedSet.has(platform)) delete state.automationDraft.accountIds[platform]; });
  container.innerHTML = selectedPlatforms.map((platform) => {
    const accounts = state.accounts.filter((account) => account.platform === platform);
    const selectedId = accounts.some((account) => account.id === state.automationDraft.accountIds[platform]) ? state.automationDraft.accountIds[platform] : "";
    if (selectedId) state.automationDraft.accountIds[platform] = selectedId;
    else delete state.automationDraft.accountIds[platform];
    const label = state.platforms.find((item) => item.id === platform)?.label || platform;
    const options = accounts.length ? [`<option value="">Auto-select first configured account</option>`, ...accounts.map((account) => {
      const readiness = account.readiness?.ready ? "ready" : "needs setup";
      return `<option value="${escapeHtml(account.id)}" ${account.id === selectedId ? "selected" : ""}>${escapeHtml(account.label)} · ${escapeHtml(readiness)}</option>`;
    })].join("") : `<option value="">No account configured</option>`;
    return `<label>${escapeHtml(label)} account<select class="automation-account-select" data-platform="${escapeHtml(platform)}" name="account_id_${escapeHtml(platform)}" ${accounts.length ? "" : "disabled"}>${options}</select></label>`;
  }).join("");
}

function renderAutomations() {
  const list = $("#automation-list");
  if (!list) return;
  $("#automation-count").textContent = `${state.automations.length} recipe${state.automations.length === 1 ? "" : "s"}`;
  list.innerHTML = state.automations.length ? state.automations.map((automation) => {
    const running = ["queued", "running"].includes(automation.status);
    const toggleLabel = automation.enabled ? "Pause" : "Enable";
    const lastRun = automation.last_run_at ? `last ${escapeHtml(automation.last_run_at.replace("T", " "))}` : "never run";
    const source = automation.source_mode === "provider" ? `${automation.provider}: ${automation.search_query}` : automation.source_mode === "watch_provider" ? `${automation.watch_dir} + ${automation.provider}` : automation.watch_dir;
    const detail = `${automation.source_mode.replaceAll("_", " + ")} · ${formatInterval(automation.interval_minutes)} · ${automation.default_rights.replaceAll("_", " ")} · ${lastRun}`;
    return `<div class="automation-row"><div class="row-icon">↻</div><div class="row-main"><strong>${escapeHtml(automation.name)}</strong><small>${escapeHtml(source)} · ${escapeHtml(detail)}${automation.last_error ? ` · ${escapeHtml(automation.last_error)}` : ""}</small></div>${statusPill(automation.status)}<button class="row-action" data-inspect-automation="${automation.id}">History</button><button class="row-action" data-edit-automation="${automation.id}" ${running ? "disabled" : ""}>Edit</button><button class="row-action" data-run-automation="${automation.id}" ${running ? "disabled" : ""}>Run</button><button class="row-action" data-toggle-automation="${automation.id}">${toggleLabel}</button><button class="row-action danger-action" data-delete-automation="${automation.id}">Delete</button></div>`;
  }).join("") : empty("No automations yet", "Create a recipe to watch a local folder and build repeatable releases.");
  renderAutomationAccountPickers();
  renderAutomationRuns();
}

function renderAutomationRuns() {
  const panel = $("#automation-history-panel");
  const automation = state.automations.find((item) => item.id === state.selectedAutomationId);
  if (!panel || !automation) {
    if (panel) { panel.hidden = true; panel.innerHTML = ""; }
    return;
  }
  const runs = state.automationRuns || [];
  const runRows = runs.length ? runs.map((run) => {
    const scan = run.scan || {};
    const scanSummary = `found ${Number(scan.found || 0)} · imported ${Number((scan.imported || []).length)} · duplicates ${Number((scan.duplicates || []).length)} · errors ${Number((scan.errors || []).length)}`;
    const resultSummary = run.error || run.result?.message || (run.project_id ? `project ${run.project_id}` : "no project");
    const openProject = run.project_id ? `<button class="row-action" data-project="${escapeHtml(run.project_id)}">Open project</button>` : "";
    const reviewImported = (scan.imported || []).length ? `<button class="row-action" data-review-automation="${escapeHtml(run.automation_id)}">Review imported</button>` : "";
    return `<div class="automation-row"><div class="row-icon">↻</div><div class="row-main"><strong>${escapeHtml(run.started_at.replace("T", " "))} · ${escapeHtml(run.id)}</strong><small>${escapeHtml(scanSummary)} · ${escapeHtml(resultSummary)}</small></div>${statusPill(run.status)}${reviewImported}${openProject}</div>`;
  }).join("") : empty("No runs yet", "Run this recipe to build its first report.");
  panel.hidden = false;
  panel.innerHTML = `<div class="panel-heading"><div><span class="eyebrow accent">RUN HISTORY</span><h3>${escapeHtml(automation.name)}</h3><span class="count-label">${runs.length} recorded run${runs.length === 1 ? "" : "s"}</span></div><button class="text-button" type="button" data-action="clear-automation-runs">Close</button></div><div class="automation-list">${runRows}</div>`;
}

async function inspectAutomationRuns(automationId) {
  state.selectedAutomationId = automationId;
  state.automationRuns = (await api(`/api/automations/${automationId}/runs`)).runs || [];
  showView("automations");
  renderAutomationRuns();
}

async function submitUpload(form) {
  const button = $("button[type=submit]", form); button.disabled = true;
  try {
    const payload = new FormData(form);
    const result = await api("/api/assets/upload", { method: "POST", body: payload });
    toast(`${result.asset.title} imported`); form.reset(); $("#file-label").textContent = "Choose a video"; await refresh();
  } catch (error) { toast(error.message, true); } finally { button.disabled = false; }
}

async function submitAudioUpload(form) {
  const button = $("button[type=submit]", form); button.disabled = true;
  try { const result = await api("/api/assets/audio-upload", { method: "POST", body: new FormData(form) }); toast(`${result.asset.title} added to audio library`); form.reset(); await refresh(); } catch (error) { toast(error.message, true); } finally { button.disabled = false; }
}

function resetAutomationForm() {
  const form = $("#automation-form");
  if (!form) return;
  state.editingAutomationId = "";
  state.automationDraft.accountIds = {};
  form.reset();
  $("#automation-form-eyebrow").textContent = "AUTOMATION RECIPE";
  $("#automation-form-title").textContent = "New recipe";
  $("#automation-submit").textContent = "Create automation";
  $("#automation-cancel").hidden = true;
  updateAutomationSourceFields();
  renderAutomationAccountPickers();
  renderAudioSelect();
}

function updateAutomationSourceFields() {
  const mode = $("#automation-source-mode")?.value || "watch";
  const watch = $("#automation-watch-config");
  const provider = $("#automation-provider-config");
  const watchInput = $("input[name=watch_dir]");
  const queryInput = $("input[name=search_query]");
  if (watch) watch.hidden = mode === "provider";
  if (provider) provider.hidden = mode === "watch";
  if (watchInput) watchInput.required = mode !== "provider";
  if (queryInput) queryInput.required = mode !== "watch";
}

function editAutomation(automationId) {
  const automation = state.automations.find((item) => item.id === automationId);
  const form = $("#automation-form");
  if (!automation || !form) return;
  state.editingAutomationId = automation.id;
  $("input[name=name]", form).value = automation.name;
  $("input[name=watch_dir]", form).value = automation.watch_dir;
  $("select[name=source_mode]", form).value = automation.source_mode || "watch";
  $("select[name=provider]", form).value = automation.provider || "pexels";
  $("input[name=provider_limit]", form).value = automation.provider_limit || 12;
  $("input[name=search_query]", form).value = automation.search_query || "";
  $("input[name=theme]", form).value = automation.theme;
  $("input[name=duration_target]", form).value = automation.duration_target;
  $("select[name=style]", form).value = automation.style;
  $("input[name=interval_minutes]", form).value = automation.interval_minutes;
  $("input[name=publish_delay_minutes]", form).value = automation.publish_delay_minutes;
  $("input[name=tags]", form).value = automation.tags.join(", ");
  $("input[name=title_template]", form).value = automation.title_template;
  $("textarea[name=caption_template]", form).value = automation.caption_template;
  $("select[name=default_rights]", form).value = automation.default_rights;
  $("#automation-music-select").value = automation.music_asset_id || "";
  $("input[name=music_volume]", form).value = automation.music_volume ?? 0.16;
  $("#automation-sfx-select").value = automation.sfx_asset_id || "";
  $("input[name=sfx_start_time]", form).value = automation.sfx_start_time ?? 0;
  $("input[name=sfx_volume]", form).value = automation.sfx_volume ?? 0.55;
  $("#automation-voiceover-select").value = automation.voiceover_asset_id || "";
  $("input[name=voiceover_start_time]", form).value = automation.voiceover_start_time ?? 0;
  $("input[name=voiceover_volume]", form).value = automation.voiceover_volume ?? 1;
  $$('input[name="platforms"]', form).forEach((input) => { input.checked = automation.platforms.includes(input.value); });
  state.automationDraft.accountIds = { ...(automation.account_ids || {}) };
  $("input[name=auto_render]", form).checked = automation.auto_render;
  $("input[name=enabled]", form).checked = automation.enabled;
  $("#automation-form-eyebrow").textContent = "EDIT AUTOMATION";
  $("#automation-form-title").textContent = automation.name;
  $("#automation-submit").textContent = "Save automation";
  $("#automation-cancel").hidden = false;
  updateAutomationSourceFields();
  showView("automations");
  renderAutomationAccountPickers();
}

async function submitAutomation(form) {
  const button = $("#automation-submit");
  button.disabled = true;
  try {
    const data = new FormData(form);
    const payload = Object.fromEntries(data);
    payload.platforms = data.getAll("platforms").map(String);
    const accountIds = {};
    payload.platforms.forEach((platform) => {
      const accountId = String(data.get(`account_id_${platform}`) || "");
      if (accountId) accountIds[platform] = accountId;
    });
    payload.account_ids = accountIds;
    state.automationDraft.accountIds = accountIds;
    payload.tags = String(data.get("tags") || "").split(",");
    payload.duration_target = Number(data.get("duration_target") || 35);
    payload.interval_minutes = Number(data.get("interval_minutes") || 0);
    payload.publish_delay_minutes = Number(data.get("publish_delay_minutes") || 0);
    payload.music_volume = Number(data.get("music_volume") ?? 0.16);
    payload.sfx_start_time = Number(data.get("sfx_start_time") ?? 0);
    payload.sfx_volume = Number(data.get("sfx_volume") ?? 0.55);
    payload.voiceover_start_time = Number(data.get("voiceover_start_time") ?? 0);
    payload.voiceover_volume = Number(data.get("voiceover_volume") ?? 1);
    payload.auto_render = data.has("auto_render");
    payload.enabled = data.has("enabled");
    const endpoint = state.editingAutomationId ? `/api/automations/${state.editingAutomationId}` : "/api/automations";
    await api(endpoint, { method: state.editingAutomationId ? "PATCH" : "POST", body: JSON.stringify(payload) });
    toast(state.editingAutomationId ? "Automation saved" : "Automation created");
    resetAutomationForm();
    await refresh();
  } catch (error) { toast(error.message, true); } finally { button.disabled = false; }
}

async function submitPublish(form) {
  const data = new FormData(form);
  const projectId = String(data.get("project_id") || "");
  const platforms = data.getAll("platform").map(String);
  if (!projectId) throw new Error("Render a project before preparing a release");
  if (!platforms.length) throw new Error("Choose at least one destination");
  let scheduledFor = "";
  const scheduledInput = String(data.get("scheduled_for") || "");
  if (scheduledInput) {
    const scheduledDate = new Date(scheduledInput);
    if (Number.isNaN(scheduledDate.getTime())) throw new Error("Schedule time is invalid");
    scheduledFor = scheduledDate.toISOString();
  }
  const accountIds = {};
  platforms.forEach((platform) => {
    const accountId = String(data.get(`account_id_${platform}`) || "");
    if (accountId) accountIds[platform] = accountId;
  });
  state.publishDraft.accountIds = accountIds;
  const result = await api(`/api/projects/${projectId}/publish`, { method: "POST", body: JSON.stringify({ platforms, scheduled_for: scheduledFor, account_ids: accountIds }) });
  state.publishDraft.projectId = projectId;
  toast(`${result.jobs.length} package${result.jobs.length === 1 ? "" : "s"} prepared`);
  await refresh();
}

async function submitBulkReview(action) {
  if (!state.selectedAssets.size) { toast("Select at least one asset", true); return; }
  const labels = { approve_licensed: "Provider licenses approved", approve_owned: "Ownership attested", reject: "Assets rejected" };
  const selected = [...state.selectedAssets].map((id) => state.assets.find((asset) => asset.id === id)).filter(Boolean);
  const reviewedIds = selected.filter(isReviewableAsset).map((asset) => asset.id);
  if (!reviewedIds.length || reviewedIds.length !== selected.length) { toast("Select only rights-review assets for this action", true); return; }
  const note = $("#bulk-review-note")?.value || "";
  const result = await api("/api/assets/bulk", { method: "PATCH", body: JSON.stringify({ ids: reviewedIds, action, note }) });
  reviewedIds.forEach((id) => { delete state.assetReviews[id]; });
  if ($("#bulk-review-note")) $("#bulk-review-note").value = "";
  state.selectedAssets.clear();
  toast(`${result.assets.length} asset${result.assets.length === 1 ? "" : "s"} updated: ${labels[action] || "Review updated"}`);
  await refresh();
}

async function submitBulkContentReview(verdict) {
  if (!state.selectedAssets.size) { toast("Select at least one asset", true); return; }
  const selected = [...state.selectedAssets].map((id) => state.assets.find((asset) => asset.id === id)).filter(Boolean);
  const reviewedIds = selected.filter(isContentReviewableAsset).map((asset) => asset.id);
  if (!reviewedIds.length || reviewedIds.length !== selected.length) { toast("Select only content-risk assets for this action", true); return; }
  const note = $("#bulk-review-note")?.value || "";
  const result = await api("/api/assets/content-review/bulk", { method: "PATCH", body: JSON.stringify({ ids: reviewedIds, verdict, note }) });
  reviewedIds.forEach((id) => { delete state.contentReviews[id]; });
  if ($("#bulk-review-note")) $("#bulk-review-note").value = "";
  state.selectedAssets.clear();
  toast(result.assets.length + " asset" + (result.assets.length === 1 ? "" : "s") + " content decision recorded");
  await refresh();
}

async function handleClick(event) {
  const target = event.target.closest("button"); if (!target) return;
  const { view, viewTarget, action, bulkAction, bulkContentAction, project, planProject, renderProject, approveLicensed, approveOwned, rejectAsset, inspectAsset, healthAsset, deleteAsset, reviewHistory, contentReviewHistory, contentReview, contentVerdict, reviewAsset, reviewAction, validateOutput, addAsset, publishProject, markPublished, runPublish, publishHistory, checkAccount, editAccount, deleteAccount, editSchedule, cancelSchedule, saveSchedule, cancelPublish, reviewAutomation, providerSave, providerImport, saveClip, toggleClip, moveClip, removeClip, direction, projectId, enabled, runAutomation, toggleAutomation, deleteAutomation, deleteProject, retryJob, inspectAutomation, editAutomation: editAutomationId } = target.dataset;
  try {
    if (view) { showView(view); return; }
    if (viewTarget) { showView(viewTarget); return; }
    if (action === "refresh") { await refresh(); toast("Workspace refreshed"); return; }
    if (action === "health-check") { const ids = [...state.selectedAssets]; const result = await api("/api/assets/health-check", { method: "POST", body: JSON.stringify(ids.length ? { ids } : {}) }); toast(`Checked ${result.report.checked} media: ${result.report.passed} healthy, ${result.report.missing + result.report.invalid + result.report.changed} flagged`); await refresh(); return; }
    if (action === "select-visible") { const selectable = state.assetFilter === "content_review" ? isContentReviewableAsset : isReviewableAsset; libraryAssets().filter(selectable).forEach((asset) => state.selectedAssets.add(asset.id)); renderAssets(); return; }
    if (action === "clear-selection") { state.selectedAssets.clear(); renderAssets(); return; }
    if (action === "clear-automation-runs") { state.selectedAutomationId = ""; state.automationRuns = []; renderAutomationRuns(); return; }
    if (action === "select-provider-results") { state.selectedProviderIds = new Set(state.providerResults.map((item) => `${item.provider}:${item.provider_id}`)); renderProviderResults(); return; }
    if (action === "clear-provider-results") { state.selectedProviderIds.clear(); renderProviderResults(); return; }
    if (action === "provider-import-selected") {
      const items = state.providerResults.filter((item) => state.selectedProviderIds.has(`${item.provider}:${item.provider_id}`));
      const result = await api("/api/scout/import/bulk", { method: "POST", body: JSON.stringify({ items }) });
      state.selectedProviderIds.clear();
      renderProviderResults();
      const failureCount = (result.errors || []).length;
      toast(`${(result.imported || []).length} provider asset${result.imported?.length === 1 ? "" : "s"} imported${failureCount ? ` · ${failureCount} failed` : ""}`, failureCount > 0);
      await refresh();
      return;
    }
    if (action === "close-asset-inspector") { state.inspectedAssetId = ""; renderAssets(); return; }
    if (bulkAction) { await submitBulkReview(bulkAction); return; }
    if (bulkContentAction) { await submitBulkContentReview(bulkContentAction); return; }
    if (action === "cancel-automation") { resetAutomationForm(); return; }
    if (action === "cancel-account-edit") { resetAccountForm(); return; }
    if (action === "new-project") { showView("forge"); $("#project-form input[name=name]").focus(); return; }
    if (project) { showView("forge"); const selected = state.projects.find((item) => item.id === project); if (selected) renderInspector(selected); return; }
    if (contentReview && contentVerdict) { const note = $("#asset-content-review-note")?.value || ""; await api("/api/assets/" + contentReview + "/content-review", { method: "POST", body: JSON.stringify({ verdict: contentVerdict, note }) }); delete state.contentReviews[contentReview]; toast(contentVerdict === "safe" ? "Content marked safe" : "Content kept flagged", contentVerdict !== "safe"); await refresh(); return; }
    if (contentReviewHistory) { if (state.contentReviews[contentReviewHistory]) { delete state.contentReviews[contentReviewHistory]; renderAssetInspector(); } else { const result = await api("/api/assets/" + contentReviewHistory + "/content-reviews"); state.contentReviews[contentReviewHistory] = result.reviews || []; renderAssetInspector(); } return; }
    if (reviewAsset && reviewAction) { const note = $("#asset-review-note")?.value || ""; await api(`/api/assets/${reviewAsset}/review`, { method: "POST", body: JSON.stringify({ action: reviewAction, note }) }); delete state.assetReviews[reviewAsset]; toast(reviewAction === "reject" ? "Asset rejected" : "Rights decision recorded"); await refresh(); return; }
    if (approveLicensed) { await api(`/api/assets/${approveLicensed}/review`, { method: "POST", body: JSON.stringify({ action: "approve_licensed" }) }); delete state.assetReviews[approveLicensed]; toast("Provider license approved"); await refresh(); return; }
    if (approveOwned) { await api(`/api/assets/${approveOwned}/review`, { method: "POST", body: JSON.stringify({ action: "approve_owned" }) }); delete state.assetReviews[approveOwned]; toast("Ownership attested"); await refresh(); return; }
    if (editAutomationId) { editAutomation(editAutomationId); return; }
    if (inspectAutomation) { await inspectAutomationRuns(inspectAutomation); return; }
    if (reviewAutomation) { state.assetFilter = "review"; state.assetQuery = reviewAutomation; state.visibleAssets = (await api("/api/assets?view=review")).assets || []; showView("scout"); renderAssets(); return; }
    if (runAutomation) { await api(`/api/automations/${runAutomation}/run`, { method: "POST", body: "{}" }); toast("Automation queued"); await refresh(); return; }
    if (retryJob) { await api(`/api/jobs/${retryJob}/retry`, { method: "POST", body: "{}" }); toast("Job retry queued"); await refresh(); return; }
    if (toggleAutomation) { const automation = state.automations.find((item) => item.id === toggleAutomation); if (automation) { await api(`/api/automations/${toggleAutomation}`, { method: "PATCH", body: JSON.stringify({ enabled: !automation.enabled }) }); toast(automation.enabled ? "Automation paused" : "Automation enabled"); await refresh(); } return; }
    if (deleteAutomation) { if (!window.confirm("Delete this automation recipe? Imported assets will stay in the library.")) return; await api(`/api/automations/${deleteAutomation}`, { method: "DELETE" }); toast("Automation deleted"); await refresh(); return; }
    if (deleteProject) { if (!window.confirm("Delete this project and its rendered package? Source assets will stay in Scout.")) return; await api(`/api/projects/${deleteProject}`, { method: "DELETE" }); if (state.selectedProject?.id === deleteProject) state.selectedProject = null; toast("Project deleted"); await refresh(); return; }
    if (checkAccount) { const result = await api(`/api/publishing/accounts/${checkAccount}/check`, { method: "POST", body: "{}" }); const check = result.account?.connection_check || {}; toast(check.status === "passed" ? `Connection verified as ${check.identity || "account"}` : check.message || "Connection check failed", check.status !== "passed"); await refresh(); return; }
    if (editAccount) { editAccountForm(editAccount); return; }
    if (deleteAccount) { if (!window.confirm("Delete this channel configuration? Pending publish jobs must be cleared first.")) return; await api(`/api/publishing/accounts/${deleteAccount}`, { method: "DELETE" }); if (state.editingAccountId === deleteAccount) resetAccountForm(); toast("Channel deleted"); await refresh(); return; }
    if (rejectAsset) { await api(`/api/assets/${rejectAsset}/review`, { method: "POST", body: JSON.stringify({ action: "reject" }) }); delete state.assetReviews[rejectAsset]; toast("Asset rejected"); await refresh(); return; }
    if (inspectAsset) { state.inspectedAssetId = inspectAsset; showView("scout"); renderAssetInspector(); return; }
    if (healthAsset) { await api(`/api/assets/${healthAsset}/health`, { method: "POST", body: "{}" }); toast("Media health updated"); await refresh(); return; }
    if (deleteAsset) { if (!window.confirm("Delete this source record and its local file? References used by a project or automation must be detached first.")) return; await api(`/api/assets/${deleteAsset}`, { method: "DELETE" }); state.selectedAssets.delete(deleteAsset); if (state.inspectedAssetId === deleteAsset) state.inspectedAssetId = ""; toast("Source record deleted"); await refresh(); return; }
    if (reviewHistory) { if (state.assetReviews[reviewHistory]) { delete state.assetReviews[reviewHistory]; renderAssetInspector(); } else { const result = await api(`/api/assets/${reviewHistory}/reviews`); state.assetReviews[reviewHistory] = result.reviews || []; renderAssetInspector(); } return; }
    if (validateOutput) { await api(`/api/projects/${validateOutput}/validate-output`, { method: "POST", body: "{}" }); toast("Output package validated"); await refresh(); return; }
    if (addAsset) { showView("forge"); const selected = state.selectedProject || (state.projects.length === 1 ? state.projects[0] : null); if (!selected) { toast("Open a project before adding a clip", true); return; } await api(`/api/projects/${selected.id}/assets`, { method: "POST", body: JSON.stringify({ asset_id: addAsset }) }); toast("Clip added to the selected project"); await refresh(); return; }
    if (removeClip) { const card = target.closest(".timeline-clip"); const clipProject = projectId || card?.dataset.projectId; await api(`/api/projects/${clipProject}/assets/${removeClip}`, { method: "DELETE" }); toast("Clip removed from the edit"); await refresh(); return; }
    if (saveClip || toggleClip) { const card = target.closest(".timeline-clip"); const clipProject = projectId || card?.dataset.projectId; const clipId = saveClip || toggleClip; const body = toggleClip ? { enabled: Number(enabled) } : { in_point: Number($(".clip-in", card).value), out_point: Number($(".clip-out", card).value) }; await api(`/api/projects/${clipProject}/assets/${clipId}`, { method: "POST", body: JSON.stringify(body) }); toast(toggleClip ? "Clip state updated" : "Trim points saved"); await refresh(); return; }
    if (moveClip) { const card = target.closest(".timeline-clip"); const clipProject = projectId || card?.dataset.projectId; await api(`/api/projects/${clipProject}/assets/${moveClip}/move`, { method: "POST", body: JSON.stringify({ direction }) }); toast("Clip order updated"); await refresh(); return; }
    if (providerSave || providerImport) { const item = state.providerResults.find((result) => result.provider_id === (providerSave || providerImport)); if (!item) { toast("Provider result expired", true); return; } if (providerSave) { await api("/api/assets/reference", { method: "POST", body: JSON.stringify({ title: item.title, source_url: item.source_url, platform: item.provider, tags: [item.provider] }) }); toast("Reference saved to Scout"); } else { await api("/api/scout/import", { method: "POST", body: JSON.stringify({ provider: item.provider, provider_id: item.provider_id, download_url: item.download_url, source_url: item.source_url, title: item.title, tags: [item.provider] }) }); toast("Provider asset imported for review"); } state.selectedProviderIds.delete(`${item.provider}:${item.provider_id}`); renderProviderResults(); await refresh(); return; }
    if (target.dataset.copyPath) { await navigator.clipboard?.writeText(target.dataset.copyPath); toast("Package path copied"); return; }
    if (planProject) { await api(`/api/projects/${planProject}/plan`, { method: "POST", body: "{}" }); toast("Edit plan updated"); await refresh(); return; }
    if (renderProject) { const result = await api(`/api/projects/${renderProject}/render`, { method: "POST", body: "{}" }); toast(`Render queued: ${result.job.id}`); await refresh(); return; }
    if (publishProject) { state.publishDraft.projectId = publishProject; showView("publisher"); renderPublisher(); toast("Release selected"); return; }
    if (publishHistory) { if (state.publishAttempts[publishHistory]) { delete state.publishAttempts[publishHistory]; } else { const result = await api(`/api/publishing/jobs/${publishHistory}/attempts`); state.publishAttempts[publishHistory] = result.attempts || []; } renderPublisher(); return; }
    if (runPublish) { await api(`/api/publishing/jobs/${runPublish}/publish`, { method: "POST", body: "{}" }); toast("Published successfully"); await refresh(); return; }
    if (markPublished) { await api(`/api/publishing/jobs/${markPublished}/mark-published`, { method: "POST", body: JSON.stringify({ remote_id: "manual" }) }); toast("Marked as posted"); await refresh(); return; }
    if (editSchedule) { state.publishScheduleJobId = editSchedule; renderPublisher(); return; }
    if (cancelSchedule) { state.publishScheduleJobId = ""; renderPublisher(); return; }
    if (saveSchedule) { const input = $(".publish-schedule-input", target.closest(".publish-entry")); const value = input?.value ? new Date(input.value) : null; if (value && Number.isNaN(value.getTime())) throw new Error("Schedule time is invalid"); await api(`/api/publishing/jobs/${saveSchedule}`, { method: "PATCH", body: JSON.stringify({ scheduled_for: value ? value.toISOString() : "" }) }); state.publishScheduleJobId = ""; toast(value ? "Schedule updated" : "Schedule cleared"); await refresh(); return; }
    if (cancelPublish) { if (!window.confirm("Cancel this outbox job? Its attempt history will stay available.")) return; await api(`/api/publishing/jobs/${cancelPublish}`, { method: "DELETE" }); state.publishScheduleJobId = ""; toast("Publish job cancelled"); await refresh(); return; }
  } catch (error) { toast(error.message, true); }
}

async function submitReference(form) {
  try { const data = Object.fromEntries(new FormData(form)); const result = await api("/api/assets/reference", { method: "POST", body: JSON.stringify({ ...data, tags: String(data.tags || "").split(",") }) }); toast(`${result.asset.title} saved to Scout`); form.reset(); await refresh(); } catch (error) { toast(error.message, true); }
}

async function submitAssetCuration(form) {
  try {
    const data = Object.fromEntries(new FormData(form));
    await api(`/api/assets/${form.dataset.assetId}`, { method: "PATCH", body: JSON.stringify({ title: data.title, source_url: data.source_url, tags: String(data.tags || "").split(",") }) });
    toast("Curation metadata saved");
    await refresh();
  } catch (error) { toast(error.message, true); }
}

async function submitProject(form) {
  try {
    const data = Object.fromEntries(new FormData(form));
    const audioTracks = [];
    const musicAssetId = $("#music-asset-select")?.value || "";
    const sfxAssetId = $("#sfx-asset-select")?.value || "";
    const voiceoverAssetId = $("#voiceover-asset-select")?.value || "";
    if (musicAssetId) audioTracks.push({ asset_id: musicAssetId, role: "music", volume: 0.16, start_time: 0 });
    if (sfxAssetId) audioTracks.push({ asset_id: sfxAssetId, role: "sfx", volume: Number($("#sfx-volume")?.value || 0.55), start_time: Number($("#sfx-start-time")?.value || 0) });
    if (voiceoverAssetId) audioTracks.push({ asset_id: voiceoverAssetId, role: "voiceover", volume: Number($("#voiceover-volume")?.value || 1), start_time: Number($("#voiceover-start-time")?.value || 0) });
    const result = await api("/api/projects", { method: "POST", body: JSON.stringify({ ...data, audio_tracks: audioTracks, duration_target: Number(data.duration_target) }) });
    toast("Project created");
    form.reset();
    $("input[name=duration_target]", form).value = 35;
    $("#music-asset-select").value = "";
    $("#sfx-asset-select").value = "";
    $("#voiceover-asset-select").value = "";
    $("#sfx-start-time").value = 0;
    $("#voiceover-start-time").value = 0;
    $("#sfx-volume").value = 0.55;
    $("#voiceover-volume").value = 1;
    await refresh();
    renderInspector(result.project);
  } catch (error) { toast(error.message, true); }
}

async function submitProjectSettings(form) {
  const button = $("button[type=submit]", form);
  button.disabled = true;
  try {
    const data = new FormData(form);
    const audioTracks = [];
    const musicAssetId = String(data.get("music_asset_id") || "");
    const sfxAssetId = String(data.get("sfx_asset_id") || "");
    const voiceoverAssetId = String(data.get("voiceover_asset_id") || "");
    if (musicAssetId) audioTracks.push({ asset_id: musicAssetId, role: "music", volume: Number(data.get("music_volume") || 0.16), start_time: 0 });
    if (sfxAssetId) audioTracks.push({ asset_id: sfxAssetId, role: "sfx", volume: Number(data.get("sfx_volume") || 0.55), start_time: Number(data.get("sfx_start_time") || 0) });
    if (voiceoverAssetId) audioTracks.push({ asset_id: voiceoverAssetId, role: "voiceover", volume: Number(data.get("voiceover_volume") || 1), start_time: Number(data.get("voiceover_start_time") || 0) });
    await api(`/api/projects/${form.dataset.projectId}`, { method: "PATCH", body: JSON.stringify({ name: data.get("name"), theme: data.get("theme"), duration_target: Number(data.get("duration_target") || 35), style: data.get("style"), title: data.get("title"), caption: data.get("caption"), add_title_card: data.has("add_title_card"), write_captions: data.has("write_captions"), audio_tracks: audioTracks }) });
    toast("Project settings saved; render again");
    await refresh();
  } catch (error) { toast(error.message, true); } finally { button.disabled = false; }
}

function resetAccountForm() {
  const form = $("#account-form");
  if (!form) return;
  state.editingAccountId = "";
  form.reset();
  $("#account-form-eyebrow").textContent = "ACCOUNTS";
  $("#account-form-title").textContent = "Add channel";
  $("#account-submit").textContent = "+ Add account";
  $("#account-cancel").hidden = true;
  $("#account-platform").disabled = false;
  updateAccountFields();
}

function editAccountForm(accountId) {
  const account = state.accounts.find((item) => item.id === accountId);
  const form = $("#account-form");
  if (!account || !form) return;
  state.editingAccountId = account.id;
  $("#account-platform").value = account.platform;
  $("#account-platform").disabled = true;
  $("input[name=label]", form).value = account.label;
  $("input[name=instagram_user_id]", form).value = account.config?.instagram_user_id || "";
  $("input[name=public_media_base_url]", form).value = account.config?.public_media_base_url || "";
  $("#account-form-eyebrow").textContent = "EDIT CHANNEL";
  $("#account-form-title").textContent = account.label;
  $("#account-submit").textContent = "Save account";
  $("#account-cancel").hidden = false;
  updateAccountFields();
  showView("publisher");
}

async function submitAccount(form) {
  try {
    const data = Object.fromEntries(new FormData(form));
    const endpoint = state.editingAccountId ? `/api/publishing/accounts/${state.editingAccountId}` : "/api/publishing/accounts";
    const method = state.editingAccountId ? "PATCH" : "POST";
    await api(endpoint, { method, body: JSON.stringify(data) });
    toast(state.editingAccountId ? "Channel saved" : "Channel added");
    resetAccountForm();
    await refresh();
  } catch (error) { toast(error.message, true); }
}

function updateAccountFields() {
  const platform = $("#account-platform")?.value;
  const config = $("#instagram-config");
  if (config) config.hidden = platform !== "instagram";
}

async function submitProviderSearch(form) {
  const button = $("button[type=submit]", form); button.disabled = true;
  try { const data = Object.fromEntries(new FormData(form)); const payload = await api(`/api/scout/search?provider=${encodeURIComponent(data.provider)}&q=${encodeURIComponent(data.query)}`); renderProviderResults(payload.results || []); toast(`${payload.results?.length || 0} source${payload.results?.length === 1 ? "" : "s"} found`); } catch (error) { renderProviderResults([]); toast(error.message, true); } finally { button.disabled = false; }
}

function handleAssetSelection(event) {
  const input = event.target.closest("input[data-asset-select]");
  if (!input) return;
  if (input.checked) state.selectedAssets.add(input.dataset.assetSelect);
  else state.selectedAssets.delete(input.dataset.assetSelect);
  renderReviewToolbar();
}

function handleProviderSelection(event) {
  const input = event.target.closest("input[data-provider-select]");
  if (!input) return;
  if (input.checked) state.selectedProviderIds.add(input.dataset.providerSelect);
  else state.selectedProviderIds.delete(input.dataset.providerSelect);
  renderProviderResults();
}

document.addEventListener("click", handleClick);
document.addEventListener("change", handleAssetSelection);
document.addEventListener("change", handleProviderSelection);
document.addEventListener("submit", (event) => { if (event.target.id === "asset-inspector-form") { event.preventDefault(); submitAssetCuration(event.target); } });
document.addEventListener("submit", (event) => { if (event.target.id === "project-settings-form") { event.preventDefault(); submitProjectSettings(event.target); } });
$("#asset-search").addEventListener("input", (event) => { state.assetQuery = event.target.value; renderAssets(); });
$("#asset-sort").addEventListener("change", (event) => { state.assetSort = event.target.value; renderAssets(); });
$("#upload-form").addEventListener("submit", (event) => { event.preventDefault(); submitUpload(event.currentTarget); });
$("#audio-upload-form").addEventListener("submit", (event) => { event.preventDefault(); submitAudioUpload(event.currentTarget); });
$("#reference-form").addEventListener("submit", (event) => { event.preventDefault(); submitReference(event.currentTarget); });
$("#automation-form").addEventListener("submit", (event) => { event.preventDefault(); submitAutomation(event.currentTarget); });
$("#automation-source-mode").addEventListener("change", updateAutomationSourceFields);
$("#automation-form").addEventListener("change", (event) => { if (event.target.name === "platforms" || event.target.classList.contains("automation-account-select")) renderAutomationAccountPickers(); });
$("#project-form").addEventListener("submit", (event) => { event.preventDefault(); submitProject(event.currentTarget); });
$("#publish-form").addEventListener("submit", (event) => { event.preventDefault(); submitPublish(event.currentTarget).catch((error) => toast(error.message, true)); });
$("#publish-form").addEventListener("change", () => renderPublishAccountPickers());
$("#account-form").addEventListener("submit", (event) => { event.preventDefault(); submitAccount(event.currentTarget); });
$("#account-platform").addEventListener("change", updateAccountFields);
$("#provider-search-form").addEventListener("submit", (event) => { event.preventDefault(); submitProviderSearch(event.currentTarget); });
$("#video-file").addEventListener("change", (event) => { $("#file-label").textContent = event.target.files[0]?.name || "Choose a video"; });
$("#asset-filters").addEventListener("click", async (event) => { const button = event.target.closest("button"); if (!button) return; state.assetFilter = button.dataset.filter; $$(".filter-tab").forEach((item) => item.classList.toggle("active", item === button)); try { state.visibleAssets = (await api(`/api/assets?view=${state.assetFilter}`)).assets || []; renderAssets(); } catch (error) { toast(error.message, true); } });

const initialView = location.hash.replace("#", "") || "overview";
updateAccountFields();
updateAutomationSourceFields();
showView(["overview", "scout", "automations", "forge", "outputs", "publisher"].includes(initialView) ? initialView : "overview");
refresh().catch((error) => { toast(error.message, true); $("#health-label").textContent = "offline"; });
setInterval(() => refresh().catch(() => {}), 2500);
