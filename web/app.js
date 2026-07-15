const state = { assets: [], visibleAssets: [], projects: [], jobs: [], publishingJobs: [], accounts: [], automations: [], automationRuns: [], selectedAutomationId: "", platforms: [], diagnostics: {}, providerResults: [], assetFilter: "all", assetQuery: "", assetSort: "recent", selectedAssets: new Set(), inspectedAssetId: "", selectedProject: null, publishDraft: { projectId: "" }, editingAutomationId: "" };

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
const statusPill = (value) => `<span class="status-pill ${statusClass(value)}">${escapeHtml(String(value).replaceAll("_", " "))}</span>`;

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
    ["Text overlay", Boolean(media.drawtext), media.drawtext ? "drawtext available" : "Optional; captions still render"],
    ...(diagnostics.providers || []).map((provider) => [provider.label, Boolean(provider.configured), provider.configured ? "API key detected" : "API key not configured"]),
  ];
  node.innerHTML = checks.map(([label, ok, detail]) => `<div class="diagnostic-row"><div class="row-main"><strong>${escapeHtml(label)}</strong><small>${escapeHtml(detail)}</small></div>${statusPill(ok ? "ready" : "not_configured")}</div>`).join("");
}

function projectRow(project) {
  return `<div class="project-row"><div class="row-icon">✦</div><div class="row-main"><strong>${escapeHtml(project.name)}</strong><small>${escapeHtml(project.theme)} · ${formatDuration(project.duration_target)}</small></div>${statusPill(project.status)}<button class="row-action" data-project="${project.id}">Open</button></div>`;
}

function empty(title, detail) { return `<div class="empty-state"><span>·</span><strong>${escapeHtml(title)}</strong><p>${escapeHtml(detail)}</p></div>`; }

