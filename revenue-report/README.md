# 营收报表（Web 版）

用 Node.js 直连 PostgreSQL、网页端维护输入、自动计算生成营收报表（与 Excel 宏同口径）。

> 说明：浏览器 JS 无法直接连 PostgreSQL（协议限制），所以采用 **Node.js 后端连库（pg 库）**，网页通过接口取数——无需装 ODBC 驱动。

## 快速开始

```bash
cd revenue-report
npm install
npm start
```

浏览器打开 **http://localhost:3000**（默认 3000 端口，可用环境变量 PORT 改）。

## 三个页面

### 1. 报表
- 选月份 + 数据源（演示数据 / 数据库）→ 点「刷新报表」
- 汇总：PKG Group ×（R%/B%）每日达成率，**月合计列在最前**，底部"总计"加权
- 入料 / 出货：FCST PLAN、量、金额、累计金额、receiving% / biling%
- 口径与 Excel 宏完全一致：异常（delay 搬移 / scrapped 扣减）只影响计算；单价留空向前沿用最近有效价；筛选条件按字段匹配（含 step）；组只按 4 号表出现顺序自适应

### 2. 输入维护
5 个编辑器，点「保存输入」写入 `data/inputs.json`（网页端维护，无需碰 Excel）：
- **4. PKG Group 分类**：产品 → 组
- **5. 筛选条件**：行内非空字段全部匹配（类型=入料/出货），新增筛选列直接在表里加列
- **6. PLAN**：矩阵编辑（行=产品×Plan in/out，列=日期）
- **8. 单价**：矩阵编辑，留空=无价（沿用最近有效价）
- **10. 异常**：delay（old_SOD 减、new_SOD 加）/ scrapped（old_SOD 减）

### 3. 数据库连接
- 连接参数（host/port/database/user/password/schema/table，默认已填 sdi_mes / sdi_th_wip_lot_transaction）
- **字段映射**：9 号表字段 ←→ 数据库实际列名。默认同名；若 `activity` 实际叫 `activity_name` 等，改这里即可，无需改代码
- 「测试连接」「按当前月份拉取预览」：先确认字段映射对了再出报表

## 数据库表要求

`last_updated_time` 为时间戳（timestamp），拉取按 `>= 月初 00:00:00 AND <= 月末 23:59:59` 过滤。
字段映射缺失的列会自动从 SELECT 中剔除（如库中无 step_name 也能跑，只是筛选条件里的 step 匹配会失效）。

## 目录

```
revenue-report/
  server.js           Express 服务 + API
  lib/calc.js         报表计算核心（与 Excel 宏同口径，纯函数）
  lib/db.js           PostgreSQL 连接与查询（pg 参数化）
  lib/store.js        配置/输入 JSON 持久化
  lib/mockdata.js     内置演示数据（无库可预览）
  public/             前端页面（原生 HTML/CSS/JS，无构建）
  data/               运行时生成 config.json / inputs.json
  test/calc.test.js   npm test 计算正确性校验
```

## 与 Excel 宏的关系

Web 版与 [营收报表宏.bas](../营收报表宏.bas) 计算口径一致（同一套逻辑的两种载体）。
日常用哪个都行；Web 版连库更直接，Excel 版适合离线/打印。
