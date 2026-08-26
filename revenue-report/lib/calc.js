'use strict';
/**
 * 报表计算核心（与 Excel 宏口径完全一致）
 * 输入：
 *   month: '2026-08'
 *   inputs: { pkgGroup, filters, plan, prices, exceptions }
 *   raw:    [ {lot_name, lot_type, component_qty, product_name, step_name, last_updated_time, activity} ]
 * 输出：{ summary, recv, ship, meta }
 */

function pad(n) { return String(n).padStart(2, '0'); }
function dayKey(y, m, d) { return `${y}-${pad(m)}-${pad(d)}`; }

function monthRange(month) {
  const [y, m] = month.split('-').map(Number);
  const days = [];
  const dim = new Date(Date.UTC(y, m, 0)).getUTCDate();
  for (let d = 1; d <= dim; d++) days.push(dayKey(y, m, d));
  return { y, m, days };
}

function toDayKey(dt) {
  if (dt instanceof Date) return dayKey(dt.getUTCFullYear(), dt.getUTCMonth() + 1, dt.getUTCDate());
  const s = String(dt);
  if (/^\d{4}-\d{2}-\d{2}/.test(s)) return s.slice(0, 10);
  const d = new Date(s);
  if (isNaN(d.getTime())) return null;
  return dayKey(d.getFullYear(), d.getMonth() + 1, d.getDate());
}

function num(v) { return (typeof v === 'number' && isFinite(v)) ? v : 0; }

function buildGroups(pkgGroup) {
  const groups = [];
  const productToGroup = {};
  const seen = new Set();
  (pkgGroup || []).forEach(r => {
    const p = String(r.product_name || '').trim();
    const g = String(r.pkg_group || '').trim();
    if (!p || !g) return;
    productToGroup[p] = g;
    if (!seen.has(g)) { seen.add(g); groups.push(g); }
  });
  const products = Object.keys(productToGroup);
  return { groups, products, productToGroup };
}

/** 单价向前补齐：产品×日 -> price */
function buildPrice(prices, products, days) {
  const map = {}; // 'p|d' -> price
  const byProd = {};
  (prices || []).forEach(r => {
    const p = String(r.product_name || '').trim();
    const d = toDayKey(r.date);
    if (!p || !d) return;
    (byProd[p] = byProd[p] || []).push({ d, v: num(r.price) });
  });
  for (const p of products) {
    const arr = (byProd[p] || []).slice().sort((a, b) => a.d.localeCompare(b.d));
    let last = null;
    let ai = 0;
    for (const d of days) {
      while (ai < arr.length && arr[ai].d <= d) { last = arr[ai].v; ai++; }
      if (last !== null) map[p + '|' + d] = last;
    }
  }
  return map;
}

/** PLAN 基础值 + 异常调整 -> plan in/out map */
function buildPlan(planRows, exceptions, days) {
  const planIn = {};   // 'p|d' -> qty
  const planOut = {};
  (planRows || []).forEach(r => {
    const p = String(r.product_name || '').trim();
    const d = toDayKey(r.date);
    const t = String(r.plan_type || '').toLowerCase();
    if (!p || !d) return;
    const q = num(r.qty);
    if (t === 'in') planIn[p + '|' + d] = q;
    else if (t === 'out') planOut[p + '|' + d] = q;
  });
  // 异常
  (exceptions || []).forEach(r => {
    const p = String(r.product_name || '').trim();
    const q = num(r.qty);
    const et = String(r['异常类型'] || '').toLowerCase().trim();
    const scope = String(r['影响范围'] || '').toLowerCase();
    const oldD = toDayKey(r.old_SOD);
    if (!p || !oldD || !q) return;
    const newD = toDayKey(r.new_SOD) || oldD;
    const isOut = scope.includes('out') && !scope.includes('in');
    const tgt = isOut ? planOut : planIn;
    const key = p + '|' + oldD;
    tgt[key] = (tgt[key] || 0) - q;
    if (et === 'delay') {
      const k2 = p + '|' + newD;
      tgt[k2] = (tgt[k2] || 0) + q;
    }
  });
  return { planIn, planOut };
}

/** 原始数据按筛选条件匹配 -> 入料/出货 qty（product|day） */
function buildQty(raw, filters, days) {
  const recvQ = {}, shipQ = {};
  const inRows = [], outRows = [];
  (filters || []).forEach(r => {
    const t = String(r['类型'] || '').trim();
    if (!t) return;
    const pairs = [];
    for (const k of Object.keys(r)) {
      if (k === '类型') continue;
      const v = String(r[k] ?? '').trim();
      if (v) pairs.push([k.toLowerCase(), v]);
    }
    if (t === '入料') inRows.push(pairs);
    else if (t === '出货') outRows.push(pairs);
  });

  const match = (rec, rows) => {
    for (const pairs of rows) {
      let ok = true;
      for (const [k, v] of pairs) {
        const rv = String(rec[k] ?? '').trim();
        if (rv.toLowerCase() !== v.toLowerCase()) { ok = false; break; }
      }
      if (ok) return true;
    }
    return false;
  };

  let matched = 0, ignored = 0;
  const inSet = new Set(days), outSet = new Set(days);
  (raw || []).forEach(r => {
    const act = String(r.activity || '').trim();
    const lt = String(r.lot_type || '').trim();
    if ((act !== 'Pre' && act !== 'Post') || lt !== 'P') { ignored++; return; }
    const d = toDayKey(r.last_updated_time);
    if (!d || !inSet.has(d)) { ignored++; return; }
    const p = String(r.product_name || '').trim();
    if (!p) { ignored++; return; }
    const q = num(r.component_qty);
    const rec = {};
    for (const k of Object.keys(r)) rec[k.toLowerCase()] = r[k];
    let t = '';
    if (match(rec, inRows)) t = 'in';
    else if (match(rec, outRows)) t = 'out';
    if (!t) { ignored++; return; }
    matched++;
    const k = p + '|' + d;
    if (t === 'in') recvQ[k] = (recvQ[k] || 0) + q;
    else shipQ[k] = (shipQ[k] || 0) + q;
  });
  return { recvQ, shipQ, matched, ignored };
}

