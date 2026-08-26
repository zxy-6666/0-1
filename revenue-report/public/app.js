'use strict';
/* 营收报表前端逻辑：报表渲染 + 输入维护 + 数据库配置 */

const state = { config: null, inputs: null };

const $ = sel => document.querySelector(sel);

/* ---------------- 通用工具 ---------------- */
const fmtNum = v => (v === null || v === undefined || isNaN(v)) ? '–' : Number(v).toLocaleString('zh-CN', { maximumFractionDigits: 2 });
const fmtInt = v => (v === null || v === undefined || isNaN(v)) ? '–' : Number(v).toLocaleString('zh-CN');
const fmtPct = v => (v === null || v === undefined || isNaN(v)) ? '–' : (v * 100).toFixed(1) + '%';
const dayShort = d => { const [, m, dd] = d.split('-'); return `${+m}/${+dd}`; };
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

async function api(url, opts) {
  const res = await fetch(url, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.message || res.statusText);
  return data;
}

/* ---------------- Tab 切换 ---------------- */
document.querySelectorAll('.tab').forEach(btn => btn.addEventListener('click', () => {
  document.querySelectorAll('.tab').forEach(b => b.classList.toggle('active', b === btn));
  document.querySelectorAll('.tabpanel').forEach(p => p.classList.toggle('active', p.id === 'tab-' + btn.dataset.tab));
}));
document.querySelectorAll('.rtab').forEach(btn => btn.addEventListener('click', () => {
  document.querySelectorAll('.rtab').forEach(b => b.classList.toggle('active', b === btn));
  renderCurrentReport(btn.dataset.rtab);
}));

/* ---------------- 报表 ---------------- */
let curReport = null;

async function refreshReport() {
  const month = $('#month').value;
  const source = $('#source').value;
  const btn = $('#btnRefresh');
  btn.disabled = true; btn.textContent = '计算中…';
  $('#reportNotice').textContent = '';
  try {
    curReport = await api(`/api/report?month=${month}&source=${source}`);
    $('#reportNotice').textContent = curReport.notice + `　｜　入料匹配 ${curReport.meta.matched} 行 / 忽略 ${curReport.meta.ignored} 行`;
    renderCurrentReport(document.querySelector('.rtab.active').dataset.rtab);
  } catch (e) {
    $('#reportNotice').innerHTML = `<span style="color:#b3261e">计算失败：${esc(e.message)}</span>`;
  } finally {
    btn.disabled = false; btn.textContent = '刷新报表';
  }
}

function renderCurrentReport(which) {
  if (!curReport) return;
  const box = $('#reportContainer');
  if (which === 'summary') box.innerHTML = buildSummaryTable(curReport.summary);
  else if (which === 'recv') box.innerHTML = buildDetailTable(curReport.recv, '入料', 'receiving%');
  else box.innerHTML = buildDetailTable(curReport.ship, '出货', 'biling%');
}

function pctCell(v, cls) { return `<td class="num ${cls || ''}">${fmtPct(v)}</td>`; }
function numCell(v, cls) { return `<td class="num ${cls || ''}">${fmtNum(v)}</td>`; }

function buildSummaryTable(s) {
  const days = s.days, g = s.groups, t = s.total;
  let h = `<table class="grid-table"><thead><tr><th class="rowhead">PKG Group</th><th>item</th><th class="month">月合计</th>`;
  days.forEach(d => { h += `<th>${dayShort(d)}</th>`; });
  h += `</tr></thead><tbody>`;
  for (const grp of g) {
    h += `<tr><td class="rowhead" rowspan="2">${esc(grp.group)}</td><td>R%</td>${pctCell(grp.rM, 'month')}`;
    grp.r.forEach(v => { h += pctCell(v); }); h += `</tr>`;
    h += `<tr><td>B%</td>${pctCell(grp.bM, 'month')}`;
    grp.b.forEach(v => { h += pctCell(v); }); h += `</tr>`;
  }
  h += `<tr class="total"><td class="rowhead" rowspan="2">总计</td><td>R%</td>${pctCell(t.rM, 'month')}`;
  t.r.forEach(v => { h += pctCell(v); }); h += `</tr>`;
  h += `<tr class="total"><td>B%</td>${pctCell(t.bM, 'month')}`;
  t.b.forEach(v => { h += pctCell(v); }); h += `</tr>`;
  return h + `</tbody></table>`;
}