function libraryAssets() {
  const query = state.assetQuery.trim().toLowerCase();
  const filtered = state.visibleAssets.filter((asset) => {
    if (!query) return true;
    return [asset.title, asset.platform, asset.source_url, ...(asset.tags || []), ...(asset.review_flags || [])].join(" ").toLowerCase().includes(query);
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
  const selectable = new Set(state.visibleAssets.filter(isReviewableAsset).map((asset) => asset.id));
  state.selectedAssets = new Set([...state.selectedAssets].filter((id) => selectable.has(id)));
  const count = assets.length === state.visibleAssets.length ? `${assets.length}` : `${assets.length} of ${state.visibleAssets.length}`;
  $("#asset-count").textContent = `${count} asset${assets.length === 1 ? "" : "s"}`;
  $("#asset-grid").innerHTML = assets.length ? assets.map(assetCard).join("") : empty("Nothing here yet", state.assetFilter === "references" ? "Save a source URL to Scout." : "Import a cleared video to populate the board.");
  renderReviewToolbar();
  renderAssetInspector();
}

function isReviewableAsset(asset) {
  return asset.kind === "media" && Boolean(asset.local_path) && ["pending_review", "private_reference"].includes(asset.rights_status);
}

function renderReviewToolbar() {
  const count = state.selectedAssets.size;
  $("#selected-asset-count").textContent = `${count} selected`;
  $$('[data-bulk-action]').forEach((button) => { button.disabled = count === 0; });
}

function renderProviderResults(results = []) {
  state.providerResults = results;
  $("#provider-results").innerHTML = results.length ? results.map((item) => `<article class="provider-result"><strong title="${escapeHtml(item.title)}">${escapeHtml(item.title)}</strong><small>${escapeHtml(item.provider)} · ${formatDuration(item.duration)} · ${item.width || "?"}x${item.height || "?"}</small><div class="asset-actions"><button class="mini-button" data-provider-save="${escapeHtml(item.provider_id)}">Save reference</button><button class="mini-button" data-provider-import="${escapeHtml(item.provider_id)}">Import for review</button></div></article>`).join("") : "";
}

function assetCard(asset) {
  const preview = asset.local_path ? (String(asset.mime_type || "").startsWith("audio/") ? "♫" : `<video src="/api/assets/${asset.id}/stream" muted preload="metadata"></video>`) : "↗";
  const selector = isReviewableAsset(asset) ? `<label class="asset-select" title="Select for batch review"><input type="checkbox" data-asset-select="${asset.id}" ${state.selectedAssets.has(asset.id) ? "checked" : ""} /></label>` : "";
  const tags = (asset.tags || []).slice(0, 4).map((tag) => `<span class="tag">${escapeHtml(tag)}</span>`).join("");
  const canApprove = isReviewableAsset(asset);
  const canAdd = asset.kind === "media" && ["owned_distribution", "licensed_distribution"].includes(asset.rights_status) && ["ready", "approved"].includes(asset.status);
  const providerImport = ["pexels", "pixabay"].includes(String(asset.platform || "").toLowerCase());
  const action = canApprove ? providerImport ? `<button class="mini-button" data-approve-licensed="${asset.id}">Approve license</button>` : `<button class="mini-button" data-approve-owned="${asset.id}">Declare owned</button>` : canAdd ? `<button class="mini-button" data-add-asset="${asset.id}">Add to edit</button>` : `<span class="count-label">Reference only</span>`;
  const sourceAction = /^https?:\/\//i.test(asset.source_url || "") ? `<a class="mini-button mini-link" href="${escapeHtml(asset.source_url)}" target="_blank" rel="noreferrer">Open source</a>` : "";
  const flags = (asset.review_flags || []).map((flag) => `<span class="risk-flag">${escapeHtml(flag.replaceAll("-", " "))}</span>`).join("");
  return `<article class="asset-card"><div class="asset-preview">${selector}${preview}</div><div class="asset-content"><strong title="${escapeHtml(asset.title)}">${escapeHtml(asset.title)}</strong><div class="asset-meta"><span>${asset.kind === "reference" ? escapeHtml(asset.platform || "reference") : `${formatDuration(asset.duration)} · ${formatBytes(asset.file_size)}`}</span>${statusPill(asset.rights_status)}</div><div class="asset-tags">${tags}${flags}</div><div class="asset-actions">${action}${sourceAction}<button class="mini-button" data-inspect-asset="${asset.id}">Inspect</button><button class="mini-button danger" data-reject-asset="${asset.id}">Reject</button></div></div></article>`;
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
  node.hidden = false;
  node.innerHTML = `<div class="asset-inspector-header"><div><span class="eyebrow accent">SOURCE RECORD</span><h3>${escapeHtml(asset.title)}</h3><p>Inspect the origin and update the curation metadata without changing the source file.</p></div><button class="text-button" type="button" data-action="close-asset-inspector">Close</button></div><div class="inspector-grid"><div class="asset-detail-list"><div class="asset-detail"><span>Origin</span><strong>${escapeHtml(provenance.source_type || "unknown")}</strong></div><div class="asset-detail"><span>Platform</span><strong>${escapeHtml(provenance.platform || "local")}</strong></div><div class="asset-detail"><span>Automation</span><strong>${escapeHtml(provenance.automation_id || "Manual import")}</strong></div><div class="asset-detail"><span>Fingerprint</span><code title="${escapeHtml(provenance.fingerprint || "")}">${escapeHtml(fingerprint)}</code></div><div class="asset-detail"><span>Local file</span><code>${escapeHtml(provenance.local_path || "Reference only")}</code></div><div class="asset-detail"><span>Source URL</span>${sourceUrl ? `<a class="inspector-link" href="${escapeHtml(sourceUrl)}" target="_blank" rel="noreferrer">${escapeHtml(sourceUrl)}</a>` : `<strong>${escapeHtml(asset.source_url || "No source URL")}</strong>`}</div><div class="asset-flags"><span>Review flags</span><div>${flags}</div></div></div><form class="inspector-form" id="asset-inspector-form" data-asset-id="${escapeHtml(asset.id)}"><label>Title<input name="title" value="${escapeHtml(asset.title)}" maxlength="160" required /></label><label>Tags<input name="tags" value="${escapeHtml((asset.tags || []).join(", "))}" placeholder="cute, funny, reaction" /></label><label>Source URL<input name="source_url" type="url" value="${escapeHtml(asset.source_url || "")}" placeholder="https://..." /></label><button class="button secondary full" type="submit">Save curation</button><p class="form-note">Rights decisions stay in the review queue. This form only edits descriptive metadata.</p></form></div>`;
}

function renderAudioSelect() {
  const selects = [$("#music-asset-select"), $("#sfx-asset-select"), $("#voiceover-asset-select"), $("#automation-music-select"), $("#automation-sfx-select"), $("#automation-voiceover-select")].filter(Boolean);
  if (!selects.length) return;
  const audioAssets = state.assets.filter((asset) => String(asset.mime_type || "").startsWith("audio/") && ["owned_distribution", "licensed_distribution"].includes(asset.rights_status) && ["ready", "approved"].includes(asset.status));
  const emptyLabels = { "music-asset-select": "No music bed", "sfx-asset-select": "No sound effect", "voiceover-asset-select": "No voiceover", "automation-music-select": "No music bed", "automation-sfx-select": "No sound effect", "automation-voiceover-select": "No voiceover" };
  selects.forEach((select) => {
    const current = select.value;
    select.innerHTML = `<option value="">${emptyLabels[select.id] || "No audio"}</option>${audioAssets.map((asset) => `<option value="${asset.id}">${escapeHtml(asset.title)} · ${formatDuration(asset.duration)}</option>`).join("")}`;
    if (audioAssets.some((asset) => asset.id === current)) select.value = current;
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
  const clipMarkup = clips.map((clip, index) => { const clipOut = clip.out_point ?? clip.duration; return `<div class="timeline-clip ${clip.enabled ? "" : "disabled"}" data-project-id="${project.id}" data-clip-id="${clip.asset_id}"><b>${String(index + 1).padStart(2, "0")}</b><strong title="${escapeHtml(clip.title)}">${escapeHtml(clip.title)}</strong><div class="clip-controls"><label>In<input class="clip-in" type="number" min="0" max="${clip.duration}" step="0.1" value="${clip.in_point}" /></label><label>Out<input class="clip-out" type="number" min="0.1" max="${clip.duration}" step="0.1" value="${clipOut}" /></label></div><small>${formatDuration(clipOut - clip.in_point)} · ${escapeHtml(clip.selected_by)}</small>${clip.note ? `<small class="clip-note">${escapeHtml(clip.note)}</small>` : ""}<div class="clip-actions"><button class="mini-button" data-save-clip="${clip.asset_id}">Save</button><button class="mini-button" data-toggle-clip="${clip.asset_id}" data-enabled="${clip.enabled ? "0" : "1"}">${clip.enabled ? "Disable" : "Enable"}</button></div></div>`; }).join("");
  const audioSummary = audio.length ? audio.map((track) => `${escapeHtml(track.role)}: ${escapeHtml(track.title)} @ ${track.start_time}s`).join(" · ") : "No added audio tracks";
  $("#project-inspector").innerHTML = `<div class="inspector-header"><div><span class="eyebrow accent">EDIT INSPECTOR</span><h3>${escapeHtml(project.name)}</h3><p>${escapeHtml(project.theme)} · ${formatDuration(project.duration_target)} target · ${clips.length} clips</p></div><div class="inspector-actions"><button class="button secondary" data-plan-project="${project.id}">✦ Plan edit</button><button class="button primary" data-render-project="${project.id}">▶ Render</button></div></div><div class="timeline">${clips.length ? clipMarkup : empty("No clips selected", "Plan automatically or add clips from Scout.")}</div><div class="manifest"><strong>Planner</strong> ${escapeHtml(manifest.planner || "Manual")}${manifest.selected_duration ? ` · ${formatDuration(manifest.selected_duration)} selected` : ""}<div class="progress-track"><i style="width:${project.status === "ready" ? 100 : 0}%"></i></div></div><div class="sound-summary"><strong>Sound</strong><span>${audioSummary}</span></div>`;
}

function renderOutputs() {
  const ready = state.projects.filter((project) => project.status === "ready" && project.output_dir);
  $("#output-list").innerHTML = ready.length ? ready.map((project) => { const validation = project.manifest?.validation || {}; const validationStatus = validation.status || "unverified"; const outputCount = Object.keys(validation.outputs || {}).length; const validateAction = validationStatus === "passed" ? "" : `<button class="button secondary" data-validate-output="${project.id}">Validate package</button>`; return `<article class="output-card"><div class="output-video"><video controls preload="metadata" src="/api/projects/${project.id}/output/final.mp4"></video></div><div class="output-info"><div class="output-status"><span class="eyebrow accent">${escapeHtml(project.style)} / 9:16</span>${statusPill(validationStatus)}</div><h3>${escapeHtml(project.title || project.name)}</h3><p>${escapeHtml(project.caption || project.theme)}</p><div class="output-files"><span class="file-chip">final.mp4</span><span class="file-chip">captions.srt</span><span class="file-chip">youtube.mp4</span><span class="file-chip">instagram.mp4</span><span class="file-chip">tiktok.mp4</span></div><p class="output-health">${outputCount ? `${outputCount} outputs validated` : "Output validation predates this check"}</p><div class="output-actions">${validateAction}<button class="button primary" data-publish-project="${project.id}">↗ Prepare publish</button><button class="button secondary" data-copy-path="${escapeHtml(project.output_dir)}">Copy package path</button></div></div></article>`; }).join("") : empty("No rendered outputs", "Your first finished edit will appear here.");
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
  $("#account-list").innerHTML = state.accounts.length ? state.accounts.map((account) => { const tokenEnv = account.config?.token_env || "token not configured"; const detail = account.status === "connected" ? `token detected · ${tokenEnv}` : `set ${tokenEnv}`; return `<div class="account-row"><div class="row-icon">◎</div><div class="row-main"><strong>${escapeHtml(account.label)}</strong><small>${escapeHtml(account.platform)} · ${escapeHtml(detail)}</small></div>${statusPill(account.status)}</div>`; }).join("") : empty("No channels added", "Add a destination to prepare your outbox.");
  $("#publish-count").textContent = `${state.publishingJobs.filter((job) => ["queued", "ready"].includes(job.status)).length} queued`;
  $("#publish-list").innerHTML = state.publishingJobs.length ? state.publishingJobs.map((job) => { const sourceCount = Number(job.payload?.provenance?.asset_ids?.length || 0); const provenance = sourceCount ? ` · ${sourceCount} credited source${sourceCount === 1 ? "" : "s"}` : ""; return `<div class="publish-row"><div class="row-icon">↗</div><div class="row-main"><strong>${escapeHtml(job.payload?.title || job.platform)}</strong><small>${escapeHtml(job.platform)} · ${job.scheduled_for ? `scheduled ${escapeHtml(job.scheduled_for)}` : "ready for account connection"}${provenance}${job.error ? ` · ${escapeHtml(job.error)}` : ""}</small></div>${statusPill(job.status)}${job.status === "ready" ? `<button class="row-action" data-run-publish="${job.id}">Publish</button><button class="row-action" data-mark-published="${job.id}">Mark posted</button>` : job.status === "failed" ? `<button class="row-action" data-run-publish="${job.id}">Retry</button>` : ""}</div>`; }).join("") : empty("Outbox is empty", "Rendered projects can be prepared for distribution.");
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
    return `<div class="automation-row"><div class="row-icon">↻</div><div class="row-main"><strong>${escapeHtml(automation.name)}</strong><small>${escapeHtml(source)} · ${escapeHtml(detail)}${automation.last_error ? ` · ${escapeHtml(automation.last_error)}` : ""}</small></div>${statusPill(automation.status)}<button class="row-action" data-inspect-automation="${automation.id}">History</button><button class="row-action" data-edit-automation="${automation.id}">Edit</button><button class="row-action" data-run-automation="${automation.id}" ${running ? "disabled" : ""}>Run</button><button class="row-action" data-toggle-automation="${automation.id}">${toggleLabel}</button><button class="row-action danger-action" data-delete-automation="${automation.id}">Delete</button></div>`;
  }).join("") : empty("No automations yet", "Create a recipe to watch a local folder and build repeatable releases.");
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
    return `<div class="automation-row"><div class="row-icon">↻</div><div class="row-main"><strong>${escapeHtml(run.started_at.replace("T", " "))} · ${escapeHtml(run.id)}</strong><small>${escapeHtml(scanSummary)} · ${escapeHtml(resultSummary)}</small></div>${statusPill(run.status)}${openProject}</div>`;
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
  form.reset();
  $("#automation-form-eyebrow").textContent = "AUTOMATION RECIPE";
  $("#automation-form-title").textContent = "New recipe";
  $("#automation-submit").textContent = "Create automation";
  $("#automation-cancel").hidden = true;
  updateAutomationSourceFields();
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
  $("input[name=auto_render]", form).checked = automation.auto_render;
  $("input[name=enabled]", form).checked = automation.enabled;
  $("#automation-form-eyebrow").textContent = "EDIT AUTOMATION";
  $("#automation-form-title").textContent = automation.name;
  $("#automation-submit").textContent = "Save automation";
  $("#automation-cancel").hidden = false;
  updateAutomationSourceFields();
  showView("automations");
}

async function submitAutomation(form) {
  const button = $("#automation-submit");
  button.disabled = true;
  try {
    const data = new FormData(form);
    const payload = Object.fromEntries(data);
    payload.platforms = data.getAll("platforms").map(String);
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
  const result = await api(`/api/projects/${projectId}/publish`, { method: "POST", body: JSON.stringify({ platforms, scheduled_for: scheduledFor }) });
  state.publishDraft.projectId = projectId;
  toast(`${result.jobs.length} package${result.jobs.length === 1 ? "" : "s"} prepared`);
  await refresh();
}

async function submitBulkReview(action) {
  if (!state.selectedAssets.size) { toast("Select at least one asset", true); return; }
  const labels = { approve_licensed: "Provider licenses approved", approve_owned: "Ownership attested", reject: "Assets rejected" };
  const result = await api("/api/assets/bulk", { method: "PATCH", body: JSON.stringify({ ids: [...state.selectedAssets], action }) });
  state.selectedAssets.clear();
  toast(`${result.assets.length} asset${result.assets.length === 1 ? "" : "s"} updated: ${labels[action] || "Review updated"}`);
  await refresh();
}

async function handleClick(event) {
  const target = event.target.closest("button"); if (!target) return;
  const { view, viewTarget, action, bulkAction, project, planProject, renderProject, approveLicensed, approveOwned, rejectAsset, inspectAsset, validateOutput, addAsset, publishProject, markPublished, runPublish, providerSave, providerImport, saveClip, toggleClip, projectId, enabled, runAutomation, toggleAutomation, deleteAutomation, retryJob, inspectAutomation, editAutomation: editAutomationId } = target.dataset;
  try {
    if (view) { showView(view); return; }
    if (viewTarget) { showView(viewTarget); return; }
    if (action === "refresh") { await refresh(); toast("Workspace refreshed"); return; }
    if (action === "select-visible") { libraryAssets().filter(isReviewableAsset).forEach((asset) => state.selectedAssets.add(asset.id)); renderAssets(); return; }
    if (action === "clear-selection") { state.selectedAssets.clear(); renderAssets(); return; }
    if (action === "clear-automation-runs") { state.selectedAutomationId = ""; state.automationRuns = []; renderAutomationRuns(); return; }
    if (action === "close-asset-inspector") { state.inspectedAssetId = ""; renderAssets(); return; }
    if (bulkAction) { await submitBulkReview(bulkAction); return; }
    if (action === "cancel-automation") { resetAutomationForm(); return; }
    if (action === "new-project") { showView("forge"); $("#project-form input[name=name]").focus(); return; }
    if (project) { showView("forge"); const selected = state.projects.find((item) => item.id === project); if (selected) renderInspector(selected); return; }
    if (approveLicensed) { await api(`/api/assets/${approveLicensed}`, { method: "PATCH", body: JSON.stringify({ status: "approved", rights_status: "licensed_distribution" }) }); toast("Provider license approved"); await refresh(); return; }
    if (approveOwned) { await api(`/api/assets/${approveOwned}`, { method: "PATCH", body: JSON.stringify({ status: "approved", rights_status: "owned_distribution" }) }); toast("Ownership attested"); await refresh(); return; }
    if (editAutomationId) { editAutomation(editAutomationId); return; }
    if (inspectAutomation) { await inspectAutomationRuns(inspectAutomation); return; }
    if (runAutomation) { await api(`/api/automations/${runAutomation}/run`, { method: "POST", body: "{}" }); toast("Automation queued"); await refresh(); return; }
    if (retryJob) { await api(`/api/jobs/${retryJob}/retry`, { method: "POST", body: "{}" }); toast("Job retry queued"); await refresh(); return; }
    if (toggleAutomation) { const automation = state.automations.find((item) => item.id === toggleAutomation); if (automation) { await api(`/api/automations/${toggleAutomation}`, { method: "PATCH", body: JSON.stringify({ enabled: !automation.enabled }) }); toast(automation.enabled ? "Automation paused" : "Automation enabled"); await refresh(); } return; }
    if (deleteAutomation) { if (!window.confirm("Delete this automation recipe? Imported assets will stay in the library.")) return; await api(`/api/automations/${deleteAutomation}`, { method: "DELETE" }); toast("Automation deleted"); await refresh(); return; }
    if (rejectAsset) { await api(`/api/assets/${rejectAsset}`, { method: "PATCH", body: JSON.stringify({ status: "rejected", rights_status: "blocked" }) }); toast("Asset rejected"); await refresh(); return; }
    if (inspectAsset) { state.inspectedAssetId = inspectAsset; showView("scout"); renderAssetInspector(); return; }
    if (validateOutput) { await api(`/api/projects/${validateOutput}/validate-output`, { method: "POST", body: "{}" }); toast("Output package validated"); await refresh(); return; }
    if (addAsset) { showView("forge"); const selected = state.selectedProject || (state.projects.length === 1 ? state.projects[0] : null); if (!selected) { toast("Open a project before adding a clip", true); return; } await api(`/api/projects/${selected.id}/assets`, { method: "POST", body: JSON.stringify({ asset_id: addAsset }) }); toast("Clip added to the selected project"); await refresh(); return; }
    if (saveClip || toggleClip) { const card = target.closest(".timeline-clip"); const clipProject = projectId || card?.dataset.projectId; const clipId = saveClip || toggleClip; const body = toggleClip ? { enabled: Number(enabled) } : { in_point: Number($(".clip-in", card).value), out_point: Number($(".clip-out", card).value) }; await api(`/api/projects/${clipProject}/assets/${clipId}`, { method: "POST", body: JSON.stringify(body) }); toast(toggleClip ? "Clip state updated" : "Trim points saved"); await refresh(); return; }
    if (providerSave || providerImport) { const item = state.providerResults.find((result) => result.provider_id === (providerSave || providerImport)); if (!item) { toast("Provider result expired", true); return; } if (providerSave) { await api("/api/assets/reference", { method: "POST", body: JSON.stringify({ title: item.title, source_url: item.source_url, platform: item.provider, tags: [item.provider] }) }); toast("Reference saved to Scout"); } else { await api("/api/scout/import", { method: "POST", body: JSON.stringify({ provider: item.provider, download_url: item.download_url, source_url: item.source_url, title: item.title, tags: [item.provider] }) }); toast("Provider asset imported for review"); } await refresh(); return; }
    if (target.dataset.copyPath) { await navigator.clipboard?.writeText(target.dataset.copyPath); toast("Package path copied"); return; }
    if (planProject) { await api(`/api/projects/${planProject}/plan`, { method: "POST", body: "{}" }); toast("Edit plan updated"); await refresh(); return; }
    if (renderProject) { const result = await api(`/api/projects/${renderProject}/render`, { method: "POST", body: "{}" }); toast(`Render queued: ${result.job.id}`); await refresh(); return; }
    if (publishProject) { state.publishDraft.projectId = publishProject; showView("publisher"); renderPublisher(); toast("Release selected"); return; }
    if (runPublish) { await api(`/api/publishing/jobs/${runPublish}/publish`, { method: "POST", body: "{}" }); toast("Published successfully"); await refresh(); return; }
    if (markPublished) { await api(`/api/publishing/jobs/${markPublished}/mark-published`, { method: "POST", body: JSON.stringify({ remote_id: "manual" }) }); toast("Marked as posted"); await refresh(); return; }
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

async function submitAccount(form) {
  try { const data = Object.fromEntries(new FormData(form)); await api("/api/publishing/accounts", { method: "POST", body: JSON.stringify(data) }); toast("Channel added"); form.reset(); await refresh(); } catch (error) { toast(error.message, true); }
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

document.addEventListener("click", handleClick);
document.addEventListener("change", handleAssetSelection);
document.addEventListener("submit", (event) => { if (event.target.id === "asset-inspector-form") { event.preventDefault(); submitAssetCuration(event.target); } });
$("#asset-search").addEventListener("input", (event) => { state.assetQuery = event.target.value; renderAssets(); });
$("#asset-sort").addEventListener("change", (event) => { state.assetSort = event.target.value; renderAssets(); });
$("#upload-form").addEventListener("submit", (event) => { event.preventDefault(); submitUpload(event.currentTarget); });
$("#audio-upload-form").addEventListener("submit", (event) => { event.preventDefault(); submitAudioUpload(event.currentTarget); });
$("#reference-form").addEventListener("submit", (event) => { event.preventDefault(); submitReference(event.currentTarget); });
$("#automation-form").addEventListener("submit", (event) => { event.preventDefault(); submitAutomation(event.currentTarget); });
$("#automation-source-mode").addEventListener("change", updateAutomationSourceFields);
$("#project-form").addEventListener("submit", (event) => { event.preventDefault(); submitProject(event.currentTarget); });
$("#publish-form").addEventListener("submit", (event) => { event.preventDefault(); submitPublish(event.currentTarget).catch((error) => toast(error.message, true)); });
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
