/*
 * TTS-Hub 管理台前端（《方案设计.md》§8.3）。
 *
 * 三条约束，读代码时请一并守住：
 * 1. **零依赖**：不引任何框架或 CDN，纯 DOM + fetch；
 * 2. **不拼 HTML**：所有文本都走 textContent，页面里没有一处 innerHTML ——
 *    厂商返回的报错、音色名都是不可信输入，拼字符串就是在给自己挖 XSS 的坑；
 * 3. **不猜数据形状**：字段名与 /openapi.json 和后端 DTO 保持一致，缺字段就显示"—"。
 */

const $ = (id) => document.getElementById(id);

// ── 通用工具 ──────────────────────────────────────────────────────────

/** 建节点。props 里 `text` 走 textContent，`on*` 走事件，其余走 setAttribute。 */
function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === null || value === undefined) continue;
    if (key === "text") node.textContent = String(value);
    else if (key === "class") node.className = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value);
  }
  for (const child of [].concat(children)) {
    if (child !== null && child !== undefined && child !== false) node.append(child);
  }
  return node;
}

function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

function banner(message, kind = "") {
  const box = $("banner");
  box.textContent = message || "";
  box.className = "banner " + kind;
  box.hidden = !message;
}

/** 从 `{error: {...}}` 里取可读消息；取不到就退回状态码。 */
async function failure(res) {
  let detail = null;
  try {
    detail = await res.json();
  } catch (err) {
    detail = null;
  }
  const info = (detail && detail.error) || {};
  const text = info.message || (detail && detail.detail) || `HTTP ${res.status}`;
  const code = info.code ? `（${info.code}）` : "";
  return `${text}${code}`;
}

async function getJSON(path) {
  const res = await fetch(path);
  if (!res.ok) throw new Error(await failure(res));
  return res.json();
}

async function sendJSON(path, body, method = "POST") {
  const res = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(await failure(res));
  return res.json();
}

async function deleteJSON(path) {
  const res = await fetch(path, { method: "DELETE" });
  if (!res.ok) throw new Error(await failure(res));
  return res.json();
}

/** 取音频：成功是二进制流，失败是 JSON 错误体——两条路都要能读懂。 */
async function postAudio(path, body) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) return { ok: false, error: await failure(res) };
  const blob = await res.blob();
  return {
    ok: true,
    blob,
    vendor: res.headers.get("X-TTS-Vendor") || body.vendor || "",
    model: res.headers.get("X-TTS-Model") || "",
    latency: Number(res.headers.get("X-TTS-Latency-Ms") || 0),
    chars: Number(res.headers.get("X-TTS-Chars") || 0),
  };
}

function dash(value) {
  return value === null || value === undefined || value === "" ? "—" : String(value);
}

// ── 状态 ──────────────────────────────────────────────────────────────

const state = {
  vendors: [],
  voices: [],
  picked: new Set(),
  objects: [], // 待回收的 blob URL，重跑试听前统一 revoke
};

async function refreshVendors() {
  const data = await getJSON("/api/vendors");
  state.vendors = data.vendors || [];
  if (state.picked.size === 0) {
    for (const row of state.vendors) {
      // 默认勾上"可用"的厂商：启用了、有密钥
      if (row.enabled && row.has_key) state.picked.add(row.vendor);
    }
  }
  renderVendorChips();
  renderVendorSelects();
  renderVendorsTable();
}

async function refreshVoices() {
  const data = await getJSON("/api/voices");
  state.voices = data.voices || [];
  renderVoicesTable();
  const list = $("voice-options");
  clear(list);
  for (const voice of state.voices) {
    list.append(el("option", { value: voice.name, text: `${voice.name}（${voice.id}）` }));
  }
}

// ── 调音台 ────────────────────────────────────────────────────────────

