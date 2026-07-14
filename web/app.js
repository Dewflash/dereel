const state = { assets: [], visibleAssets: [], projects: [], jobs: [], publishingJobs: [], accounts: [], providerResults: [], assetFilter: "all", selectedProject: null };

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
  if (view === "forge") renderProjects();
  if (view === "outputs") renderOutputs();
  if (view === "publisher") renderPublisher();
}

async function refresh() {
  const [dashboard, allAssets, filteredAssets, projects, jobs, accounts, publishing] = await Promise.all([
    api("/api/dashboard"), api("/api/assets?view=all"), api(`/api/assets?view=${state.assetFilter}`), api("/api/projects"), api("/api/jobs"), api("/api/publishing/accounts"), api("/api/publishing/jobs"),
  ]);
  state.assets = allAssets.assets || [];
  state.visibleAssets = filteredAssets.assets || [];
  state.projects = projects.projects || [];
  state.jobs = jobs.jobs || [];
  state.accounts = accounts.accounts || [];
  state.publishingJobs = publishing.jobs || [];
  renderDashboard(dashboard);
  renderAssets();
  renderAudioSelect();
  renderProjects();
  renderOutputs();
  renderPublisher();
  const active = state.jobs.find((job) => ["queued", "running"].includes(job.status));
  $("#latest-job-status").textContent = active ? `${active.message} ${Math.round(active.progress * 100)}%` : "No active jobs";
}

function renderDashboard(data) {
  const counts = data.counts || {};
  const metrics = [["ASSET LIBRARY", counts.assets || 0, "total sources"], ["PRODUCTION READY", counts.ready_assets || 0, "cleared media"], ["PROJECTS", counts.projects || 0, "edit briefs"], ["RENDERED", counts.ready_projects || 0, "ready exports"], ["OUTBOX", counts.publish_queue || 0, "queued packages"]];
  $("#metric-grid").innerHTML = metrics.map(([label, value, caption]) => `<div class="metric"><span class="eyebrow">${label}</span><strong>${value}</strong><span>${caption}</span></div>`).join("");
  const projects = data.recent_projects || [];
  $("#recent-projects").innerHTML = projects.length ? projects.map(projectRow).join("") : empty("No projects yet", "Create a brief in Forge to start an edit.");
  const jobs = data.jobs || [];
  $("#job-list").innerHTML = jobs.length ? jobs.map((job) => `<div class="job-row"><div class="row-icon">${job.kind === "render" ? "▶" : "•"}</div><div class="row-main"><strong>${escapeHtml(job.message || job.kind)}</strong><small>${escapeHtml(job.project_id || "Workspace job")}</small></div>${statusPill(job.status)}</div>`).join("") : empty("No jobs yet", "Render and analysis jobs will appear here.");
  $("#health-label").textContent = "ready";
}

function projectRow(project) {
  return `<div class="project-row"><div class="row-icon">✦</div><div class="row-main"><strong>${escapeHtml(project.name)}</strong><small>${escapeHtml(project.theme)} · ${formatDuration(project.duration_target)}</small></div>${statusPill(project.status)}<button class="row-action" data-project="${project.id}">Open</button></div>`;
}

function empty(title, detail) { return `<div class="empty-state"><span>·</span><strong>${escapeHtml(title)}</strong><p>${escapeHtml(detail)}</p></div>`; }

function renderAssets() {
  const assets = state.visibleAssets;
  $("#asset-count").textContent = `${assets.length} asset${assets.length === 1 ? "" : "s"}`;
  $("#asset-grid").innerHTML = assets.length ? assets.map(assetCard).join("") : empty("Nothing here yet", state.assetFilter === "references" ? "Save a source URL to Scout." : "Import a cleared video to populate the board.");
}

function renderProviderResults(results = []) {
  state.providerResults = results;
  $("#provider-results").innerHTML = results.length ? results.map((item) => `<article class="provider-result"><strong title="${escapeHtml(item.title)}">${escapeHtml(item.title)}</strong><small>${escapeHtml(item.provider)} · ${formatDuration(item.duration)} · ${item.width || "?"}x${item.height || "?"}</small><div class="asset-actions"><button class="mini-button" data-provider-save="${escapeHtml(item.provider_id)}">Save reference</button><button class="mini-button" data-provider-import="${escapeHtml(item.provider_id)}">Import for review</button></div></article>`).join("") : "";
}

