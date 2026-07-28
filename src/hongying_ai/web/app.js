const state = {
  templates: [],
  assets: [],
  runId: null,
  runIds: [],
  runSnapshots: new Map(),
  timer: null,
  streamControllers: [],
  lastRun: null,
  completedRuns: [],
  tasks: [],
  notifiedRuns: new Set(),
  directionTouched: false,
};
const $ = (id) => document.getElementById(id);

const platformLabels = {
  douyin: "抖音",
  kuaishou: "快手",
  wechat_channels: "视频号",
};
const generationDirections = [
  { id: "merchant_promo", icon: "🔥", title: "商户爆款推广", desc: "推广+真实场景+卖点证明+语音字幕" },
  { id: "knowledge_stickman", icon: "☻", title: "火柴人知识讲解", desc: "知识+静态火柴人+语音字幕" },
  { id: "knowledge_pencil", icon: "✎", title: "铅笔画知识讲解", desc: "知识+静态铅笔画+语音字幕" },
  { id: "miniature_world", icon: "🌿", title: "微缩景观小人国", desc: "故事+动态小人国+背景音乐" },
  { id: "orange_cat_daily", icon: "🐈", title: "橘猫的日常", desc: "故事+动态橘猫+背景音乐" },
  { id: "anime_drama", icon: "🎬", title: "动漫短剧", desc: "故事+动态漫画+语音字幕" },
  { id: "children_picture_book", icon: "🦁", title: "儿童绘本故事", desc: "故事+动态绘本+语音字幕" },
];
const directionDefaultVoices = {
  merchant_promo: "baidu_hot_female",
  knowledge_stickman: "baidu_hot_male",
  knowledge_pencil: "baidu_story_male",
  miniature_world: "baidu_hot_female",
  orange_cat_daily: "baidu_energy_female",
  anime_drama: "baidu_story_male",
  children_picture_book: "baidu_child",
};

function headers(json = true) {
  const value = {
    "X-Service-Name": "ops-console",
    "X-Tenant-Id": $("tenantId").value.trim(),
    "X-Trace-Id": `studio_${crypto.randomUUID()}`,
  };
  if (json) value["Content-Type"] = "application/json";
  return value;
}

async function api(path, options = {}) {
  const response = await fetch(path, options);
  let body;
  try { body = await response.json(); } catch { body = {}; }
  if (!response.ok) throw new Error(body.message || `请求失败 (${response.status})`);
  return body.data;
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[char]));
}

function splitList(value) {
  return value.split(/[,，\s]+/).map((item) => item.trim()).filter(Boolean);
}

function selectedPlatforms() {
  return [...document.querySelectorAll(".platform-check:checked")].map((node) => node.value);
}

function setSelectedPlatforms(platforms) {
  const selected = new Set(platforms?.length ? platforms : ["douyin", "kuaishou", "wechat_channels"]);
  for (const node of document.querySelectorAll(".platform-check")) {
    node.checked = selected.has(node.value);
  }
}

function selectedDirection() {
  return document.querySelector('input[name="generationDirection"]:checked')?.value || "merchant_promo";
}

function selectDirection(direction) {
  const radio = document.querySelector(`input[name="generationDirection"][value="${CSS.escape(direction || "merchant_promo")}"]`);
  if (radio) radio.checked = true;
}

function toast(message) {
  const node = $("toast");
  node.textContent = message;
  node.hidden = false;
  clearTimeout(node._timer);
  node._timer = setTimeout(() => { node.hidden = true; }, 3200);
}

function mediaKindFromFile(file) {
  if (file.type.startsWith("video/")) return "video";
  if (file.type.startsWith("image/")) return "image";
  if (file.type.startsWith("audio/")) return "audio";
  const suffix = file.name.toLowerCase().split(".").pop();
  if (["mp4", "mov", "mkv", "webm"].includes(suffix)) return "video";
  if (["jpg", "jpeg", "png", "webp"].includes(suffix)) return "image";
  return "audio";
}

function resultRoot() {
  return $("resultContent") || $("resultPanel");
}

function nowTime() {
  return new Date().toLocaleTimeString("zh-CN", { hour12: false });
}

function objectUrl(objectKey) {
  const tenantId = $("tenantId").value.trim() || "10001";
  return `/studio/objects?tenant_id=${encodeURIComponent(tenantId)}&object_key=${encodeURIComponent(objectKey)}`;
}