function renderVendorChips() {
  const box = $("tuner-vendors");
  clear(box);
  if (state.vendors.length === 0) {
    box.append(el("span", { class: "hint", text: "没有可用厂商：检查 providers.yaml 与密钥" }));
    return;
  }
  for (const row of state.vendors) {
    const on = state.picked.has(row.vendor);
    box.append(
      el("span", {
        class: `chip ${on ? "on" : "off"}`,
        text: `${row.vendor}${row.has_key ? "" : "（无密钥）"}`,
        title: row.enabled ? "" : "该厂商已停用",
        onclick: () => {
          if (on) state.picked.delete(row.vendor);
          else state.picked.add(row.vendor);
          renderVendorChips();
        },
      }),
    );
  }
}

function logicalVoice(text) {
  const needle = (text || "").trim();
  return state.voices.find((v) => v.id === needle || v.name === needle) || null;
}

function renderTunerCurrent() {
  const voice = logicalVoice($("tuner-voice").value);
  const box = $("tuner-current");
  clear(box);
  if (!voice) {
    box.append(el("span", { text: "当前音色不是逻辑音色，无法设默认（只有注册表里的音色才有绑定的概念）。" }));
    return;
  }
  const defaultVendor = voice.bindings.find((b) => b.preferred);
  box.append(
    el("span", {
      text: defaultVendor
        ? `逻辑音色「${voice.name}」的默认厂商是 ${defaultVendor.vendor}`
        : `逻辑音色「${voice.name}」还没设默认厂商，speak 不带 vendor 时会走全局 default_vendor`,
    }),
  );
}

async function runTuner() {
  const text = $("tuner-text").value;
  const voice = $("tuner-voice").value.trim();
  const model = $("tuner-model").value.trim() || null;
  const targets = [...state.picked];

  for (const url of state.objects.splice(0)) URL.revokeObjectURL(url);
  clear($("tuner-results"));
  banner("");

  if (!text || !voice) {
    banner("文本与音色都必须填。", "bad");
    return;
  }
  if (targets.length === 0) {
    banner("至少要勾一家厂商。", "bad");
    return;
  }

  const button = $("tuner-run");
  button.disabled = true;
  button.textContent = `合成中（${targets.length} 家）…`;
  const logical = logicalVoice(voice);
  const results = await Promise.all(
    targets.map(async (vendor) => {
      const started = performance.now();
      const outcome = await postAudio("/api/tts", { text, voice, vendor, model });
      return { vendor, outcome, wall: Math.round(performance.now() - started) };
    }),
  );
  button.disabled = false;
  button.textContent = "并排试听";
  renderTunerResults(results, logical);
}

function renderTunerResults(results, logical) {
  const box = $("tuner-results");
  clear(box);
  for (const { vendor, outcome, wall } of results) {
    const card = el("div", { class: "card" });
    card.append(el("h3", { text: vendor }));
    if (!outcome.ok) {
      card.append(el("div", { class: "err", text: outcome.error }));
      box.append(card);
      continue;
    }
    const url = URL.createObjectURL(outcome.blob);
    state.objects.push(url);
    card.append(
      el("div", {
        class: "meta",
        text: `${dash(outcome.model)} · ${outcome.chars || 0} 字符 · 首字节 ${outcome.latency || wall} ms · 端到端 ${wall} ms`,
      }),
      el("audio", { controls: "", src: url }),
    );
    if (logical) {
      const isDefault = logical.bindings.some((b) => b.preferred && b.vendor === vendor);
      if (isDefault) card.className = "card default";
      const bound = logical.bindings.some((b) => b.vendor === vendor);
      card.append(
        el("button", {
          text: isDefault ? "当前默认" : bound ? "设为默认" : "无绑定，无法设默认",
          disabled: isDefault || !bound ? "" : null,
          title: bound ? "" : "这个逻辑音色在该厂商下没有绑定记录",
          onclick: async () => {
            try {
              await sendJSON(`/api/voices/${logical.id}/default`, { vendor });
              await refreshVoices();
              renderTunerCurrent();
              banner(`已把「${logical.name}」的默认厂商设为 ${vendor}。`);
            } catch (err) {
              banner(err.message, "bad");
            }
          },
        }),
      );
    }
    box.append(card);
  }
}