function aggregate(groups, products, productToGroup, days, plan, price, qty) {
  const nG = groups.length, nD = days.length;
  const planA = Array.from({ length: nG }, () => new Array(nD).fill(0));
  const amtA = Array.from({ length: nG }, () => new Array(nD).fill(0));
  const qtyA = Array.from({ length: nG }, () => new Array(nD).fill(0));
  const cumA = Array.from({ length: nG }, () => new Array(nD).fill(0));
  const pctA = Array.from({ length: nG }, () => new Array(nD).fill(null));
  const planM = new Array(nG).fill(0), amtM = new Array(nG).fill(0);

  for (let g = 0; g < nG; g++) {
    for (let d = 0; d < nD; d++) {
      let pa = 0, aa = 0, qa = 0;
      for (const p of products) {
        if (productToGroup[p] !== groups[g]) continue;
        const k = p + '|' + days[d];
        const pr = price[k] || 0;
        pa += (plan[k] || 0) * pr;
        aa += (qty[k] || 0) * pr;
        qa += qty[k] || 0;
      }
      planA[g][d] = pa; amtA[g][d] = aa; qtyA[g][d] = qa;
      cumA[g][d] = d === 0 ? aa : cumA[g][d - 1] + aa;
      pctA[g][d] = pa !== 0 ? aa / pa : null;
      planM[g] += pa; amtM[g] += aa;
    }
  }
  const pctM = groups.map((_, g) => planM[g] !== 0 ? amtM[g] / planM[g] : null);
  return { planA, amtA, qtyA, cumA, pctA, planM, amtM, pctM };
}

function compute(ctx) {
  const { month, inputs, raw } = ctx;
  const { days } = monthRange(month);
  const { groups, products, productToGroup } = buildGroups(inputs.pkgGroup);
  const price = buildPrice(inputs.prices, products, days);
  const { planIn, planOut } = buildPlan(inputs.plan, inputs.exceptions, days);
  const { recvQ, shipQ, matched, ignored } = buildQty(raw, inputs.filters, days);

  const recv = aggregate(groups, products, productToGroup, days, planIn, price, recvQ);
  const ship = aggregate(groups, products, productToGroup, days, planOut, price, shipQ);

  // 汇总：R%/B%（每日 + 月合计前置），总计加权
  const summaryGroups = groups.map((g, i) => ({
    group: g,
    r: recv.pctA[i],
    b: ship.pctA[i],
    rM: recv.pctM[i],
    bM: ship.pctM[i],
  }));
  const totPlanIn = days.map((_, d) => groups.reduce((s, _, i) => s + recv.planA[i][d], 0));
  const totAmtIn = days.map((_, d) => groups.reduce((s, _, i) => s + recv.amtA[i][d], 0));
  const totPlanOut = days.map((_, d) => groups.reduce((s, _, i) => s + ship.planA[i][d], 0));
  const totAmtOut = days.map((_, d) => groups.reduce((s, _, i) => s + ship.amtA[i][d], 0));
  const tPlanInM = groups.reduce((s, _, i) => s + recv.planM[i], 0);
  const tAmtInM = groups.reduce((s, _, i) => s + recv.amtM[i], 0);
  const tPlanOutM = groups.reduce((s, _, i) => s + ship.planM[i], 0);
  const tAmtOutM = groups.reduce((s, _, i) => s + ship.amtM[i], 0);

  return {
    summary: {
      month,
      days,
      groups: summaryGroups,
      total: {
        r: days.map((_, d) => totPlanIn[d] !== 0 ? totAmtIn[d] / totPlanIn[d] : null),
        b: days.map((_, d) => totPlanOut[d] !== 0 ? totAmtOut[d] / totPlanOut[d] : null),
        rM: tPlanInM !== 0 ? tAmtInM / tPlanInM : null,
        bM: tPlanOutM !== 0 ? tAmtOutM / tPlanOutM : null,
      },
    },
    recv: { groups, days, ...recv },
    ship: { groups, days, ...ship },
    meta: { matched, ignored, groupCount: groups.length, productCount: products.length },
  };
}

module.exports = { compute, monthRange, toDayKey, buildGroups, buildPrice, buildPlan, buildQty };