function resetAiProgress(title, steps = []) {
  $("aiProgress").hidden = false;
  $("aiProgressTitle").textContent = title;
  $("aiProgressPercent").textContent = "0%";
  $("aiProgressBar").style.width = "0%";
  $("aiProgressSteps").innerHTML = steps.map((step, index) => `
    <li class="${index === 0 ? "active" : ""}" data-step="${escapeHtml(step)}">
      <i></i><span>${escapeHtml(step)}</span><time>${index === 0 ? nowTime() : ""}</time>
    </li>
  `).join("");
}

function setAiProgress(percent, title) {
  const value = Math.max(0, Math.min(100, Math.round(percent)));
  $("aiProgress").hidden = false;
  if (title) $("aiProgressTitle").textContent = title;
  $("aiProgressPercent").textContent = `${value}%`;
  $("aiProgressBar").style.width = `${Math.max(2, value)}%`;
}

function updateAiStep(step, status = "active", percent = null, title = null) {
  $("aiProgress").hidden = false;
  let node = [...$("aiProgressSteps").querySelectorAll("li")]
    .find((item) => item.dataset.step === step);
  if (!node) {
    $("aiProgressSteps").insertAdjacentHTML("beforeend", `
      <li data-step="${escapeHtml(step)}"><i></i><span>${escapeHtml(step)}</span><time></time></li>
    `);
    node = $("aiProgressSteps").lastElementChild;
  }
  node.className = status;
  node.querySelector("time").textContent = nowTime();
  if (percent != null) setAiProgress(percent, title);
}

function failAiProgress(message) {
  updateAiStep(message, "failed", 100, "执行失败");
}

function stagePercent(run) {
  if (typeof run.progress === "number") return Math.round(run.progress * 100);
  return {
    WAITING: 5,
    PLANNING: 15,
    DOWNLOADING: 25,
    COMPILING: 35,
    RENDERING: 65,
    QUALITY: 85,
    UPLOADING: 94,
    COMPLETED: 100,
  }[run.stage] || 5;
}

function maybeNotifyComplete(run) {
  if (!run?.runId || run.stage !== "COMPLETED" || state.notifiedRuns.has(run.runId)) return;
  state.notifiedRuns.add(run.runId);
  if (!("Notification" in window)) return;
  const title = "宏映AI：视频已生成";
  const body = run.metadata?.activityTitle || run.metadata?.topic || "作品已通过质量检测，可预览或发布";
  if (Notification.permission === "granted") {
    new Notification(title, { body });
  } else if (Notification.permission === "default") {
    Notification.requestPermission().then((permission) => {
      if (permission === "granted") new Notification(title, { body });
    });
  }
}

function currentProgressLabel(run) {
  if (["COMPLETED", "FAILED", "CANCELLED", "TIMEOUT"].includes(run?.stage)) {
    return stageName[run.stage] || run.stage;
  }
  return run?.metadata?.aiWorkflowLabel || stageName[run?.stage] || run?.stage || "等待执行";
}

function workflowHistory(run) {
  return Array.isArray(run?.metadata?.aiWorkflowHistory)
    ? run.metadata.aiWorkflowHistory.filter((item) => item?.label)
    : [];
}

function syncWorkflowProgress(run) {
  const history = workflowHistory(run);
  history.forEach((item, index) => {
    updateAiStep(item.label, index === history.length - 1 ? "active" : "done");
  });
  updateAiStep(
    currentProgressLabel(run),
    run.stage === "COMPLETED" ? "done" : "active",
    stagePercent(run),
    currentProgressLabel(run),
  );
}

function stopRunStreams() {
  for (const controller of state.streamControllers) controller.abort();
  state.streamControllers = [];
}

async function loadTemplates() {
  const data = await api("/internal/v1/studio/templates", { headers: headers(false) });
  state.templates = data.templates;
  $("templateList").innerHTML = data.templates.map((item, index) => `
    <label class="template-card" style="--accent:${escapeHtml(item.accent)}">
      <input type="radio" name="template" value="${escapeHtml(item.id)}" ${index === 0 ? "checked" : ""}>
      <span class="template-body">
        <span class="template-kicker"><span>${item.width > item.height ? "横屏" : "竖屏"} ${item.width}:${item.height}</span><span>${item.durationMs / 1000}s</span></span>
        <h3>${escapeHtml(item.name)}</h3>
        <p>${escapeHtml(item.description)}</p>
      </span>
    </label>
  `).join("");
}