// ── 音色库 ────────────────────────────────────────────────────────────

function renderVoicesTable() {
  const body = $("voices-table").querySelector("tbody");
  clear(body);
  if (state.voices.length === 0) {
    body.append(el("tr", {}, [el("td", { colspan: "5", class: "hint", text: "还没有逻辑音色。" })]));
    return;
  }
  for (const voice of state.voices) {
    body.append(
      el("tr", {}, [
        el("td", { text: voice.name }),
        el("td", { class: "mono", text: voice.id }),
        el("td", { text: dash(voice.tags) }),
        el("td", {
          text: voice.bindings.length
            ? voice.bindings.map((b) => `${b.vendor}${b.preferred ? "★" : ""}`).join("、")
            : "—",
        }),
        el("td", {}, [
          el("button", { text: "详情", onclick: () => showVoiceDetail(voice.id) }),
          el("button", {
            text: "删除",
            onclick: async () => {
              if (!confirm(`删除逻辑音色「${voice.name}」及其全部绑定？`)) return;
              try {
                await deleteJSON(`/api/voices/${voice.id}`);
                await refreshVoices();
                banner(`已删除「${voice.name}」。（厂商侧的音色不受影响）`);
              } catch (err) {
                banner(err.message, "bad");
              }
            },
          }),
        ]),
      ]),
    );
  }
}

async function showVoiceDetail(voiceId) {
  const box = $("voice-detail");
  clear(box);
  let detail;
  try {
    detail = await getJSON(`/api/voices/${voiceId}`);
  } catch (err) {
    banner(err.message, "bad");
    return;
  }

  box.append(el("h3", { text: `「${detail.name}」的绑定` }));
  const body = el("tbody");
  if (detail.bindings.length === 0) {
    body.append(el("tr", {}, [el("td", { colspan: "7", class: "hint", text: "没有绑定。" })]));
  }
  for (const binding of detail.bindings) {
    body.append(
      el("tr", {}, [
        el("td", { text: binding.vendor }),
        el("td", { class: "mono", text: dash(binding.vendor_voice_id) }),
        el("td", { text: dash(binding.model) }),
        el("td", {}, [el("span", { class: `tag ${binding.status === "ready" ? "ok" : "muted"}`, text: binding.status })]),
        el("td", { text: binding.preferred ? "★ 默认" : "—" }),
        el("td", { text: dash(binding.expires_at) }),
        el("td", {}, [
          el("button", {
            text: "试听样本",
            onclick: () => playSample(detail.id, binding.id),
          }),
          el("button", {
            text: "设为默认",
            disabled: binding.preferred ? "" : null,
            onclick: async () => {
              try {
                await sendJSON(`/api/voices/${detail.id}/default`, { vendor: binding.vendor });
                await refreshVoices();
                showVoiceDetail(detail.id);
              } catch (err) {
                banner(err.message, "bad");
              }
            },
          }),
          el("button", {
            text: "解绑",
            onclick: async () => {
              try {
                await deleteJSON(`/api/voices/${detail.id}/bindings/${binding.id}`);
                await refreshVoices();
                showVoiceDetail(detail.id);
              } catch (err) {
                banner(err.message, "bad");
              }
            },
          }),
        ]),
      ]),
    );
  }
  box.append(
    el("table", { class: "grid" }, [
      el("thead", {}, [
        el("tr", {}, [
          el("th", { text: "厂商" }),
          el("th", { text: "厂商音色 ID" }),
          el("th", { text: "模型" }),
          el("th", { text: "状态" }),
          el("th", { text: "默认" }),
          el("th", { text: "TTL 到期" }),
          el("th", { text: "操作" }),
        ]),
      ]),
      body,
    ]),
  );

  // 补一条绑定的入口：绑定也可以手工登记（例如厂商控制台里已经建好的音色）
  const vendorSelect = el("select", {});
  for (const row of state.vendors) vendorSelect.append(el("option", { value: row.vendor, text: row.vendor }));
  const vendorVoiceInput = el("input", { placeholder: "厂商侧音色 ID" });
  box.append(
    el("div", { class: "row" }, [
      el("span", { class: "label", text: "手工补一条绑定" }),
      vendorSelect,
      vendorVoiceInput,
      el("button", {
        text: "绑定",
        onclick: async () => {
          try {
            await sendJSON(`/api/voices/${detail.id}/bindings`, {
              vendor: vendorSelect.value,
              vendor_voice_id: vendorVoiceInput.value.trim(),
            });
            await refreshVoices();
            showVoiceDetail(detail.id);
          } catch (err) {
            banner(err.message, "bad");
          }
        },
      }),
      el("button", {
        text: "改名/改标签",
        onclick: async () => {
          const name = prompt("新名字（留空则不改）", detail.name);
          if (name === null) return;
          const tags = prompt("新标签（留空表示清空）", detail.tags || "");
          if (tags === null) return;
          try {
            await sendJSON(
              `/api/voices/${detail.id}`,
              { name: name.trim() || null, tags: tags.trim() },
              "PATCH",
            );
            await refreshVoices();
            showVoiceDetail(detail.id);
          } catch (err) {
            banner(err.message, "bad");
          }
        },
      }),
    ]),
  );
}