function assetCard(asset) {
  const preview = asset.local_path ? (String(asset.mime_type || "").startsWith("audio/") ? "♫" : `<video src="/api/assets/${asset.id}/stream" muted preload="metadata"></video>`) : "↗";
  const tags = (asset.tags || []).slice(0, 4).map((tag) => `<span class="tag">${escapeHtml(tag)}</span>`).join("");
  const canApprove = asset.kind === "media" && ["pending_review", "private_reference"].includes(asset.rights_status);
  const canAdd = asset.kind === "media" && ["owned_distribution", "licensed_distribution"].includes(asset.rights_status) && ["ready", "approved"].includes(asset.status);
  const action = canApprove ? `<button class="mini-button" data-approve-asset="${asset.id}">Clear for use</button>` : canAdd ? `<button class="mini-button" data-add-asset="${asset.id}">Add to edit</button>` : `<span class="count-label">Reference only</span>`;
  return `<article class="asset-card"><div class="asset-preview">${preview}</div><div class="asset-content"><strong title="${escapeHtml(asset.title)}">${escapeHtml(asset.title)}</strong><div class="asset-meta"><span>${asset.kind === "reference" ? escapeHtml(asset.platform || "reference") : `${formatDuration(asset.duration)} · ${formatBytes(asset.file_size)}`}</span>${statusPill(asset.rights_status)}</div><div class="asset-tags">${tags}</div><div class="asset-actions">${action}<button class="mini-button danger" data-reject-asset="${asset.id}">Reject</button></div></div></article>`;
}

function renderAudioSelect() {
  const select = $("#music-asset-select");
  if (!select) return;
  const current = select.value;
  const audioAssets = state.assets.filter((asset) => String(asset.mime_type || "").startsWith("audio/") && ["owned_distribution", "licensed_distribution"].includes(asset.rights_status) && ["ready", "approved"].includes(asset.status));
  select.innerHTML = `<option value="">No music bed</option>${audioAssets.map((asset) => `<option value="${asset.id}">${escapeHtml(asset.title)} · ${formatDuration(asset.duration)}</option>`).join("")}`;
  if (audioAssets.some((asset) => asset.id === current)) select.value = current;
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
  const manifest = project.manifest || {};
  const clipMarkup = clips.map((clip, index) => { const clipOut = clip.out_point ?? clip.duration; return `<div class="timeline-clip ${clip.enabled ? "" : "disabled"}" data-project-id="${project.id}" data-clip-id="${clip.asset_id}"><b>${String(index + 1).padStart(2, "0")}</b><strong title="${escapeHtml(clip.title)}">${escapeHtml(clip.title)}</strong><div class="clip-controls"><label>In<input class="clip-in" type="number" min="0" max="${clip.duration}" step="0.1" value="${clip.in_point}" /></label><label>Out<input class="clip-out" type="number" min="0.1" max="${clip.duration}" step="0.1" value="${clipOut}" /></label></div><small>${formatDuration(clipOut - clip.in_point)} · ${escapeHtml(clip.selected_by)}</small><div class="clip-actions"><button class="mini-button" data-save-clip="${clip.asset_id}">Save</button><button class="mini-button" data-toggle-clip="${clip.asset_id}" data-enabled="${clip.enabled ? "0" : "1"}">${clip.enabled ? "Disable" : "Enable"}</button></div></div>`; }).join("");
  $("#project-inspector").innerHTML = `<div class="inspector-header"><div><span class="eyebrow accent">EDIT INSPECTOR</span><h3>${escapeHtml(project.name)}</h3><p>${escapeHtml(project.theme)} · ${formatDuration(project.duration_target)} target · ${clips.length} clips</p></div><div class="inspector-actions"><button class="button secondary" data-plan-project="${project.id}">✦ Plan edit</button><button class="button primary" data-render-project="${project.id}">▶ Render</button></div></div><div class="timeline">${clips.length ? clipMarkup : empty("No clips selected", "Plan automatically or add clips from Scout.")}</div><div class="manifest"><strong>Planner</strong> ${escapeHtml(manifest.planner || "Manual")}${manifest.selected_duration ? ` · ${formatDuration(manifest.selected_duration)} selected` : ""}<div class="progress-track"><i style="width:${project.status === "ready" ? 100 : 0}%"></i></div></div>`;
}

function renderOutputs() {
  const ready = state.projects.filter((project) => project.status === "ready" && project.output_dir);
  $("#output-list").innerHTML = ready.length ? ready.map((project) => `<article class="output-card"><div class="output-video"><video controls preload="metadata" src="/api/projects/${project.id}/output/final.mp4"></video></div><div class="output-info"><span class="eyebrow accent">${escapeHtml(project.style)} / 9:16</span><h3>${escapeHtml(project.title || project.name)}</h3><p>${escapeHtml(project.caption || project.theme)}</p><div class="output-files"><span class="file-chip">final.mp4</span><span class="file-chip">captions.srt</span><span class="file-chip">youtube.mp4</span><span class="file-chip">instagram.mp4</span><span class="file-chip">tiktok.mp4</span></div><div class="output-actions"><button class="button primary" data-publish-project="${project.id}">↗ Prepare publish</button><button class="button secondary" data-copy-path="${escapeHtml(project.output_dir)}">Copy package path</button></div></div></article>`).join("") : empty("No rendered outputs", "Your first finished edit will appear here.");
}

