'use strict';
const path = require('path');
const express = require('express');
const store = require('./lib/store');
const db = require('./lib/db');
const calc = require('./lib/calc');
const mock = require('./lib/mockdata');

const app = express();
const PORT = Number(process.env.PORT || 3000);

app.use(express.json({ limit: '50mb' }));
app.use(express.static(path.join(__dirname, 'public')));

/** 当前状态（配置 + 维护数据 + 演示信息） */
app.get('/api/state', (req, res) => {
  res.json({
    config: store.getConfig(),
    inputs: store.getInputs(),
    mock: { month: mock.MONTH, rawCount: mock.raw.length },
  });
});

/** 保存数据库配置 */
app.post('/api/config', (req, res) => {
  store.saveConfig(req.body || {});
  res.json({ ok: true });
});

/** 测试数据库连接 */
app.post('/api/test-db', async (req, res) => {
  const cfg = { ...store.getConfig(), ...(req.body || {}) };
  const r = await db.testConnection(cfg);
  res.json(r);
});

/** 保存维护数据 */
app.post('/api/inputs', (req, res) => {
  const body = req.body || {};
  store.saveInputs({
    pkgGroup: body.pkgGroup || [],
    filters: body.filters || [],
    plan: body.plan || [],
    prices: body.prices || [],
    exceptions: body.exceptions || [],
  });
  res.json({ ok: true });
});

/** 拉取原始数据预览（db 模式） */
app.get('/api/fetch-raw', async (req, res) => {
  const month = String(req.query.month || mock.MONTH);
  const cfg = store.getConfig();
  const start = month.slice(0, 7) + '-01';
  const end = monthEnd(month);
  const r = await db.fetchRaw(cfg, start, end);
  res.json({ ok: r.ok, message: r.message, count: r.rows.length });
});

/**
 * 报表计算
 * GET /api/report?month=2026-08&source=demo|db
 */
app.get('/api/report', async (req, res) => {
  const month = String(req.query.month || mock.MONTH);
  const source = String(req.query.source || 'demo');
  let raw, notice = '';

  if (source === 'db') {
    const cfg = store.getConfig();
    const start = month.slice(0, 7) + '-01';
    const end = monthEnd(month);
    const r = await db.fetchRaw(cfg, start, end);
    if (!r.ok) return res.status(502).json({ ok: false, message: r.message });
    raw = r.rows;
    notice = `数据库拉取 ${r.rows.length} 行（${start} ~ ${end}）`;
  } else {
    raw = mock.raw;
    notice = '演示数据（未连数据库）';
  }

  const inputs = store.getInputs();
  const result = calc.compute({ month, inputs, raw });
  result.notice = notice;
  result.source = source;
  res.json({ ok: true, ...result });
});

function monthEnd(month) {
  const [y, m] = month.split('-').map(Number);
  const dim = new Date(Date.UTC(y, m, 0)).getUTCDate();
  return `${y}-${String(m).padStart(2, '0')}-${String(dim).padStart(2, '0')}`;
}

app.listen(PORT, () => {
  console.log(`营收报表服务已启动: http://localhost:${PORT}`);
  console.log('演示数据模式: 报表页数据源选「演示数据」即可无数据库预览');
});