function renderDirections() {
  $("directionList").innerHTML = generationDirections.map((item, index) => `
    <label class="direction-card ${index === 0 ? "featured" : ""}">
      <input type="radio" name="generationDirection" value="${escapeHtml(item.id)}" ${index === 0 ? "checked" : ""}>
      <span class="direction-icon">${escapeHtml(item.icon)}</span>
      <span>
        <strong>${escapeHtml(item.title)}</strong>
        <em>${escapeHtml(item.desc)}</em>
      </span>
    </label>
  `).join("");
}

function typeLabel(asset) {
  return { video: "视频", image: "图片", audio: "音频" }[asset.mediaType] || "素材";
}

function renderAssets() {
  const root = $("assetList");
  if (!state.assets.length) {
    root.innerHTML = `<div class="empty">暂无素材也可以生成：AI 会自动创建默认视觉素材；也可以点击右上角“上传素材”。</div>`;
  } else {
    root.innerHTML = state.assets.map((item) => {
      const asset = item.asset;
      const score = asset.qualityScore == null ? "" : ` · 质量 ${Math.round(asset.qualityScore)}`;
      const preview = item.thumbnailUrl
        ? `<img src="${escapeHtml(item.thumbnailUrl)}" alt="${escapeHtml(item.fileName)}缩略图">`
        : `<span>${asset.mediaType === "audio" ? "♫" : "▣"}</span>`;
      return `
        <label class="asset-card">
          <input type="checkbox" class="asset-check" value="${escapeHtml(asset.assetId)}">
          <span class="asset-preview">${preview}</span>
          <span class="asset-info">
            <strong title="${escapeHtml(item.fileName)}">${escapeHtml(item.fileName)}</strong>
            <span class="asset-meta">${typeLabel(asset)} · ${(asset.durationMs / 1000).toFixed(1)}s${score}</span>
          </span>
        </label>`;
    }).join("");
  }
  const logo = state.assets.filter((item) => item.asset.mediaType === "image");
  const bgm = state.assets.filter((item) => item.asset.mediaType === "audio");
  $("logoAsset").innerHTML = `<option value="">不添加 Logo</option>${logo.map((item) => `<option value="${escapeHtml(item.asset.assetId)}">${escapeHtml(item.fileName)}</option>`).join("")}`;
  $("bgmAsset").innerHTML = `<option value="">使用素材原声</option>${bgm.map((item) => `<option value="${escapeHtml(item.asset.assetId)}">${escapeHtml(item.fileName)}</option>`).join("")}`;
}

async function loadAssets() {
  const data = await api("/internal/v1/studio/assets", { headers: headers(false) });
  state.assets = data.assets;
  renderAssets();
}

async function loadTasks() {
  const data = await api("/internal/v1/runs?limit=20", { headers: headers(false) });
  state.tasks = data.runs || [];
  renderTaskCenter();
  for (const run of state.tasks) maybeNotifyComplete(run);
}

function renderTaskCenter() {
  const root = $("taskList");
  if (!root) return;
  if (!state.tasks.length) {
    root.innerHTML = `<div class="empty compact">暂无生成任务</div>`;
    return;
  }
  root.innerHTML = state.tasks.map((run) => {
    const done = run.stage === "COMPLETED" && run.outputObjectKey;
    const output = done ? objectUrl(run.outputObjectKey) : "";
    const percent = Math.round((run.progress || 0) * 100);
    return `
      <div class="task-item" role="button" tabindex="0" data-run-id="${escapeHtml(run.runId)}">
        <strong><span>${escapeHtml(run.metadata?.activityTitle || run.metadata?.topic || "视频生成任务")}</span><em>${escapeHtml(currentProgressLabel(run))}</em></strong>
        <span>任务 ${run.taskId} · ${percent}%</span>
        <i><b style="width:${Math.max(3, percent)}%"></b></i>
        ${done ? `<a href="${output}" target="_blank" rel="noopener">查看作品</a>` : ""}
      </div>`;
  }).join("");
}