async function playSample(voiceId, bindingId) {
  const res = await fetch(`/api/voices/${voiceId}/bindings/${bindingId}/sample`);
  if (!res.ok) {
    banner(await failure(res), "bad");
    return;
  }
  const url = URL.createObjectURL(await res.blob());
  state.objects.push(url);
  const audio = new Audio(url);
  audio.play().catch((err) => banner(`播放失败：${err.message}`, "bad"));
}

async function runClone() {
  const name = $("clone-name").value.trim();
  const file = $("clone-file").files[0];
  if (!name || !file) {
    banner("复刻至少要给「名字」和「样本文件」。", "bad");
    return;
  }
  const form = new FormData();
  form.append("name", name);
  form.append("file", file);
  const vendor = $("clone-vendor").value;
  if (vendor) form.append("vendor", vendor);
  for (const [id, field] of [
    ["clone-model", "model"],
    ["clone-transcript", "transcript"],
    ["clone-preview", "preview_text"],
  ]) {
    const value = $(id).value.trim();
    if (value) form.append(field, value);
  }

  const button = $("clone-run");
  button.disabled = true;
  button.textContent = "复刻中…";
  $("clone-status").textContent = "已提交，等待厂商返回…";
  try {
    // 标签不在 /api/clone 的表单里：先建逻辑音色（同名幂等），再复刻时会复用它
    const tags = $("clone-tags").value.trim();
    if (tags) await sendJSON("/api/voices", { name, tags });
    const res = await fetch("/api/clone", { method: "POST", body: form });
    if (!res.ok) throw new Error(await failure(res));
    const data = await res.json();
    $("clone-status").textContent = `完成：音色 ${data.voice_id}（任务状态 ${data.task.status}）`;
    banner(`「${data.name}」已复刻并绑定。`);
    await refreshVoices();
  } catch (err) {
    $("clone-status").textContent = "";
    banner(err.message, "bad");
  } finally {
    button.disabled = false;
    button.textContent = "上传并复刻";
  }
}

// ── 厂商 ──────────────────────────────────────────────────────────────

function renderVendorSelects() {
  for (const id of ["clone-vendor", "log-vendor"]) {
    const select = $(id);
    const keep = select.value;
    clear(select);
    if (id === "log-vendor") select.append(el("option", { value: "", text: "全部" }));
    for (const row of state.vendors) {
      select.append(el("option", { value: row.vendor, text: row.vendor }));
    }
    if (keep) select.value = keep;
  }
}