function renderPublisher() {
  $("#account-list").innerHTML = state.accounts.length ? state.accounts.map((account) => `<div class="account-row"><div class="row-icon">◎</div><div class="row-main"><strong>${escapeHtml(account.label)}</strong><small>${escapeHtml(account.platform)} · OAuth connection pending</small></div>${statusPill(account.status)}</div>`).join("") : empty("No channels added", "Add a destination to prepare your outbox.");
  $("#publish-count").textContent = `${state.publishingJobs.filter((job) => ["queued", "ready"].includes(job.status)).length} queued`;
  $("#publish-list").innerHTML = state.publishingJobs.length ? state.publishingJobs.map((job) => `<div class="publish-row"><div class="row-icon">↗</div><div class="row-main"><strong>${escapeHtml(job.payload?.title || job.platform)}</strong><small>${escapeHtml(job.platform)} · ${job.scheduled_for ? `scheduled ${escapeHtml(job.scheduled_for)}` : "ready for account connection"}${job.error ? ` · ${escapeHtml(job.error)}` : ""}</small></div>${statusPill(job.status)}${job.status === "ready" ? `<button class="row-action" data-run-publish="${job.id}">Publish</button><button class="row-action" data-mark-published="${job.id}">Mark posted</button>` : job.status === "failed" ? `<button class="row-action" data-run-publish="${job.id}">Retry</button>` : ""}</div>`).join("") : empty("Outbox is empty", "Rendered projects can be prepared for distribution.");
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

async function handleClick(event) {
  const target = event.target.closest("button"); if (!target) return;
  const { view, viewTarget, action, project, planProject, renderProject, approveAsset, rejectAsset, addAsset, publishProject, markPublished, runPublish, providerSave, providerImport, saveClip, toggleClip, projectId, enabled } = target.dataset;
  try {
    if (view) { showView(view); return; }
    if (viewTarget) { showView(viewTarget); return; }
    if (action === "refresh") { await refresh(); toast("Workspace refreshed"); return; }
    if (action === "new-project") { showView("forge"); $("#project-form input[name=name]").focus(); return; }
    if (project) { showView("forge"); const selected = state.projects.find((item) => item.id === project); if (selected) renderInspector(selected); return; }
    if (approveAsset) { await api(`/api/assets/${approveAsset}`, { method: "PATCH", body: JSON.stringify({ status: "approved", rights_status: "owned_distribution" }) }); toast("Asset cleared for production"); await refresh(); return; }
    if (rejectAsset) { await api(`/api/assets/${rejectAsset}`, { method: "PATCH", body: JSON.stringify({ status: "rejected", rights_status: "blocked" }) }); toast("Asset rejected"); await refresh(); return; }
    if (addAsset) { showView("forge"); const selected = state.selectedProject || (state.projects.length === 1 ? state.projects[0] : null); if (!selected) { toast("Open a project before adding a clip", true); return; } await api(`/api/projects/${selected.id}/assets`, { method: "POST", body: JSON.stringify({ asset_id: addAsset }) }); toast("Clip added to the selected project"); await refresh(); return; }
    if (saveClip || toggleClip) { const card = target.closest(".timeline-clip"); const clipProject = projectId || card?.dataset.projectId; const clipId = saveClip || toggleClip; const body = toggleClip ? { enabled: Number(enabled) } : { in_point: Number($(".clip-in", card).value), out_point: Number($(".clip-out", card).value) }; await api(`/api/projects/${clipProject}/assets/${clipId}`, { method: "POST", body: JSON.stringify(body) }); toast(toggleClip ? "Clip state updated" : "Trim points saved"); await refresh(); return; }
    if (providerSave || providerImport) { const item = state.providerResults.find((result) => result.provider_id === (providerSave || providerImport)); if (!item) { toast("Provider result expired", true); return; } if (providerSave) { await api("/api/assets/reference", { method: "POST", body: JSON.stringify({ title: item.title, source_url: item.source_url, platform: item.provider, tags: [item.provider] }) }); toast("Reference saved to Scout"); } else { await api("/api/scout/import", { method: "POST", body: JSON.stringify({ provider: item.provider, download_url: item.download_url, source_url: item.source_url, title: item.title, tags: [item.provider] }) }); toast("Provider asset imported for review"); } await refresh(); return; }
    if (target.dataset.copyPath) { await navigator.clipboard?.writeText(target.dataset.copyPath); toast("Package path copied"); return; }
    if (planProject) { await api(`/api/projects/${planProject}/plan`, { method: "POST", body: "{}" }); toast("Edit plan updated"); await refresh(); return; }
    if (renderProject) { const result = await api(`/api/projects/${renderProject}/render`, { method: "POST", body: "{}" }); toast(`Render queued: ${result.job.id}`); await refresh(); return; }
    if (publishProject) { showView("publisher"); const projectItem = state.projects.find((item) => item.id === publishProject); if (projectItem) { const platforms = ["youtube", "tiktok", "instagram"]; await api(`/api/projects/${publishProject}/publish`, { method: "POST", body: JSON.stringify({ platforms }) }); toast("Publish packages prepared"); await refresh(); } return; }
    if (runPublish) { await api(`/api/publishing/jobs/${runPublish}/publish`, { method: "POST", body: "{}" }); toast("Published successfully"); await refresh(); return; }
    if (markPublished) { await api(`/api/publishing/jobs/${markPublished}/mark-published`, { method: "POST", body: JSON.stringify({ remote_id: "manual" }) }); toast("Marked as posted"); await refresh(); return; }
  } catch (error) { toast(error.message, true); }
}

async function submitReference(form) {
  try { const data = Object.fromEntries(new FormData(form)); const result = await api("/api/assets/reference", { method: "POST", body: JSON.stringify({ ...data, tags: String(data.tags || "").split(",") }) }); toast(`${result.asset.title} saved to Scout`); form.reset(); await refresh(); } catch (error) { toast(error.message, true); }
}

async function submitProject(form) {
  try { const data = Object.fromEntries(new FormData(form)); const result = await api("/api/projects", { method: "POST", body: JSON.stringify({ ...data, music_asset_id: $("#music-asset-select")?.value || "", duration_target: Number(data.duration_target) }) }); toast("Project created"); form.reset(); $("input[name=duration_target]", form).value = 35; $("#music-asset-select").value = ""; await refresh(); renderInspector(result.project); } catch (error) { toast(error.message, true); }
}

async function submitAccount(form) {
  try { const data = Object.fromEntries(new FormData(form)); await api("/api/publishing/accounts", { method: "POST", body: JSON.stringify(data) }); toast("Channel added"); form.reset(); await refresh(); } catch (error) { toast(error.message, true); }
}

async function submitProviderSearch(form) {
  const button = $("button[type=submit]", form); button.disabled = true;
  try { const data = Object.fromEntries(new FormData(form)); const payload = await api(`/api/scout/search?provider=${encodeURIComponent(data.provider)}&q=${encodeURIComponent(data.query)}`); renderProviderResults(payload.results || []); toast(`${payload.results?.length || 0} source${payload.results?.length === 1 ? "" : "s"} found`); } catch (error) { renderProviderResults([]); toast(error.message, true); } finally { button.disabled = false; }
}

document.addEventListener("click", handleClick);
$("#upload-form").addEventListener("submit", (event) => { event.preventDefault(); submitUpload(event.currentTarget); });
$("#audio-upload-form").addEventListener("submit", (event) => { event.preventDefault(); submitAudioUpload(event.currentTarget); });
$("#reference-form").addEventListener("submit", (event) => { event.preventDefault(); submitReference(event.currentTarget); });
$("#project-form").addEventListener("submit", (event) => { event.preventDefault(); submitProject(event.currentTarget); });
$("#account-form").addEventListener("submit", (event) => { event.preventDefault(); submitAccount(event.currentTarget); });
$("#provider-search-form").addEventListener("submit", (event) => { event.preventDefault(); submitProviderSearch(event.currentTarget); });
$("#video-file").addEventListener("change", (event) => { $("#file-label").textContent = event.target.files[0]?.name || "Choose a video"; });
$("#asset-filters").addEventListener("click", async (event) => { const button = event.target.closest("button"); if (!button) return; state.assetFilter = button.dataset.filter; $$(".filter-tab").forEach((item) => item.classList.toggle("active", item === button)); try { state.visibleAssets = (await api(`/api/assets?view=${state.assetFilter}`)).assets || []; renderAssets(); } catch (error) { toast(error.message, true); } });

const initialView = location.hash.replace("#", "") || "overview";
showView(["overview", "scout", "forge", "outputs", "publisher"].includes(initialView) ? initialView : "overview");
refresh().catch((error) => { toast(error.message, true); $("#health-label").textContent = "offline"; });
setInterval(() => refresh().catch(() => {}), 2500);