async function uploadAssets(files) {
  if (!files.length) return;
  const existingVideos = state.assets.filter((item) => item.asset.mediaType === "video").length;
  const existingImages = state.assets.filter((item) => item.asset.mediaType === "image").length;
  const incomingVideos = files.filter((file) => mediaKindFromFile(file) === "video").length;
  const incomingImages = files.filter((file) => mediaKindFromFile(file) === "image").length;
  if (existingVideos + incomingVideos > 9) {
    toast(`视频素材最多 9 段，当前素材库已有 ${existingVideos} 段`);
    return;
  }
  if (existingImages + incomingImages > 100) {
    toast(`图片素材最多 100 张，当前素材库已有 ${existingImages} 张`);
    return;
  }
  const box = $("uploadState");
  box.hidden = false;
  resetAiProgress("素材解析进度", ["上传素材", "分析媒体", "生成缩略图", "写入素材库"]);
  for (let index = 0; index < files.length; index += 1) {
    box.textContent = `正在解析 ${index + 1}/${files.length}：${files[index].name}`;
    updateAiStep("上传素材", "active", Math.round((index / files.length) * 35), `正在上传 ${files[index].name}`);
    const form = new FormData();
    form.append("file", files[index]);
    const result = await api("/internal/v1/studio/assets/upload", {
      method: "POST",
      headers: headers(false),
      body: form,
    });
    updateAiStep("分析媒体", "done", Math.round(((index + 0.6) / files.length) * 80), "正在分析素材质量");
    state.assets.unshift(result);
    renderAssets();
  }
  updateAiStep("生成缩略图", "done", 92, "缩略图和封面已就绪");
  updateAiStep("写入素材库", "done", 100, "素材解析完成");
  box.textContent = `已完成 ${files.length} 个素材的上传与解析`;
  setTimeout(() => { box.hidden = true; }, 2800);
}

function selectedAssets() {
  const selected = new Set([...document.querySelectorAll(".asset-check:checked")].map((node) => node.value));
  for (const role of [$("logoAsset").value, $("bgmAsset").value]) if (role) selected.add(role);
  return state.assets.filter((item) => selected.has(item.asset.assetId)).map((item) => item.asset);
}

function scriptPayload() {
  const platforms = selectedPlatforms();
  return {
    merchantId: $("merchantId").value.trim(),
    merchantName: $("merchantName").value.trim(),
    activityId: $("activityId").value.trim(),
    activityTitle: $("activityTitle").value.trim(),
    activityType: $("activityType").value,
    topic: $("videoTopic").value.trim(),
    targetPlatform: platforms[0] || "douyin",
    generationDirection: selectedDirection(),
    sellingPoints: splitList($("sellingPoints").value),
    durationSeconds: 15,
    useAi: true,
  };
}

function renderScriptDraft(draft) {
  $("scriptTitle").value = draft.title || "";
  $("scriptText").value = draft.narration || "";
  $("scriptTags").value = (draft.hashtags || []).join(" ");
  $("materialTerms").value = (draft.materialTerms || []).join(", ");
  const storyboard = draft.storyboard || [];
  if (storyboard.length) {
    $("storyboardPreview").hidden = false;
    $("storyboardPreview").innerHTML = `
      <h3>AI 分镜建议</h3>
      ${storyboard.map((item) => `<p>${escapeHtml(item)}</p>`).join("")}
    `;
  }
  const source = draft.modelMeta?.fallback ? "规则文案已生成" : "AI 文案已生成";
  $("scriptState").textContent = `${source}，可以继续手动调整`;
}

function detailsComplete() {
  return [
    "merchantName",
    "merchantId",
    "activityTitle",
    "activityId",
    "videoTopic",
    "userGoal",
  ].every((id) => $(id).value.trim());
}

function selectTemplate(templateId) {
  const radio = document.querySelector(`input[name="template"][value="${CSS.escape(templateId)}"]`);
  if (radio) radio.checked = true;
}

function applyAutofill(result) {
  $("merchantId").value = $("merchantId").value.trim() || result.merchantId || "";
  $("merchantName").value = $("merchantName").value.trim() || result.merchantName || "";
  $("activityId").value = result.activityId || "";
  $("activityTitle").value = result.activityTitle || "";
  $("activityType").value = result.activityType || "营销活动";
  $("videoTopic").value = result.topic || $("creationGoal").value.trim();
  $("userGoal").value = result.userGoal || $("creationGoal").value.trim();
  $("sellingPoints").value = (result.sellingPoints || []).join(", ");
  $("scriptTitle").value = result.scriptTitle || "";
  $("scriptText").value = result.scriptText || "";
  $("scriptTags").value = (result.scriptTags || []).join(" ");
  $("materialTerms").value = (result.materialTerms || []).join(", ");
  setSelectedPlatforms(result.targetPlatforms);
  if (result.templateId) selectTemplate(result.templateId);
  const options = result.options || {};
  if (options.generationDirection && !state.directionTouched) selectDirection(options.generationDirection);
  if (options.videoAspect) $("videoAspect").value = options.videoAspect;
  $("durationSeconds").value = options.durationSeconds ? String(Math.max(15, options.durationSeconds)) : "15";
  if (options.clipDurationSeconds) $("clipDuration").value = String(options.clipDurationSeconds);
  if (options.transitionMode) $("transitionMode").value = options.transitionMode;
  if (options.renderCount) $("renderCount").value = String(options.renderCount);
  if (options.ttsVoice) $("ttsVoice").value = options.ttsVoice;
  $("matchMaterials").checked = options.matchMaterialsToScript !== false;
  const storyboard = result.storyboard || [];
  if (storyboard.length) {
    $("storyboardPreview").hidden = false;
    $("storyboardPreview").innerHTML = `
      <h3>AI 分镜建议</h3>
      ${storyboard.map((item) => `<p>${escapeHtml(item)}</p>`).join("")}
    `;
  }
  const source = result.modelMeta?.fallback ? "规则方案已生成" : "AI 方案已生成";
  $("autofillState").textContent = `${source}，你可以继续修改后再生成视频。`;
  $("scriptState").textContent = "文案、话题和素材关键词已自动填写";
}

