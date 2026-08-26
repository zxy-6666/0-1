'use strict';
/**
 * PostgreSQL 连接与查询（pg 库，参数化查询）
 */
const { Client } = require('pg');

// 9号表固定表头顺序
const HEAD = ['lot_name', 'lot_type', 'component_qty', 'product_name', 'step_name', 'last_updated_time', 'activity'];

function buildClient(cfg) {
  return new Client({
    host: cfg.host,
    port: Number(cfg.port || 5432),
    database: cfg.database,
    user: cfg.user,
    password: cfg.password,
    connectionTimeoutMillis: 10000,
  });
}

function quoteIdent(s) { return '"' + String(s).replace(/"/g, '""') + '"'; }

/** 测试连接，返回 { ok, message } */
async function testConnection(cfg) {
  const client = buildClient(cfg);
  try {
    await client.connect();
    await client.query('SELECT 1');
    return { ok: true, message: '连接成功' };
  } catch (e) {
    return { ok: false, message: String(e.message || e) };
  } finally {
    await client.end().catch(() => {});
  }
}

/**
 * 拉取原始数据（按字段映射、日期范围）
 * 返回 { ok, rows, message }
 */
async function fetchRaw(cfg, start, end) {
  const map = cfg.fieldMap || {};
  const cols = [];
  for (const h of HEAD) {
    const dbCol = String(map[h] || '').trim();
    if (dbCol) cols.push(`${quoteIdent(dbCol)} AS ${quoteIdent(h)}`);
  }
  if (cols.length === 0) return { ok: false, rows: [], message: '字段映射为空' };

  const timeCol = quoteIdent(String(map.last_updated_time || 'last_updated_time').trim());
  const sql = `SELECT ${cols.join(', ')} FROM ${quoteIdent(cfg.schema)}.${quoteIdent(cfg.table)} ` +
    `WHERE ${timeCol} >= $1 AND ${timeCol} <= $2`;

  const client = buildClient(cfg);
  try {
    await client.connect();
    const s = `${start} 00:00:00`, e = `${end} 23:59:59`;
    const res = await client.query({ text: sql, values: [s, e] });
    return { ok: true, rows: res.rows, message: `拉取 ${res.rows.length} 行` };
  } catch (err) {
    return { ok: false, rows: [], message: String(err.message || err) };
  } finally {
    await client.end().catch(() => {});
  }
}

module.exports = { testConnection, fetchRaw, HEAD };