function renderVendorsTable() {
  const body = $("vendors-table").querySelector("tbody");
  clear(body);
  for (const row of state.vendors) {
    body.append(
      el("tr", {}, [
        el("td", { text: row.vendor }),
        el("td", {}, [
          el("button", {
            text: row.enabled ? "停用" : "启用",
            onclick: async () => {
              try {
                // 两个路径都写成字面量：测试会扫描本文件、核对每个 /api 路径真实存在，
                // 拼出来的路径扫不到，也就失去了那道防漂移的保障。
                const path = row.enabled
                  ? `/api/vendors/${row.vendor}/disable`
                  : `/api/vendors/${row.vendor}/enable`;
                await sendJSON(path, {});
                await refreshVendors();
                banner(`${row.vendor} 已${row.enabled ? "停用" : "启用"}。`);
              } catch (err) {
                banner(err.message, "bad");
              }
            },
          }),
        ]),
        el("td", {}, [
          el("span", {
            class: `tag ${row.has_key ? "ok" : "bad"}`,
            text: row.has_key ? "已配置" : `缺 ${row.api_key_env}`,
          }),
        ]),
        el("td", { text: dash(row.default_model) }),
        el("td", { text: dash(row.clone_model) }),
        el("td", { text: dash(row.pricing) }),
        el("td", {}, [el("button", { text: "查看模型", onclick: () => showVendorModels(row.vendor) })]),
      ]),
    );
  }
}

async function showVendorModels(vendor) {
  const box = $("vendor-models");
  clear(box);
  try {
    const data = await getJSON(`/api/vendors/${vendor}/models`);
    const body = el("tbody");
    for (const model of data.models) {
      body.append(
        el("tr", {}, [
          el("td", { class: "mono", text: model.id }),
          el("td", { text: dash(model.display_name) }),
          el("td", { text: model.supports_clone ? "是" : "否" }),
          el("td", { text: model.supports_stream ? "是" : "否" }),
          el("td", { text: model.char_limit ? `${model.char_limit} 字符` : "—" }),
          el("td", { class: "hint", text: dash(model.note) }),
        ]),
      );
    }
    box.append(
      el("h3", { text: `${vendor} 的模型与限额` }),
      el("table", { class: "grid" }, [
        el("thead", {}, [
          el("tr", {}, [
            el("th", { text: "模型 ID" }),
            el("th", { text: "名称" }),
            el("th", { text: "可复刻" }),
            el("th", { text: "可流式" }),
            el("th", { text: "单次上限" }),
            el("th", { text: "备注" }),
          ]),
        ]),
        body,
      ]),
    );
  } catch (err) {
    banner(err.message, "bad");
  }
}

async function refreshExpiring() {
  const box = $("expiring");
  try {
    const data = await getJSON("/api/expiring");
    const rows = data.bindings || [];
    box.hidden = rows.length === 0;
    box.textContent = rows.length
      ? `注意：${rows.length} 条绑定已过 TTL（厂商可能已删除该音色）——${rows
          .map((r) => `${r.vendor}/${r.voice_id}`)
          .join("、")}`
      : "";
  } catch (err) {
    box.hidden = true;
  }
}

// ── 成本看板 ──────────────────────────────────────────────────────────