async function autoFillForm() {
  const goal = $("creationGoal").value.trim();
  if (!goal) {
    toast("请先输入想制作的目标");
    return null;
  }
  const button = $("autofillButton");
  button.disabled = true;
  $("autofillState").textContent = "AI 正在围绕制作目标生成爆款文案、关键词、分镜和视频设置…";
  resetAiProgress("AI 正在理解制作目标", ["锁定制作目标", "生成爆款文案", "提炼关键词与分镜", "推荐模板和视频设置"]);
  updateAiStep("锁定制作目标", "active", 12);
  try {
    updateAiStep("锁定制作目标", "active", 20, "AI 正在锁定用户目标和注册商户信息");
    const result = await api("/internal/v1/studio/autofill", {
      method: "POST",
      headers: headers(),
      body: JSON.stringify({
        creationGoal: goal,
        merchantId: $("merchantId").value.trim() || null,
        merchantName: $("merchantName").value.trim() || null,
        generationDirection: state.directionTouched ? selectedDirection() : null,
        targetPlatforms: selectedPlatforms(),
        useAi: true,
      }),
    });
    updateAiStep("锁定制作目标", "done", 35);
    updateAiStep("生成爆款文案", "done", 65, "AI 已生成标题、口播和 CTA");
    applyAutofill(result);
    updateAiStep("提炼关键词与分镜", "done", 86, "关键词和分镜已生成");
    updateAiStep("推荐模板和视频设置", "done", 100, "AI 方案已生成，可修改");
    toast("方案已自动填写");
    return result;
  } catch (error) {
    $("autofillState").textContent = "方案生成失败，请稍后重试";
    failAiProgress(error.message);
    toast(error.message);
    return null;
  } finally {
    button.disabled = false;
  }
}

async function draftScript() {
  if (!detailsComplete() && $("creationGoal").value.trim()) {
    await autoFillForm();
    return;
  }
  if (!$("videoTopic").value.trim()) {
    toast("请先输入制作目标，或填写视频主题");
    return;
  }
  const button = $("scriptButton");
  button.disabled = true;
  $("scriptState").textContent = "正在生成适合短视频平台的文案…";
  resetAiProgress("AI 正在重写视频文案", ["分析主题和卖点", "生成标题和口播", "生成话题和素材关键词"]);
  updateAiStep("分析主题和卖点", "active", 20);
  try {
    updateAiStep("生成标题和口播", "active", 48);
    const draft = await api("/internal/v1/studio/scripts", {
      method: "POST",
      headers: headers(),
      body: JSON.stringify(scriptPayload()),
    });
    updateAiStep("生成标题和口播", "done", 78);
    renderScriptDraft(draft);
    updateAiStep("生成话题和素材关键词", "done", 100, "文案已生成，可修改");
    toast("文案已生成");
  } catch (error) {
    $("scriptState").textContent = "文案生成失败，请稍后重试";
    failAiProgress(error.message);
    toast(error.message);
  } finally {
    button.disabled = false;
  }
}

const stageOrder = ["WAITING", "PLANNING", "DOWNLOADING", "COMPILING", "RENDERING", "QUALITY", "UPLOADING", "COMPLETED"];
const stageName = {
  WAITING: "等待执行", CREATED: "创建任务", PLANNING: "生成文案与分镜", DOWNLOADING: "下载素材",
  COMPILING: "编译时间线", RENDERING: "FFmpeg 视频合成", QUALITY: "成片质量检测",
  UPLOADING: "上传作品", COMPLETED: "作品完成", FAILED: "生成失败", CANCELLED: "已取消", TIMEOUT: "执行超时",
};

