"use strict";

// ---------------------------------------------------------------------------
// tiny helpers
// ---------------------------------------------------------------------------

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

async function api(method, path, payload) {
  const opts = { method, headers: {} };
  if (payload !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(payload);
  }
  const resp = await fetch(path, opts);
  const body = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    const err = new Error(body.error || `HTTP ${resp.status}`);
    err.status = resp.status;
    err.body = body;
    throw err;
  }
  return body;
}

function uuid() {
  return "b-" + crypto.randomUUID();
}

function toast(message, kind = "info") {
  const el = $("#toast");
  el.textContent = message;
  el.className = `toast ${kind}`;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.add("hidden"), 5200);
}

function esc(s) {
  return String(s)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function renderCell(cell) {
  if (cell.t === "null") return '<span class="cell-null">NULL</span>';
  if (cell.t === "bool") return cell.v ? "true" : "false";
  if (cell.t === "float") return `${cell.v} <span class="small">(float)</span>`;
  if (cell.t === "int") return `${cell.v} <span class="small">(int)</span>`;
  return esc(cell.v);
}

function cellInputs(cols) {
  // One typed input line per compared column.
  return cols
    .map(
      (col) => `
      <div class="field-line">
        <span class="mono small">${esc(col)}</span>
        <select data-role="tag">
          <option value="int">int</option>
          <option value="float">float</option>
          <option value="text">text</option>
          <option value="bool">bool</option>
          <option value="null">null</option>
        </select>
        <input type="text" data-role="value" placeholder="值" />
      </div>`
    )
    .join("");
}

function readTypedCell(lineEl) {
  const tag = lineEl.querySelector('[data-role="tag"]').value;
  const raw = lineEl.querySelector('[data-role="value"]').value.trim();
  if (tag === "null") return { t: "null", v: null };
  if (tag === "int") return { t: "int", v: Number(raw) };
  if (tag === "float") return { t: "float", v: Number(raw) };
  if (tag === "bool") return { t: "bool", v: raw.toLowerCase() === "true" };
  return { t: "text", v: raw };
}

// ---------------------------------------------------------------------------
// state
// ---------------------------------------------------------------------------

const state = {
  config: null,
  knownColumns: { left: [], right: [] },
  activeTab: "union",
  activeSrc: "left",
  readVersion: null,
  ops: [],
};

// ---------------------------------------------------------------------------
// top-level refresh
// ---------------------------------------------------------------------------

async function refreshState() {
  const s = await api("GET", "/api/state");
  $("#current-version").textContent = s.version;
  $("#left-count").textContent = s.left_count;
  $("#right-count").textContent = s.right_count;
  state.config = s.config;
  state.knownColumns = s.known_columns;
  renderColumnPickers();
  renderConfigStatus();
  await refreshVersionSelector(s.version);
  await refreshResults();
  await refreshLedger();
}

function renderConfigStatus() {
  const el = $("#config-status");
  if (!state.config) {
    el.textContent = "尚未配置比较列";
    return;
  }
  el.textContent = `当前：左 [${state.config.left_columns.join(
    ", "
  )}] 对 右 [${state.config.right_columns.join(", ")}]（v${state.config.version}）`;
}

function renderColumnPickers() {
  for (const side of ["left", "right"]) {
    const host = $(`#${side}-cols`);
    const cols = state.knownColumns[side] || [];
    if (!cols.length) {
      host.innerHTML = '<p class="hint">导入文件后出现可选列</p>';
      continue;
    }
    const selected = state.config
      ? state.config[`${side}_columns`]
      : cols.slice(0, Math.min(2, cols.length));
    host.innerHTML = cols
      .map(
        (c) => `
        <label>
          <input type="checkbox" data-col="${esc(c)}"
            ${selected.includes(c) ? "checked" : ""} />
          <span>${esc(c)}</span>
        </label>`
      )
      .join("");
  }
}

async function refreshVersionSelector(currentVersion) {
  const { versions } = await api("GET", "/api/versions");
  const sel = $("#version-select");
  const previous = state.readVersion;
  sel.innerHTML = versions
    .map(
      (v) =>
        `<option value="${v.version}" ${
          v.version === currentVersion ? "selected" : ""
        }>v${v.version} · ${esc(v.reason)}</option>`
    )
    .join("");
  state.readVersion =
    previous !== null && versions.some((v) => v.version === previous)
      ? previous
      : currentVersion;
  sel.value = String(state.readVersion);
  $("#read-version").textContent = `读取版本 v${state.readVersion}`;
}

// ---------------------------------------------------------------------------
// imports and config
// ---------------------------------------------------------------------------

async function readFile(file) {
  if (!file) throw new Error("请先选择 CSV 文件");
  return await file.text();
}

async function importSide(side) {
  const input = $(`#${side}-file`);
  const csv = await readFile(input.files[0]);
  const body = await api("POST", "/api/import", {
    side,
    csv,
    client_batch_id: uuid(),
    auto_configure: !state.config,
  });
  toast(`${side} 侧导入成功，写入版本 v${body.version}`, "ok");
  await refreshState();
}

async function applyConfig() {
  const pick = (side) =>
    $$(`#${side}-cols input:checked`).map((el) => el.dataset.col);
  const leftColumns = pick("left");
  const rightColumns = pick("right");
  if (!leftColumns.length || !rightColumns.length) {
    toast("两侧都至少要选一列", "err");
    return;
  }
  if (leftColumns.length !== rightColumns.length) {
    toast("两侧比较列数量必须一致", "err");
    return;
  }
  const body = await api("POST", "/api/config", {
    left_columns: leftColumns,
    right_columns: rightColumns,
    client_batch_id: uuid(),
  });
  toast(`比较列已应用，版本 v${body.version}`, "ok");
  await refreshState();
}

// ---------------------------------------------------------------------------
// batch editor
// ---------------------------------------------------------------------------

function ensureBatchId() {
  const el = $("#batch-id");
  if (!el.value.trim()) el.value = uuid();
  return el.value.trim();
}

function addOpRow(kind = "add") {
  state.ops.push({ kind });
  renderOps();
}

function removeOp(index) {
  state.ops.splice(index, 1);
  renderOps();
}

function renderOps() {
  const host = $("#ops-editor");
  if (!state.ops.length) {
    host.innerHTML = '<p class="hint">还没有操作，点「加一条新增」开始。</p>';
    return;
  }
  host.innerHTML = state.ops
    .map((op, idx) => {
      const side = op.side || "left";
      const cols = state.config
        ? state.config[`${side}_columns`]
        : [];
      return `
      <div class="op-row">
        <select data-role="kind">
          <option value="add" ${op.kind === "add" ? "selected" : ""}>新增 add</option>
          <option value="retract" ${op.kind === "retract" ? "selected" : ""}>撤销 retract</option>
          <option value="correct" ${op.kind === "correct" ? "selected" : ""}>更正 correct</option>
        </select>
        <select data-role="side">
          <option value="left" ${side === "left" ? "selected" : ""}>left</option>
          <option value="right" ${side === "right" ? "selected" : ""}>right</option>
        </select>
        <div class="fields">
          ${
            op.kind !== "add"
              ? `<div class="field-line">
                   <span class="mono small">来源行号</span>
                   <input type="text" data-role="row-id" placeholder="row_id"
                     value="${esc(op.rowId ?? "")}" />
                 </div>`
              : ""
          }
          ${
            op.kind !== "retract"
              ? cellInputs(cols)
              : '<span class="hint">撤销只需要来源行号，原元组会自动减一份。</span>'
          }
        </div>
        <button class="danger" data-remove="${idx}">删除</button>
      </div>`;
    })
    .join("");

  host.querySelectorAll(".op-row").forEach((rowEl, idx) => {
    const op = state.ops[idx];
    rowEl.querySelector('[data-role="kind"]').addEventListener("change", (e) => {
      state.ops[idx] = { kind: e.target.value, side: op.side || "left" };
      renderOps();
    });
    rowEl.querySelector('[data-role="side"]').addEventListener("change", (e) => {
      state.ops[idx].side = e.target.value;
      renderOps();
    });
    const idEl = rowEl.querySelector('[data-role="row-id"]');
    if (idEl) {
      idEl.addEventListener("input", (e) => {
        state.ops[idx].rowId = e.target.value;
      });
    }
    rowEl.querySelector("[data-remove]").addEventListener("click", () =>
      removeOp(idx)
    );
  });
}

function collectOps() {
  const rows = $$("#ops-editor .op-row");
  return rows.map((rowEl) => {
    const kind = rowEl.querySelector('[data-role="kind"]').value;
    const side = rowEl.querySelector('[data-role="side"]').value;
    const cols = state.config[`${side}_columns`];
    if (kind === "retract") {
      const rowId = Number(
        rowEl.querySelector('[data-role="row-id"]').value.trim()
      );
      if (!Number.isInteger(rowId)) throw new Error("撤销操作需要整数 row_id");
      return { op: "retract", side, row_id: rowId };
    }
    const lines = rowEl.querySelectorAll(".field-line");
    // retract 以外，field-line 里第一条可能是 row-id（correct），其余是单元格。
    const cellLines = Array.from(lines).filter(
      (l) => !l.querySelector('[data-role="row-id"]')
    );
    const row = {};
    cellLines.forEach((lineEl, i) => {
      row[cols[i]] = readTypedCell(lineEl);
    });
    if (kind === "add") return { op: "add", side, row };
    const rowId = Number(
      rowEl.querySelector('[data-role="row-id"]').value.trim()
    );
    if (!Number.isInteger(rowId)) throw new Error("更正操作需要整数 row_id");
    return { op: "correct", side, row_id: rowId, row };
  });
}

async function submitBatch() {
  if (!state.config) {
    toast("请先配置比较列", "err");
    return;
  }
  if (!state.ops.length) {
    toast("批次为空", "err");
    return;
  }
  let operations;
  try {
    operations = collectOps();
  } catch (e) {
    toast(e.message, "err");
    return;
  }
  const batchId = ensureBatchId();
  try {
    const body = await api("POST", "/api/batch", {
      client_batch_id: batchId,
      operations,
    });
    toast(
      body.replayed
        ? `批次 ${batchId} 已存在，幂等重放（仍为 v${body.version}）`
        : `批次提交成功，结果已切换到 v${body.version}`,
      body.replayed ? "info" : "ok"
    );
    state.ops = [];
    renderOps();
    await refreshState();
  } catch (e) {
    toast(
      e.status === 409
        ? `冲突（409）：${e.message}，整批已回滚，版本未变`
        : `提交失败：${e.message}`,
      "err"
    );
  }
}

// ---------------------------------------------------------------------------
// result tables
// ---------------------------------------------------------------------------

function tupleHeader() {
  const cfg = state.config;
  if (!cfg) return "元组";
  return cfg.left_columns.map((c, i) => `${esc(c)} ↔ ${esc(cfg.right_columns[i])}`).join(" , ");
}

function renderResults(snap) {
  state.readVersion = snap.version;
  $("#read-version").textContent = `读取版本 v${snap.version}（三张表同版本快照）`;
  const rows = snap.results[state.activeTab] || [];
  if (!rows.length) {
    $("#result-table").innerHTML =
      '<p class="hint">该版本下此结果没有任何元组。</p>';
    return;
  }
  const head = tupleHeader();
  $("#result-table").innerHTML = `
    <table>
      <thead>
        <tr>
          <th>${head}</th>
          <th>左出现次数</th>
          <th>右出现次数</th>
          <th>结果重数</th>
          <th>为什么保留这些份数 / 来源追溯</th>
        </tr>
      </thead>
      <tbody>
        ${rows
          .map(
            (r) => `
          <tr>
            <td>${r.cells.map(renderCell).join(" , ")}</td>
            <td><span class="badge l">${r.left_count}</span></td>
            <td><span class="badge r">${r.right_count}</span></td>
            <td><span class="badge mult">${r.multiplicity}</span></td>
            <td>
              <div>${esc(r.provenance.reason)}</div>
              <div class="reason">${renderProvenance(r.provenance)}</div>
            </td>
          </tr>`
          )
          .join("")}
      </tbody>
    </table>`;
}

function renderProvenance(prov) {
  const parts = [];
  if (prov.pairs) {
    parts.push(
      `左右配对行号：${prov.pairs
        .map(([l, r]) => `<span class="prov">L#${l}↔R#${r}</span>`)
        .join(" ")}`
    );
  }
  if (prov.survivor_left_row_ids) {
    parts.push(
      `差集幸存左行：${prov.survivor_left_row_ids
        .map((id) => `<span class="prov">#${id}</span>`)
        .join(" ")}`
    );
  }
  if (prov.consumed_by_right_row_ids) {
    parts.push(
      `被右行抵消：${prov.consumed_by_right_row_ids
        .map((id) => `<span class="prov">#${id}</span>`)
        .join(" ")}`
    );
  }
  if (prov.left_row_ids) {
    parts.push(
      `左来源行：${prov.left_row_ids
        .map((id) => `<span class="prov">#${id}</span>`)
        .join(" ")}`
    );
  }
  if (prov.right_row_ids) {
    parts.push(
      `右来源行：${prov.right_row_ids
        .map((id) => `<span class="prov">#${id}</span>`)
        .join(" ")}`
    );
  }
  return parts.join("<br/>");
}

async function refreshResults() {
  const v = state.readVersion;
  const snap = await api(
    "GET",
    "/api/results" + (v !== null ? `?version=${v}` : "")
  );
  renderResults(snap);
}

// ---------------------------------------------------------------------------
// ledger
// ---------------------------------------------------------------------------

async function refreshLedger() {
  const host = $("#ledger-table");
  if (state.activeSrc === "events") {
    const { events } = await api("GET", "/api/events");
    host.innerHTML = `
      <table><thead><tr>
        <th>版本</th><th>批次</th><th>侧</th><th>操作</th><th>行号</th><th>内容</th>
      </tr></thead><tbody>
      ${events
        .map(
          (e) => `
        <tr>
          <td>v${e.version}</td>
          <td class="mono small">${esc(e.batch_id)}</td>
          <td>${esc(e.side)}</td>
          <td><span class="badge op-${e.op}">${esc(e.op)}</span></td>
          <td class="mono">${e.row_id ?? `${e.old_row_id ?? "–"}→${e.new_row_id ?? "–"}`}</td>
          <td class="small mono">${esc(e.payload)}</td>
        </tr>`
        )
        .join("")}
      </tbody></table>`;
    return;
  }
  if (state.activeSrc === "batches") {
    const { batches } = await api("GET", "/api/batches");
    host.innerHTML = `
      <table><thead><tr>
        <th>版本</th><th>客户端批次号</th><th>状态</th><th>摘要</th><th>提交时间</th>
      </tr></thead><tbody>
      ${batches
        .map(
          (b) => `
        <tr>
          <td>v${b.version}</td>
          <td class="mono small">${esc(b.client_batch_id)}</td>
          <td>${esc(b.status)}</td>
          <td class="small mono">${esc(b.summary)}</td>
          <td class="small">${esc(b.committed_at)}</td>
        </tr>`
        )
        .join("")}
      </tbody></table>`;
    return;
  }
  const data = await api("GET", `/api/source/${state.activeSrc}`);
  if (!data.rows.length) {
    host.innerHTML = '<p class="hint">该侧还没有来源行。</p>';
    return;
  }
  const cols = Object.keys(data.rows[0].row);
  host.innerHTML = `
    <table><thead><tr>
      <th>持续保留的行号</th>${cols.map((c) => `<th>${esc(c)}</th>`).join("")}
    </tr></thead><tbody>
    ${data.rows
      .map(
        (r) => `
      <tr>
        <td class="mono">#${r.row_id}</td>
        ${cols
          .map((c) => `<td>${renderCell(r.row[c])}</td>`)
          .join("")}
      </tr>`
      )
      .join("")}
    </tbody></table>`;
}

// ---------------------------------------------------------------------------
// wiring
// ---------------------------------------------------------------------------

function wire() {
  $$("[data-import]").forEach((btn) =>
    btn.addEventListener("click", () =>
      importSide(btn.dataset.import).catch((e) => toast(e.message, "err"))
    )
  );
  $("#apply-config").addEventListener("click", () =>
    applyConfig().catch((e) => toast(e.message, "err"))
  );
  $("#load-sample").addEventListener("click", async () => {
    try {
      const body = await api("POST", "/api/sample", {});
      toast(
        `手算样本已载入，最新版本 v${body.state.version}`,
        "ok"
      );
      state.readVersion = null;
      await refreshState();
    } catch (e) {
      toast(e.message, "err");
    }
  });
  $("#reset-all").addEventListener("click", async () => {
    if (!confirm("确认清空所有数据并重建空库？")) return;
    await api("DELETE", "/api/state");
    state.ops = [];
    state.readVersion = null;
    toast("已清空并重建", "info");
    await refreshState();
    renderOps();
  });
  $("#gen-batch-id").addEventListener("click", () => {
    $("#batch-id").value = uuid();
  });
  $("#add-op").addEventListener("click", () => addOpRow("add"));
  $("#submit-batch").addEventListener("click", submitBatch);

  $$(".tab").forEach((tab) =>
    tab.addEventListener("click", () => {
      $$(".tab").forEach((t) => t.classList.remove("active"));
      tab.classList.add("active");
      state.activeTab = tab.dataset.tab;
      refreshResults();
    })
  );
  $$(".tab-src").forEach((tab) =>
    tab.addEventListener("click", () => {
      $$(".tab-src").forEach((t) => t.classList.remove("active"));
      tab.classList.add("active");
      state.activeSrc = tab.dataset.src;
      refreshLedger();
    })
  );
  $("#version-select").addEventListener("change", async (e) => {
    state.readVersion = Number(e.target.value);
    await refreshResults();
  });
  $("#refresh-results").addEventListener("click", refreshResults);
}

wire();
renderOps();
refreshState().catch((e) => toast(`初始化失败：${e.message}`, "err"));