function buildDetailTable(r, label, pctName) {
  const days = r.days, groups = r.groups;
  const qtyName = label === '出货' ? '出货量' : '入料量';
  const amtName = label === '出货' ? '出货金额' : '入料金额';
  const cumName = label === '出货' ? '出货累计金额' : '入料累计金额';
  const zones = [
    ['FCST PLAN', r.planA, false, fmtNum],
    [qtyName, r.qtyA, false, fmtInt],
    [amtName, r.amtA, false, fmtNum],
    [cumName, r.cumA, false, fmtNum],
    [pctName, r.pctA, true, fmtPct],
  ];
  let html = '';
  for (const [name, arr, hasMonth, fmt] of zones) {
    html += `<div style="margin:8px 10px 4px"><b>${name}</b></div><table class="grid-table"><thead><tr><th class="rowhead">PKG Group\\日期</th>`;
    if (hasMonth) html += `<th class="month">月合计</th>`;
    days.forEach(d => { html += `<th>${dayShort(d)}</th>`; });
    html += `</tr></thead><tbody>`;
    groups.forEach((grp, gi) => {
      html += `<tr><td class="rowhead">${esc(grp)}</td>`;
      if (hasMonth) html += `<td class="month num">${fmt(r.pctM[gi])}</td>`;
      arr[gi].forEach(v => { html += `<td class="num">${fmt(v)}</td>`; });
      html += `</tr>`;
    });
    html += `</tbody></table>`;
  }
  return html;
}

/* ---------------- 输入维护：通用行编辑器 ---------------- */
function rowEditor(el, columns, rows) {
  el.innerHTML = '';
  const table = document.createElement('table');
  table.className = 'edit-table';
  const thead = document.createElement('thead');
  let hr = '<tr><th>#</th>';
  columns.forEach(c => { hr += `<th>${esc(c.label)}</th>`; });
  hr += '<th></th></tr>';
  thead.innerHTML = hr;
  const tbody = document.createElement('tbody');
  table.appendChild(thead); table.appendChild(tbody);
  el.appendChild(table);

  const renderRows = () => {
    tbody.innerHTML = '';
    rows.forEach((row, i) => {
      const tr = document.createElement('tr');
      let cells = `<td>${i + 1}</td>`;
      columns.forEach(c => {
        const v = row[c.key] ?? '';
        const cls = c.type === 'num' ? '' : 'txt';
        cells += `<td><input class="${cls}" data-i="${i}" data-k="${c.key}" value="${esc(v)}" ${c.placeholder ? `placeholder="${esc(c.placeholder)}"` : ''}></td>`;
      });
      cells += `<td class="del"><button class="mini" data-del="${i}">删除</button></td>`;
      tr.innerHTML = cells;
      tbody.appendChild(tr);
    });
  };
  tbody.addEventListener('input', e => {
    const inp = e.target.closest('input'); if (!inp) return;
    rows[+inp.dataset.i][inp.dataset.k] = inp.value;
  });
  tbody.addEventListener('click', e => {
    const btn = e.target.closest('button[data-del]'); if (!btn) return;
    rows.splice(+btn.dataset.del, 1); renderRows();
  });
  const addBtn = document.createElement('button');
  addBtn.className = 'mini'; addBtn.textContent = '＋ 添加行';
  addBtn.style.marginTop = '6px';
  addBtn.addEventListener('click', () => { rows.push({}); renderRows(); });
  el.appendChild(addBtn);
  renderRows();
}

/* ---------------- 输入维护：矩阵编辑器 ---------------- */
function matrixEditor(el, rowKeys, colKeys, getCell, setCell, emptyHint) {
  el.innerHTML = '';
  const table = document.createElement('table');
  table.className = 'edit-table';
  let h = '<thead><tr><th>产品</th>' + colKeys.map(d => `<th>${dayShort(d)}</th>`).join('') + '</tr></thead><tbody>';
  const tbody = document.createElement('tbody');
  rowKeys.forEach((rk, i) => {
    let cells = `<td class="rowhead" style="background:#fbfcfb;font-weight:600">${esc(rk)}</td>`;
    colKeys.forEach((d, j) => {
      cells += `<td><input data-i="${i}" data-j="${j}" value="${esc(getCell(i, j) ?? '')}" placeholder="${emptyHint || ''}"></td>`;
    });
    const tr = document.createElement('tr'); tr.innerHTML = cells; tbody.appendChild(tr);
  });
  table.insertAdjacentHTML('afterbegin', h);
  table.appendChild(tbody);
  el.appendChild(table);
  tbody.addEventListener('input', e => {
    const inp = e.target.closest('input'); if (!inp) return;
    setCell(+inp.dataset.i, +inp.dataset.j, inp.value);
  });
}