function progressView(run, runs = []) {
  const current = stageOrder.indexOf(run.stage);
  const history = workflowHistory(run);
  resultRoot().className = "result-content";
  resultRoot().innerHTML = `
    <div class="progress-card">
      <div class="progress-top"><p class="eyebrow">LANGGRAPH WORKFLOW</p><span class="stage-badge">${escapeHtml(currentProgressLabel(run))}</span></div>
      <h2>${escapeHtml(run.metadata?.activityTitle || "正在生成视频")}</h2>
      <p>任务 ${run.taskId} · ${escapeHtml(String(run.runId || "").slice(0, 16))}</p>
      <div class="progress-track"><div class="progress-bar" style="width:${Math.max(3, (run.progress || 0) * 100)}%"></div></div>
      <p>${Math.round((run.progress || 0) * 100)}% 完成</p>
      ${history.length ? `<div class="workflow-history">
        <strong>AI 实时执行节点</strong>
        <ol>${history.slice(-6).map((item, index, list) => `
          <li class="${index === list.length - 1 && run.stage !== "COMPLETED" ? "active" : "done"}">
            <i></i><span>${escapeHtml(item.label)}</span>
          </li>`).join("")}
        </ol>
      </div>` : ""}
      <ol class="stage-list">${stageOrder.slice(1, -1).map((stage, index) => {
        const absolute = index + 1;
        const className = absolute < current ? "done" : absolute === current ? "active" : "";
        return `<li class="${className}"><i></i>${stageName[stage]}</li>`;
      }).join("")}</ol>
      ${runs.length > 1 ? `<div class="variant-list">${runs.map((item, index) => variantView(item, index)).join("")}</div>` : ""}
    </div>`;
}

function variantView(run, index) {
  const done = run.stage === "COMPLETED" && run.outputObjectKey;
  const output = done ? objectUrl(run.outputObjectKey) : "";
  return `
    <div class="variant-item">
      <strong><span>候选 ${index + 1}</span><span>${stageName[run.stage] || run.stage}</span></strong>
      <p>${Math.round((run.progress || 0) * 100)}% · ${escapeHtml(String(run.runId || "").slice(0, 14))}</p>
      ${done ? `<a href="${output}" target="_blank" rel="noopener">打开候选视频</a>` : ""}
    </div>`;
}

function completedView(run, runs = []) {
  state.lastRun = run;
  state.completedRuns = runs.filter((item) => item.stage === "COMPLETED");
  maybeNotifyComplete(run);
  resultRoot().className = "result-content";
  const output = objectUrl(run.outputObjectKey);
  const previewKey = run.metadata?.previewObjectKey || run.outputObjectKey;
  const preview = objectUrl(previewKey);
  const publishDefaults = new Set(run.metadata?.targetPlatforms?.length ? run.metadata.targetPlatforms : selectedPlatforms());
  resultRoot().innerHTML = `
    <div class="video-result"><video src="${preview}" controls playsinline aria-label="生成的视频作品"></video></div>
    <div class="result-actions">
      <a href="${output}" target="_blank" rel="noopener">打开成片</a>
      <a href="${output}" download>下载作品</a>
    </div>
    ${runs.length > 1 ? `<div class="progress-card"><h2>候选视频</h2><div class="variant-list">${runs.map((item, index) => variantView(item, index)).join("")}</div></div>` : ""}
    <div class="publish-panel">
      <h3>生成后选择平台发布</h3>
      <p>请选择要发布的平台；首次发布需要登录并授权自己的平台账号。</p>
      <div class="publish-platforms">
        ${Object.entries(platformLabels).map(([value, label]) => `
          <label class="publish-platform-check">
            <input type="checkbox" class="publish-check" value="${escapeHtml(value)}" ${publishDefaults.has(value) ? "checked" : ""}>
            <span>${escapeHtml(label)}</span>
          </label>
        `).join("")}
      </div>
      <button class="publish-all" type="button" data-publish-selected="true">一键发布到选中平台</button>
      <div id="publishStatus" class="publish-status">发布标题和文案会使用左侧已确认内容；账号未绑定时会创建授权待处理任务。</div>
    </div>`;
}

