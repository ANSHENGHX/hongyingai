const state = { templates: [], assets: [], runId: null, timer: null };
const $ = (id) => document.getElementById(id);

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

function toast(message) {
  const node = $("toast");
  node.textContent = message;
  node.hidden = false;
  clearTimeout(node._timer);
  node._timer = setTimeout(() => { node.hidden = true; }, 3200);
}

async function loadTemplates() {
  const data = await api("/internal/v1/studio/templates", { headers: headers(false) });
  state.templates = data.templates;
  $("templateList").innerHTML = data.templates.map((item, index) => `
    <label class="template-card" style="--accent:${item.accent}">
      <input type="radio" name="template" value="${item.id}" ${index === 0 ? "checked" : ""}>
      <span class="template-body">
        <span class="template-kicker"><span>${item.width > item.height ? "横屏" : "竖屏"} ${item.width}:${item.height}</span><span>${item.durationMs / 1000}s</span></span>
        <h3>${item.name}</h3>
        <p>${item.description}</p>
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
    root.innerHTML = `<div class="empty">暂无素材，点击右上角“上传素材”开始。</div>`;
  } else {
    root.innerHTML = state.assets.map((item) => {
      const asset = item.asset;
      const score = asset.qualityScore == null ? "" : ` · 质量 ${Math.round(asset.qualityScore)}`;
      const preview = item.thumbnailUrl
        ? `<img src="${item.thumbnailUrl}" alt="${item.fileName}缩略图">`
        : `<span>${asset.mediaType === "audio" ? "♫" : "▣"}</span>`;
      return `
        <label class="asset-card">
          <input type="checkbox" class="asset-check" value="${asset.assetId}" ${asset.mediaType !== "audio" ? "checked" : ""}>
          <span class="asset-preview">${preview}</span>
          <span class="asset-info">
            <strong title="${item.fileName}">${item.fileName}</strong>
            <span class="asset-meta">${typeLabel(asset)} · ${(asset.durationMs / 1000).toFixed(1)}s${score}</span>
          </span>
        </label>`;
    }).join("");
  }
  const logo = state.assets.filter((item) => item.asset.mediaType === "image");
  const bgm = state.assets.filter((item) => item.asset.mediaType === "audio");
  $("logoAsset").innerHTML = `<option value="">不添加 Logo</option>${logo.map((item) => `<option value="${item.asset.assetId}">${item.fileName}</option>`).join("")}`;
  $("bgmAsset").innerHTML = `<option value="">使用素材原声</option>${bgm.map((item) => `<option value="${item.asset.assetId}">${item.fileName}</option>`).join("")}`;
}

async function loadAssets() {
  const data = await api("/internal/v1/studio/assets", { headers: headers(false) });
  state.assets = data.assets;
  renderAssets();
}

async function uploadAssets(files) {
  if (!files.length) return;
  const box = $("uploadState");
  box.hidden = false;
  for (let index = 0; index < files.length; index += 1) {
    box.textContent = `正在解析 ${index + 1}/${files.length}：${files[index].name}`;
    const form = new FormData();
    form.append("file", files[index]);
    const result = await api("/internal/v1/studio/assets/upload", {
      method: "POST",
      headers: headers(false),
      body: form,
    });
    state.assets.unshift(result);
    renderAssets();
  }
  box.textContent = `已完成 ${files.length} 个素材的上传与解析`;
  setTimeout(() => { box.hidden = true; }, 2800);
}

function selectedAssets() {
  const selected = new Set([...document.querySelectorAll(".asset-check:checked")].map((node) => node.value));
  for (const role of [$("logoAsset").value, $("bgmAsset").value]) if (role) selected.add(role);
  return state.assets.filter((item) => selected.has(item.asset.assetId)).map((item) => item.asset);
}

const stageOrder = ["WAITING", "PLANNING", "DOWNLOADING", "COMPILING", "RENDERING", "QUALITY", "UPLOADING", "COMPLETED"];
const stageName = {
  WAITING: "等待执行", CREATED: "创建任务", PLANNING: "AI 创意规划", DOWNLOADING: "下载素材",
  COMPILING: "编译时间线", RENDERING: "FFmpeg 视频合成", QUALITY: "成片质量检测",
  UPLOADING: "上传作品", COMPLETED: "作品完成", FAILED: "生成失败", CANCELLED: "已取消", TIMEOUT: "执行超时",
};

function progressView(run) {
  const current = stageOrder.indexOf(run.stage);
  $("resultPanel").innerHTML = `
    <div class="progress-card">
      <div class="progress-top"><p class="eyebrow">GENERATING</p><span class="stage-badge">${stageName[run.stage] || run.stage}</span></div>
      <h2>${run.metadata?.activityTitle || "正在生成视频"}</h2>
      <p>任务 ${run.taskId} · ${run.runId.slice(0, 16)}</p>
      <div class="progress-track"><div class="progress-bar" style="width:${Math.max(3, run.progress * 100)}%"></div></div>
      <p>${Math.round(run.progress * 100)}% 完成</p>
      <ol class="stage-list">${stageOrder.slice(1, -1).map((stage, index) => {
        const absolute = index + 1;
        const className = absolute < current ? "done" : absolute === current ? "active" : "";
        return `<li class="${className}"><i></i>${stageName[stage]}</li>`;
      }).join("")}</ol>
    </div>`;
}

function completedView(run) {
  const output = `/internal/v1/studio/objects?object_key=${encodeURIComponent(run.outputObjectKey)}`;
  const previewKey = run.metadata?.previewObjectKey || run.outputObjectKey;
  const preview = `/internal/v1/studio/objects?object_key=${encodeURIComponent(previewKey)}`;
  $("resultPanel").innerHTML = `
    <div class="video-result"><video src="${preview}" controls playsinline aria-label="生成的视频作品"></video></div>
    <div class="result-actions">
      <a href="${output}" target="_blank" rel="noopener">打开成片</a>
      <a href="${output}" download>下载作品</a>
    </div>`;
}

async function pollRun() {
  try {
    const run = await api(`/internal/v1/runs/${state.runId}`, { headers: headers(false) });
    if (run.stage === "COMPLETED") {
      clearInterval(state.timer);
      completedView(run);
      $("generateButton").disabled = false;
      toast("视频已生成并通过质量检测");
    } else if (["FAILED", "CANCELLED", "TIMEOUT"].includes(run.stage)) {
      clearInterval(state.timer);
      progressView(run);
      $("resultPanel").querySelector(".progress-card").insertAdjacentHTML("beforeend", `<div class="error-box">${run.errorSummary || stageName[run.stage]}</div>`);
      $("generateButton").disabled = false;
    } else {
      progressView(run);
    }
  } catch (error) {
    console.error(error);
  }
}

async function generate(event) {
  event.preventDefault();
  const assets = selectedAssets();
  const visualAssets = assets.filter((item) => ["video", "image"].includes(item.mediaType) && item.assetId !== $("logoAsset").value);
  if (!visualAssets.length) {
    toast("请至少选择一个视频或图片素材");
    return;
  }
  const selectedTemplate = document.querySelector('input[name="template"]:checked');
  if (!selectedTemplate) {
    toast("请选择视频模板");
    return;
  }
  const payload = {
    merchantId: $("merchantId").value.trim(),
    merchantName: $("merchantName").value.trim(),
    activityId: $("activityId").value.trim(),
    activityTitle: $("activityTitle").value.trim(),
    activityType: $("activityType").value,
    userGoal: $("userGoal").value.trim(),
    templateId: selectedTemplate.value,
    assets,
    logoAssetId: $("logoAsset").value || null,
    bgmAssetId: $("bgmAsset").value || null,
    sellingPoints: $("sellingPoints").value.split(/[,，]/).map((value) => value.trim()).filter(Boolean),
    useAi: true,
  };
  $("generateButton").disabled = true;
  try {
    const result = await api("/internal/v1/studio/generations", {
      method: "POST",
      headers: headers(),
      body: JSON.stringify(payload),
    });
    state.runId = result.runId;
    progressView({ ...result, progress: 0, metadata: { activityTitle: payload.activityTitle } });
    clearInterval(state.timer);
    state.timer = setInterval(pollRun, 1800);
    await pollRun();
  } catch (error) {
    $("generateButton").disabled = false;
    toast(error.message);
  }
}

async function init() {
  try {
    await Promise.all([loadTemplates(), loadAssets()]);
  } catch (error) {
    toast(error.message);
    $("assetList").innerHTML = `<div class="empty">无法连接素材库，请检查 MinIO 与服务配置。</div>`;
  }
  $("assetUpload").addEventListener("change", async (event) => {
    try { await uploadAssets([...event.target.files]); }
    catch (error) { toast(error.message); }
    event.target.value = "";
  });
  $("studioForm").addEventListener("submit", generate);
  $("tenantId").addEventListener("change", () => loadAssets().catch((error) => toast(error.message)));
}

init();