async function runCost() {
  const range = $("cost-range").value;
  const by = $("cost-by").value;
  let data;
  try {
    data = await getJSON(`/api/cost?range=${encodeURIComponent(range)}&by=${encodeURIComponent(by)}`);
  } catch (err) {
    banner(err.message, "bad");
    return;
  }
  $("cost-total").textContent = `近 ${data.days} 天共 ${data.rows.reduce((n, r) => n + r.calls, 0)} 次调用，估算合计 ${Number(data.total).toFixed(4)} ${data.currency}`;
  const rows = data.rows || [];
  const peak = rows.reduce((n, r) => Math.max(n, Number(r.cost) || 0), 0) || 1;
  const body = el("tbody");
  for (const row of rows) {
    const share = Math.round(((Number(row.cost) || 0) / peak) * 100);
    body.append(
      el("tr", {}, [
        el("td", { text: dash(row.bucket) }),
        el("td", { text: row.calls }),
        el("td", { text: row.ok_calls }),
        el("td", { text: row.chars }),
        el("td", { text: `${Math.round(Number(row.avg_latency_ms) || 0)} ms` }),
        el("td", { text: Number(row.cost).toFixed(4) }),
        el("td", {}, [el("div", { class: "bar", style: `width:${share}%` })]),
      ]),
    );
  }
  const table = $("cost-table");
  const old = table.querySelector("tbody");
  if (old) old.replaceWith(body);
  else table.append(body);
}

// ── 调用日志 ──────────────────────────────────────────────────────────

async function runLogs() {
  const params = new URLSearchParams({ limit: "200", range: $("log-range").value });
  for (const [id, key] of [["log-vendor", "vendor"], ["log-status", "status"], ["log-q", "q"]]) {
    const value = $(id).value.trim();
    if (value) params.set(key, value);
  }
  let data;
  try {
    data = await getJSON(`/api/calls?${params.toString()}`);
  } catch (err) {
    banner(err.message, "bad");
    return;
  }
  const body = el("tbody");
  if ((data.calls || []).length === 0) {
    body.append(el("tr", {}, [el("td", { colspan: "8", class: "hint", text: "没有匹配的调用记录。" })]));
  }
  for (const row of data.calls) {
    body.append(
      el("tr", {}, [
        el("td", { class: "mono", text: row.ts }),
        el("td", { text: row.vendor }),
        el("td", { text: dash(row.model) }),
        el("td", { class: "mono", text: dash(row.voice_id) }),
        el("td", { text: row.chars }),
        el("td", { text: `${row.latency_ms} ms` }),
        el("td", { text: Number(row.cost_estimate).toFixed(4) }),
        el("td", {}, [
          el("span", {
            class: `tag ${row.status === "ok" ? "ok" : "bad"}`,
            text: row.status === "ok" ? "成功" : `失败 ${dash(row.error_code)}`,
          }),
        ]),
      ]),
    );
  }
  const table = $("logs-table");
  const old = table.querySelector("tbody");
  if (old) old.replaceWith(body);
  else table.append(body);
}

// ── 页签与启动 ────────────────────────────────────────────────────────

function showPanel(name) {
  for (const tab of document.querySelectorAll("#tabs .tab")) {
    tab.classList.toggle("active", tab.dataset.panel === name);
  }
  for (const panel of document.querySelectorAll("main .panel")) {
    panel.hidden = panel.id !== `panel-${name}`;
  }
  if (name === "cost") runCost();
  if (name === "logs") runLogs();
  if (name === "vendors") refreshExpiring();
}

async function refreshHealth() {
  try {
    const data = await getJSON("/api/health");
    const lan = data.exposes_lan ? " · ⚠ 正在对外暴露（不只是本机）" : "";
    $("health").textContent = `v${data.version} · ${data.host}:${data.port} · 默认厂商 ${data.default_vendor}${lan}`;
  } catch (err) {
    $("health").textContent = `无法连接：${err.message}`;
  }
}

async function boot() {
  for (const tab of document.querySelectorAll("#tabs .tab")) {
    tab.addEventListener("click", () => showPanel(tab.dataset.panel));
  }
  $("tuner-run").addEventListener("click", runTuner);
  $("tuner-voice").addEventListener("input", renderTunerCurrent);
  $("clone-run").addEventListener("click", runClone);
  $("cost-run").addEventListener("click", runCost);
  $("log-run").addEventListener("click", runLogs);
  showPanel("tuner");
  await refreshHealth();
  try {
    await refreshVendors();
    await refreshVoices();
    renderTunerCurrent();
  } catch (err) {
    banner(`初始化失败：${err.message}`, "bad");
  }
}

boot();