async function pollRun() {
  try {
    const runs = await Promise.all(state.runIds.map((runId) => api(`/internal/v1/runs/${runId}`, { headers: headers(false) })));
    const run = runs.find((item) => item.stage === "COMPLETED") || runs[0];
    const allTerminal = runs.every((item) => ["COMPLETED", "FAILED", "CANCELLED", "TIMEOUT"].includes(item.stage));
    if (allTerminal && runs.some((item) => item.stage === "COMPLETED")) {
      clearInterval(state.timer);
      completedView(run, runs);
      updateAiStep("生成作品", "done", 100, "视频已生成并通过质量检测");
      $("generateButton").disabled = false;
      toast(runs.length > 1 ? "候选视频已生成完成" : "视频已生成并通过质量检测");
      await loadTasks().catch(console.warn);
    } else if (allTerminal) {
      clearInterval(state.timer);
      progressView(run, runs);
      resultRoot().querySelector(".progress-card").insertAdjacentHTML("beforeend", `<div class="error-box">${escapeHtml(run.errorSummary || stageName[run.stage])}</div>`);
      failAiProgress(run.errorSummary || stageName[run.stage]);
      $("generateButton").disabled = false;
      await loadTasks().catch(console.warn);
    } else {
      progressView(run, runs);
      syncWorkflowProgress(run);
    }
  } catch (error) {
    console.error(error);
  }
}

function renderStreamSnapshots() {
  const runs = state.runIds
    .map((runId) => state.runSnapshots.get(runId))
    .filter(Boolean);
  if (!runs.length) return;
  const run = runs.find((item) => item.stage === "COMPLETED") || runs[0];
  const allTerminal = runs.every((item) => ["COMPLETED", "FAILED", "CANCELLED", "TIMEOUT"].includes(item.stage));
  if (allTerminal && runs.some((item) => item.stage === "COMPLETED")) {
    completedView(run, runs);
    updateAiStep("生成作品", "done", 100, "视频已生成并通过质量检测");
    $("generateButton").disabled = false;
    loadTasks().catch(console.warn);
    return;
  }
  progressView(run, runs);
  syncWorkflowProgress(run);
}

async function streamRun(runId) {
  const controller = new AbortController();
  state.streamControllers.push(controller);
  try {
    const response = await fetch(`/internal/v1/runs/${runId}/stream`, {
      headers: headers(false),
      signal: controller.signal,
    });
    if (!response.ok || !response.body) throw new Error(`进度流连接失败 (${response.status})`);
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop() || "";
      for (const line of lines) {
        if (!line.trim()) continue;
        const run = JSON.parse(line);
        state.runSnapshots.set(run.runId, run);
        renderStreamSnapshots();
      }
    }
  } catch (error) {
    if (error.name !== "AbortError") {
      console.warn(error);
    }
  }
}

async function generate(event) {
  event.preventDefault();
  if (!detailsComplete()) {
    const filled = await autoFillForm();
    if (!filled) return;
  }
  const assets = selectedAssets();
  const selectedTemplate = document.querySelector('input[name="template"]:checked');
  if (!selectedTemplate) {
    toast("请选择视频模板");
    return;
  }
  const platforms = selectedPlatforms();
  if (!platforms.length) {
    toast("请至少选择一个发布平台");
    return;
  }
  const script = $("scriptText").value.trim();
  const goal = $("userGoal").value.trim() || $("creationGoal").value.trim();
  const payload = {
    merchantId: $("merchantId").value.trim(),
    merchantName: $("merchantName").value.trim(),
    activityId: $("activityId").value.trim(),
    activityTitle: $("activityTitle").value.trim(),
    activityType: $("activityType").value,
    userGoal: goal || script || $("videoTopic").value.trim(),
    topic: $("videoTopic").value.trim(),
    script: script || null,
    targetPlatforms: platforms,
    templateId: selectedTemplate.value,
    assets,
    logoAssetId: $("logoAsset").value || null,
    bgmAssetId: $("bgmAsset").value || null,
    sellingPoints: splitList($("sellingPoints").value),
    materialTerms: splitList($("materialTerms").value),
    options: {
      videoAspect: $("videoAspect").value,
      durationSeconds: $("durationSeconds").value ? Number($("durationSeconds").value) : null,
      clipDurationSeconds: Number($("clipDuration").value),
      transitionMode: $("transitionMode").value,
      matchMaterialsToScript: $("matchMaterials").checked,
      renderCount: Number($("renderCount").value),
      generationDirection: selectedDirection(),
      ttsVoice: $("ttsVoice").value,
    },
    useAi: true,
  };
  $("generateButton").disabled = true;
  stopRunStreams();
  state.runSnapshots.clear();
  resetAiProgress("AI 视频生成进度", ["提交任务", "生成文案与分镜", "视觉素材生成", "配音与时间线", "FFmpeg 合成", "质量检测", "生成作品"]);
  updateAiStep("提交任务", "active", 5);
  try {
    const result = await api("/internal/v1/studio/generations", {
      method: "POST",
      headers: headers(),
      body: JSON.stringify(payload),
    });
    updateAiStep("提交任务", "done", 10);
    updateAiStep("生成文案与分镜", "active", 15);
    const runs = result.runs?.length ? result.runs : [result];
    state.runIds = runs.map((item) => item.runId);
    state.runId = state.runIds[0];
    await loadTasks().catch(console.warn);
    for (const run of runs) {
      state.runSnapshots.set(run.runId, run);
      streamRun(run.runId);
    }
    progressView({ ...result, progress: 0, metadata: { activityTitle: payload.activityTitle } });
    clearInterval(state.timer);
    state.timer = setInterval(pollRun, 1800);
    await pollRun();
  } catch (error) {
    $("generateButton").disabled = false;
    failAiProgress(error.message);
    toast(error.message);
  }
}

