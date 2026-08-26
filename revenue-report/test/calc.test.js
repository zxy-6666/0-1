'use strict';
/** 计算正确性校验（对照 mockdata 手算期望值） */
const assert = require('assert');
const mock = require('../lib/mockdata');
const calc = require('../lib/calc');

const r = calc.compute({ month: '2026-08', inputs: mock.inputs, raw: mock.raw });
const { recv, ship, summary, meta } = r;

// 组顺序（仅按 4号表 出现顺序）
assert.deepStrictEqual(recv.groups, ['FCBGA', 'HBW', 'FCCSP', 'FOFCBGA', 'AA'], '组顺序');

// 匹配统计：入料25 + 出货25 + qty非数字1 = 51 匹配；忽略 = 87-51 = 36
assert.strictEqual(meta.matched, 51, 'matched');
assert.strictEqual(meta.ignored, 36, 'ignored');

const g = recv.groups.indexOf('FCBGA');
const idx = d => recv.days.indexOf(`2026-08-${String(d).padStart(2, '0')}`);

// FCBGA 8/3：入料量10，金额=10*100=1000，计划=1700
assert.strictEqual(recv.qtyA[g][idx(3)], 10, 'FCBGA 8/3 入料量');
assert.strictEqual(recv.amtA[g][idx(3)], 1000, 'FCBGA 8/3 入料金额');
assert.strictEqual(recv.planA[g][idx(3)], 1700, 'FCBGA 8/3 计划');
assert.ok(Math.abs(recv.pctA[g][idx(3)] - 1000 / 1700) < 1e-9, 'FCBGA 8/3 R%');

// 异常 delay：A005 8/5 plan 5 被搬走 -> FCBGA 8/5 计划 = 1200
assert.strictEqual(recv.planA[g][idx(5)], 1200, 'FCBGA 8/5 计划(异常delay)');
// 入料 8/5：A009 6个(270)
assert.strictEqual(recv.amtA[g][idx(5)], 270, 'FCBGA 8/5 入料金额');

// 单价补齐：A006 8/2 用 80 -> FCBGA 8/2 计划 = 1700
assert.strictEqual(recv.planA[g][idx(2)], 1700, 'FCBGA 8/2 计划(单价补齐)');

// 累计：8/2 A007 4*120=480 + 8/3 1000 = 1480
assert.strictEqual(recv.cumA[g][idx(3)], 1480, 'FCBGA 8/3 累计');

// scrapped：A005 8/6 plan out 4-2=2 -> FCBGA 8/6 计划=1185
const gs = ship.groups.indexOf('FCBGA');
assert.strictEqual(ship.planA[gs][idx(6)], 1185, 'FCBGA 8/6 出货计划(scrapped)');
// 出货 8/12：A005 10 个 * 100 = 1000
assert.strictEqual(ship.amtA[gs][idx(12)], 1000, 'FCBGA 8/12 出货金额');

// FOFCBGA 8/9 入料量 = A040 9
const gf = recv.groups.indexOf('FOFCBGA');
assert.strictEqual(recv.qtyA[gf][idx(9)], 9, 'FOFCBGA 8/9 入料量');

// 月合计 R%：FCBGA 整月金额/整月计划
const totAmt = recv.amtA[g].reduce((s, v) => s + v, 0);
const totPlan = recv.planA[g].reduce((s, v) => s + v, 0);
assert.ok(Math.abs(recv.pctM[g] - totAmt / totPlan) < 1e-9, 'FCBGA 月合计 R%');

// 汇总表：R%/B% 与单表一致，总计加权
assert.strictEqual(summary.groups.length, 5, '汇总组数');
assert.ok(Math.abs(summary.groups[g].r[idx(3)] - 1000 / 1700) < 1e-9, '汇总 R% 与入料表一致');
const tr = summary.total.r[idx(3)];
const expTr = recv.amtA.reduce((s, arr, i) => s + arr[idx(3)], 0) /
              recv.planA.reduce((s, arr, i) => s + arr[idx(3)], 0);
assert.ok(Math.abs(tr - expTr) < 1e-9, '总计 R% 加权');

console.log('全部断言通过 ✔');
console.log('  组:', recv.groups.join(' → '));
console.log('  匹配', meta.matched, '行 / 忽略', meta.ignored, '行');
console.log('  FCBGA 8/3 R% =', (recv.pctA[g][idx(3)] * 100).toFixed(1) + '%');
console.log('  FCBGA 月合计 R% =', (recv.pctM[g] * 100).toFixed(1) + '%');