/* ---------------- 输入维护：加载与保存 ---------------- */
let edPlan = null, edPrice = null;

function buildInputEditors() {
  const inp = state.inputs;
  // 4. PKG 分类
  rowEditor($('#edPkgGroup'),
    [{ key: 'product_name', label: 'product_name', type: 'txt' }, { key: 'pkg_group', label: 'PKG Group', type: 'txt' }],
    inp.pkgGroup);

  // 5. 筛选条件（动态列：取现有所有字段 + 类型）
  const fKeys = [];
  inp.filters.forEach(r => Object.keys(r).forEach(k => { if (!fKeys.includes(k)) fKeys.push(k); }));
  const std = ['product_name', '类型', 'step_name', 'activity', 'lot_type'];
  std.forEach(k => { if (!fKeys.includes(k)) fKeys.push(k); });
  const fCols = fKeys.map(k => ({ key: k, label: k, type: 'txt' }));
  rowEditor($('#edFilters'), fCols, inp.filters);

  // 6. PLAN 矩阵
  edPlan = { rows: [], set: [] };
  const pTypes = ['in', 'out'];
  inp.plan.forEach(r => {
    const key = `${r.product_name}|${r.plan_type}`;
    if (!edPlan.rows.includes(key)) edPlan.rows.push(key);
  });
  const days = getMonthDays($('#month').value);
  edPlan.values = edPlan.rows.map(() => ({}));
  inp.plan.forEach(r => {
    const i = edPlan.rows.indexOf(`${r.product_name}|${r.plan_type}`);
    if (i >= 0) edPlan.values[i][r.date] = r.qty;
  });
  matrixEditor($('#edPlan'), edPlan.rows, days,
    (i, j) => edPlan.values[i][days[j]],
    (i, j, v) => { edPlan.values[i][days[j]] = v; });

  // 8. 单价矩阵
  edPrice = { rows: [], values: [] };
  inp.prices.forEach(r => { if (!edPrice.rows.includes(r.product_name)) edPrice.rows.push(r.product_name); });
  edPrice.values = edPrice.rows.map(() => ({}));
  inp.prices.forEach(r => {
    const i = edPrice.rows.indexOf(r.product_name);
    if (i >= 0) edPrice.values[i][r.date] = r.price;
  });
  matrixEditor($('#edPrice'), edPrice.rows, days,
    (i, j) => edPrice.values[i][days[j]],
    (i, j, v) => { edPrice.values[i][days[j]] = v; });

  // 10. 异常
  rowEditor($('#edExceptions'), [
    { key: 'product_name', label: 'product_name', type: 'txt' },
    { key: 'qty', label: 'qty', type: 'num' },
    { key: '异常类型', label: '异常类型', type: 'txt', placeholder: 'delay/scrapped' },
    { key: '影响范围', label: '影响范围', type: 'txt', placeholder: 'plan in/plan out' },
    { key: 'old_SOD', label: 'old_SOD', type: 'txt', placeholder: '2026-08-05' },
    { key: 'new_SOD', label: 'new_SOD', type: 'txt', placeholder: 'delay 必填' },
  ], inp.exceptions);
}

function collectRowEditor(el) {
  const inputs = [...el.querySelectorAll('input')];
  const rows = [];
  const map = {};
  inputs.forEach(inp => {
    const i = +inp.dataset.i, k = inp.dataset.k;
    (map[i] = map[i] || {})[k] = inp.value;
  });
  Object.keys(map).forEach(i => rows.push(map[i]));
  return rows.filter(r => Object.values(r).some(v => v !== ''));
}

function collectPlan() {
  const out = [];
  const days = getMonthDays($('#month').value);
  edPlan.rows.forEach((rk, i) => {
    const [p, t] = rk.split('|');
    days.forEach(d => {
      const v = edPlan.values[i][d];
      if (v !== undefined && v !== '') out.push({ product_name: p, plan_type: t, date: d, qty: Number(v) || 0 });
    });
  });
  return out;
}