async function publish(platforms) {
  if (!state.lastRun?.runId) {
    toast("请先生成视频");
    return;
  }
  const status = $("publishStatus");
  status.textContent = "正在创建发布任务…";
  resetAiProgress("AI 发布进度", ["整理发布文案", "创建发布任务", "等待平台账号绑定"]);
  updateAiStep("整理发布文案", "active", 25);
  try {
    updateAiStep("创建发布任务", "active", 62);
    const result = await api("/internal/v1/studio/publications", {
      method: "POST",
      headers: headers(),
      body: JSON.stringify({
        runId: state.lastRun.runId,
        platforms,
        title: $("scriptTitle").value.trim() || $("activityTitle").value.trim(),
        description: $("scriptText").value.trim(),
        hashtags: splitList($("scriptTags").value),
      }),
    });
    status.innerHTML = result.platforms.map((item) => {
      const label = platformLabels[item.platform] || item.platform;
      return `<p>${label}：${escapeHtml(item.message)}</p>`;
    }).join("");
    updateAiStep("创建发布任务", "done", 85);
    updateAiStep("等待平台账号绑定", "done", 100, "发布任务已创建");
    toast("发布任务已创建");
  } catch (error) {
    status.textContent = error.message;
    failAiProgress(error.message);
    toast(error.message);
  }
}

async function init() {
  renderDirections();
  try {
    await Promise.all([loadTemplates(), loadAssets(), loadTasks()]);
  } catch (error) {
    toast(error.message);
    $("assetList").innerHTML = `<div class="empty">无法连接素材库，请检查 MinIO 与服务配置。</div>`;
  }
  $("autofillButton").addEventListener("click", autoFillForm);
  $("scriptButton").addEventListener("click", draftScript);
  $("directionList").addEventListener("change", (event) => {
    if (event.target?.name !== "generationDirection") return;
    state.directionTouched = true;
    const voice = directionDefaultVoices[selectedDirection()];
    if (voice) $("ttsVoice").value = voice;
  });
  $("assetUpload").addEventListener("change", async (event) => {
    try { await uploadAssets([...event.target.files]); }
    catch (error) { toast(error.message); }
    event.target.value = "";
  });
  $("studioForm").addEventListener("submit", generate);
  $("refreshTasks").addEventListener("click", () => loadTasks().catch((error) => toast(error.message)));
  $("tenantId").addEventListener("change", () => {
    loadAssets().catch((error) => toast(error.message));
    loadTasks().catch((error) => toast(error.message));
  });
  $("taskList").addEventListener("click", async (event) => {
    if (event.target.closest("a")) return;
    const button = event.target.closest(".task-item");
    if (!button?.dataset.runId) return;
    state.runIds = [button.dataset.runId];
    state.runId = button.dataset.runId;
    await pollRun();
  });
  $("resultPanel").addEventListener("click", (event) => {
    const button = event.target.closest("button");
    if (!button) return;
    if (button.dataset.publishSelected) {
      const platforms = [...document.querySelectorAll(".publish-check:checked")].map((node) => node.value);
      if (!platforms.length) {
        toast("请至少选择一个发布平台");
        return;
      }
      publish(platforms);
    }
  });
}

init();
