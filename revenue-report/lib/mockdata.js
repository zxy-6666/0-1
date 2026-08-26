'use strict';
/**
 * 内置模拟数据（与 Excel 调试数据集口径一致，便于无数据库时演示/测试）
 */

const MONTH = '2026-08';

function d(day) { return `2026-08-${String(day).padStart(2, '0')}`; }

// 产品: 组, 首step, 中间steps, 末step, 单价(或null), lots=[qty, 入料日, 出货日]
const DATA = [
  ['A005-M1', 'FCBGA', 'IN_RECV', ['WIP_ETCH', 'WIP_PKG'], 'OUT_SHIP', 100, [[10, 3, 12], [15, 7, 18], [8, 15, 28]]],
  ['A006-M1', 'FCBGA', 'IN_RECV', ['WIP_ASM'], 'OUT_SHIP', 80, [[6, 4, 13], [5, 11, 24]]],
  ['A007-M1', 'FCBGA', 'IN_RECV', [], 'OUT_SHIP', 120, [[4, 2, 9], [9, 16, 25]]],
  ['A008-M1', 'FCBGA', 'IN_RECV', ['WIP_SMT'], 'OUT_SHIP', 90, [[7, 6, 15], [5, 22, 30]]],
  ['A009-M1', 'FCBGA', 'IN_RECV', [], 'OUT_SHIP', 45, [[6, 5, 14]]],
  ['A020-M1', 'HBW', 'IN_RECV', ['WIP_PLATE'], 'OUT_SHIP', 200, [[20, 8, 13], [15, 19, 24], [12, 26, 30]]],
  ['A021-M1', 'HBW', 'IN_RECV', [], 'OUT_SHIP', 150, [[8, 8, 16], [11, 21, 27]]],
  ['A022-M1', 'HBW', 'IN_RECV', [], 'OUT_SHIP', 60, [[6, 10, 18], [7, 22, 29]]],
  ['A030-M1', 'FCCSP', 'IN_RECV', ['WIP_DIE'], 'OUT_SHIP', 50, [[5, 12, 20], [3, 24, 28]]],
  ['A031-M1', 'FCCSP', 'IN_RECV', [], 'OUT_SHIP', 30, [[2, 10, 17], [6, 21, 27]]],
  ['A040-M1', 'FOFCBGA', 'IN_RECV', [], 'OUT_SHIP', null, [[9, 9, 19], [6, 24, 29]]],
  ['A050-M1', 'AA', 'IN_RECV', [], 'OUT_SHIP', 25, [[3, 11, 16], [5, 22, 29]]],
];

const PLAN_BASE = {
  'A005-M1': [5, 4], 'A006-M1': [3, 2], 'A007-M1': [2, 2], 'A008-M1': [6, 5],
  'A009-M1': [4, 3], 'A020-M1': [10, 8], 'A021-M1': [8, 6], 'A022-M1': [5, 4],
  'A030-M1': [2, 1], 'A031-M1': [3, 2], 'A040-M1': [4, 3], 'A050-M1': [1, 1],
};

// ---- 输入数据（网页端维护） ----
const pkgGroup = DATA.map(([p, g]) => ({ product_name: p, pkg_group: g }));

const filters = [];
for (const [p] of DATA) {
  filters.push({ product_name: p, 类型: '入料', step_name: 'IN_RECV', activity: 'Pre', lot_type: 'P' });
  filters.push({ product_name: p, 类型: '出货', step_name: 'OUT_SHIP', activity: 'Post', lot_type: 'P' });
}

const plan = [];
const dim = 31;
for (const [p, , , , , , ] of DATA) {
  const [pi, po] = PLAN_BASE[p];
  for (let dd = 1; dd <= dim; dd++) {
    plan.push({ product_name: p, plan_type: 'in', date: d(dd), qty: pi });
    plan.push({ product_name: p, plan_type: 'out', date: d(dd), qty: po });
  }
}

const prices = [];
for (const [p, , , , , pr] of DATA) {
  if (pr === null) continue;
  for (let dd = 1; dd <= dim; dd++) {
    if (p === 'A006-M1' && dd > 1) continue; // 只填8/1，验证向前补齐
    prices.push({ product_name: p, date: d(dd), price: pr });
  }
}

const exceptions = [
  { product_name: 'A005-M1', qty: 5, 异常类型: 'delay', 影响范围: 'plan in', old_SOD: d(5), new_SOD: d(10) },
  { product_name: 'A005-M1', qty: 2, 异常类型: 'scrapped', 影响范围: 'plan out', old_SOD: d(6), new_SOD: null },
  { product_name: 'A020-M1', qty: 8, 异常类型: 'delay', 影响范围: 'plan in', old_SOD: d(15), new_SOD: d(22) },
  { product_name: 'A007-M1', qty: 1, 异常类型: 'scrapped', 影响范围: 'plan out', old_SOD: d(8), new_SOD: null },
  { product_name: 'A030-M1', qty: 2, 异常类型: 'delay', 影响范围: 'plan out', old_SOD: d(14), new_SOD: d(20) },
];

// ---- 原始数据（数据库拉取结果的结构） ----
const raw = [];
let seq = 0;
function T(day, h, m) { return new Date(Date.UTC(2026, 7, day, h, m)); }
function addrow(lt, qty, pn, step, t, act) {
  seq++;
  raw.push({ lot_name: `LOT${String(seq).padStart(4, '0')}`, lot_type: lt, component_qty: qty, product_name: pn, step_name: step, last_updated_time: t, activity: act });
}
for (const [p, , inS, mids, outS] of DATA) {
  const meta = DATA.find(x => x[0] === p);
  for (const [qty, inDay, outDay] of meta[6]) {
    addrow('P', qty, p, inS, T(inDay, 9, 0), 'Pre');
    mids.forEach((mid, k) => {
      addrow('P', qty, p, mid, T(inDay + k + 1, 8, 0), 'Pre');
      addrow('P', qty, p, mid, T(inDay + k + 1, 17, 0), 'Post');
    });
    addrow('P', qty, p, outS, T(outDay, 18, 0), 'Post');
  }
}
// 边界
addrow('R', 9, 'A005-M1', 'IN_RECV', T(5, 9, 0), 'Pre');              // lot_type 不匹配
addrow('P', 'NC', 'A005-M1', 'IN_RECV', T(9, 10, 0), 'Pre');          // qty 非数字
addrow('P', 3, 'A005-M1', 'IN_RECV', T(15, 9, 0), null);              // activity 为空
addrow('P', 4, 'A005-M1', 'WIP_ETCH', T(16, 9, 0), 'Pre');            // 中间step pre
addrow('P', 5, 'A005-M1', 'OUT_SHIP', T(17, 9, 0), 'Pre');            // 末step pre
addrow('P', 6, 'A005-M1', 'IN_RECV', T(2, 9, 0), 'Post');             // 首step post
addrow('P', 7, 'A005-M1', 'IN_RECV', new Date(Date.UTC(2026, 8, 2, 9)), 'Pre'); // 9月

module.exports = {
  MONTH,
  inputs: { pkgGroup, filters, plan, prices, exceptions },
  raw,
  dim,
};