function collectPrice() {
  const out = [];
  const days = getMonthDays($('#month').value);
  edPrice.rows.forEach((p, i) => {
    days.forEach(d => {
      const v = edPrice.values[i][d];
      if (v !== undefined && v !== '') out.push({ product_name: p, date: d, price: Number(v) });
    });
  });
  return out;
}

async function saveInputs() {
  const payload = {
    pkgGroup: collectRowEditor($('#edPkgGroup')),
    filters: collectRowEditor($('#edFilters')),
    plan: collectPlan(),
    prices: collectPrice(),
    exceptions: collectRowEditor($('#edExceptions')),
  };
  try {
    await api('/api/inputs', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
    state.inputs = payload;
    alert('已保存。点击「刷新报表」生效。');
  } catch (e) {
    alert('保存失败：' + e.message);
  }
}

function getMonthDays(month) {
  const [y, m] = month.split('-').map(Number);
  const dim = new Date(Date.UTC(y, m, 0)).getUTCDate();
  const out = [];
  for (let d = 1; d <= dim; d++) out.push(`${y}-${String(m).padStart(2, '0')}-${String(d).padStart(2, '0')}`);
  return out;
}

/* ---------------- 数据库页 ---------------- */
function fillDbForm() {
  const f = $('#dbForm');
  ['host', 'port', 'database', 'user', 'password', 'schema', 'table'].forEach(k => {
    f.elements[k].value = state.config[k] ?? '';
  });
  const tbody = $('#fieldMapTable tbody');
  tbody.innerHTML = '';
  const head = ['lot_name', 'lot_type', 'component_qty', 'product_name', 'step_name', 'last_updated_time', 'activity'];
  head.forEach(h => {
    const tr = document.createElement('tr');
    tr.innerHTML = `<td>${esc(h)}</td><td><input data-fm="${esc(h)}" value="${esc(state.config.fieldMap[h] ?? '')}"></td>`;
    tbody.appendChild(tr);
  });
}

function collectFieldMap() {
  const map = {};
  document.querySelectorAll('#fieldMapTable input[data-fm]').forEach(inp => { map[inp.dataset.fm] = inp.value.trim(); });
  return map;
}

async function testDb() {
  const msg = $('#dbMsg'); msg.textContent = '测试中…'; msg.className = 'msg';
  try {
    const cfg = readDbForm();
    const r = await api('/api/test-db', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(cfg) });
    msg.textContent = r.message; msg.className = 'msg ' + (r.ok ? 'ok' : 'err');
  } catch (e) { msg.textContent = e.message; msg.className = 'msg err'; }
}

function readDbForm() {
  const f = $('#dbForm'), cfg = {};
  ['host', 'port', 'database', 'user', 'password', 'schema', 'table'].forEach(k => { cfg[k] = f.elements[k].value; });
  cfg.fieldMap = collectFieldMap();
  return cfg;
}

async function saveDb() {
  const cfg = readDbForm();
  await api('/api/config', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(cfg) });
  state.config = cfg;
  const msg = $('#dbMsg'); msg.textContent = '已保存'; msg.className = 'msg ok';
}

async function previewRaw() {
  const box = $('#rawPreview');
  box.innerHTML = '拉取中…';
  try {
    const month = $('#month').value;
    const r = await api(`/api/fetch-raw?month=${month}`);
    box.innerHTML = `<span class="${r.ok ? '' : ''}" style="${r.ok ? 'color:var(--accent)' : 'color:#b3261e'}">${esc(r.message)}</span>`;
  } catch (e) {
    box.innerHTML = `<span style="color:#b3261e">${esc(e.message)}</span>`;
  }
}

/* ---------------- 初始化 ---------------- */
async function init() {
  try {
    const st = await api('/api/state');
    state.config = st.config;
    state.inputs = st.inputs;
    $('#month').value = st.mock.month;
    fillDbForm();
    buildInputEditors();
    await refreshReport();
  } catch (e) {
    $('#reportNotice').innerHTML = `<span style="color:#b3261e">初始化失败：${esc(e.message)}</span>`;
  }
}

$('#btnRefresh').addEventListener('click', refreshReport);
$('#btnSaveInputs').addEventListener('click', saveInputs);
$('#btnTestDb').addEventListener('click', testDb);
$('#btnSaveDb').addEventListener('click', saveDb);
$('#btnPreviewRaw').addEventListener('click', previewRaw);
$('#btnSaveFieldMap').addEventListener('click', saveDb);

init();
