# 帆软（FineReport 桌面设计器）SQL 指引

> 数据源：PostgreSQL
> 主机 localhost:5432 数据库 postgres 用户 postgres 密码 1
> 核心表：`sdi_mes.sdi_th_wip_lot_transaction`
>
> 以下 SQL 中的列名按常见命名编写，**请先运行"第 0 步"确认实际列名**，若有出入把列名替换为实际值即可。

---

## 0. 先确认表结构（务必先跑）

```sql
SELECT column_name, data_type
FROM information_schema.columns
WHERE table_schema = 'sdi_mes' AND table_name = 'sdi_th_wip_lot_transaction'
ORDER BY ordinal_position;
```

重点确认这几个字段是否存在、叫什么：
- lot_name（批次号）
- lot_type（批次类型，P=正常）
- component_qty（数量）
- product_name（产品名）
- step_name（工序名）
- last_updated_time（更新时间，用于按日归集）
- activity（活动，Pre=入料侧 / Post=出货侧）

如果 `activity` 的实际列名不同（如 `action`、`event_name`），把下面所有 `activity` 替换即可。

---

## 1. 帆软数据连接

1. 设计器菜单：`服务器 -> 定义数据连接`（或左侧数据集面板新建连接）
2. 类型选 **PostgreSQL**，填：
   - 主机：`localhost`，端口：`5432`
   - 数据库：`postgres`，用户名：`postgres`，密码：`1`
3. 测试连接通过后，新建"数据库查询"数据集使用下面的 SQL。

---

## 2. 数据集 A：入料明细（= 9号表入料侧）

与 Excel 宏口径一致：`activity='Pre'`（最开始 step 的 Pre 即入料），`lot_type='P'`。

```sql
SELECT
    lot_name,
    lot_type,
    component_qty AS qty,
    product_name,
    step_name,
    last_updated_time::date AS biz_date
FROM sdi_mes.sdi_th_wip_lot_transaction
WHERE activity = 'Pre'
  AND lot_type = 'P'
  AND last_updated_time::date BETWEEN '${start_date}' AND '${end_date}'
ORDER BY last_updated_time;
```

参数（数据集参数里定义）：`start_date`、`end_date`，格式 `2026-08-01`。
模板参数可用 `=format($month,"yyyy-MM-01")` 之类的公式自动取月初/月末，或在数据集参数里写：
- 开始：`=format(if(len($month)=0,now(),todate($month)),"yyyy-MM-01")`
- 结束：`=dateInMonth(todate($month),-1)` 或 `=format(datelastday(todate($month)),"yyyy-MM-dd")`

> 若某产品入料的 step 不是所有行都为首 step，可加上 `step_name = '${入料step}'` 精确过滤（参数默认值填 IN_RECV）。

## 3. 数据集 B：出货明细（= 9号表出货侧）

```sql
SELECT
    lot_name,
    lot_type,
    component_qty AS qty,
    product_name,
    step_name,
    last_updated_time::date AS biz_date
FROM sdi_mes.sdi_th_wip_lot_transaction
WHERE activity = 'Post'
  AND lot_type = 'P'
  AND last_updated_time::date BETWEEN '${start_date}' AND '${end_date}'
ORDER BY last_updated_time;
```

---

## 4. 数据集 C：单价（两种来源任选）

**来源1：数据库维护**（若已在库里建了单价表，假设表 `sdi_mes.sdi_price(product_name, price_date, unit_price)`）：

```sql
SELECT product_name, price_date, unit_price
FROM sdi_mes.sdi_price
WHERE price_date BETWEEN '${start_date}' AND '${end_date}'
ORDER BY product_name, price_date;
```

**来源2：继续在 Excel 8.单价 维护**：帆软不建数据集，汇总报表的单价通过模板单元格从参数/内置数据集传入，或用帆软的"文件数据集/内置数据集"手填一张产品单价表。

> 说明：现在 8.单价 仍是手工维护的输入表（可选输入，不从数据库拉），帆软侧需要同口径单价才能算金额/达成率，建议方案：在数据库建 `sdi_price` 表，Excel 的 8.单价 和帆软共用；或每次把 Excel 8.单价 导出给帆软。

## 5. 数据集 D：PLAN（同口径）

同样两种来源。数据库维护方案（假设表 `sdi_mes.sdi_plan(product_name, plan_type, plan_date, qty)`，plan_type = 'in'/'out'）：

```sql
SELECT product_name, plan_type, plan_date, qty
FROM sdi_mes.sdi_plan
WHERE plan_date BETWEEN '${start_date}' AND '${end_date}'
ORDER BY product_name, plan_date;
```

---

## 6. 数据集 E：入料汇总（PKG Group × 日期，金额与达成率）

把入料明细与单价、组分类连起来（`4.PKG Group分类` 建议在数据库维护成表 `sdi_mes.sdi_pkg_group(product_name, pkg_group)`）：

```sql
SELECT
    g.pkg_group,
    t.biz_date,
    SUM(t.qty * COALESCE(p.unit_price, 0))                         AS amt,        -- 当日入料金额
    SUM(t.qty)                                                     AS qty         -- 当日入料量
FROM (
    SELECT lot_name, component_qty AS qty, product_name, last_updated_time::date AS biz_date
    FROM sdi_mes.sdi_th_wip_lot_transaction
    WHERE activity = 'Pre' AND lot_type = 'P'
      AND last_updated_time::date BETWEEN '${start_date}' AND '${end_date}'
) t
JOIN sdi_mes.sdi_pkg_group g ON g.product_name = t.product_name
LEFT JOIN sdi_mes.sdi_price p
       ON p.product_name = t.product_name AND p.price_date = t.biz_date
GROUP BY g.pkg_group, t.biz_date
ORDER BY g.pkg_group, t.biz_date;
```

## 7. 数据集 F：入料达成率（R%，单日 + 月合计）

在帆软里做"报表数据集"或直接在模板单元格里：
- 单日 R% = 当日入料金额 / 当日 FCST PLAN（plan in × 单价）
- 月合计 R% = 整月入料金额 / 整月计划金额

参考 SQL（计划金额来自 plan 表）：

```sql
SELECT
    g.pkg_group,
    t.biz_date,
    SUM(t.qty * COALESCE(p.unit_price, 0))        AS amt,
    SUM(pl.qty * COALESCE(p.unit_price, 0))       AS plan_amt,
    CASE WHEN SUM(pl.qty * COALESCE(p.unit_price, 0)) = 0 THEN NULL
         ELSE SUM(t.qty * COALESCE(p.unit_price, 0))
              / SUM(pl.qty * COALESCE(p.unit_price, 0)) END AS r_pct
FROM (
    SELECT component_qty AS qty, product_name, last_updated_time::date AS biz_date
    FROM sdi_mes.sdi_th_wip_lot_transaction
    WHERE activity = 'Pre' AND lot_type = 'P'
      AND last_updated_time::date BETWEEN '${start_date}' AND '${end_date}'
) t
JOIN sdi_mes.sdi_pkg_group g ON g.product_name = t.product_name
LEFT JOIN sdi_mes.sdi_price p ON p.product_name = t.product_name AND p.price_date = t.biz_date
LEFT JOIN sdi_mes.sdi_plan pl
       ON pl.product_name = t.product_name
      AND pl.plan_type = 'in'
      AND pl.plan_date = t.biz_date
GROUP BY g.pkg_group, t.biz_date;
```

## 8. 数据集 G：出货汇总 / B%（同 F，`activity='Post'`、`plan_type='out'`）

把 F 中 `activity` 改为 `'Post'`、`plan_type` 改为 `'out'`、`r_pct` 改为 `b_pct` 即可。

---

## 9. 模板布局建议（与 Excel 1/2/3 对齐）

| 报表 | 布局 | 数据来源 |
|---|---|---|
| 汇总 | 行=PKG Group（R%/B% 两行），列=月合计在最左 + 8/1..8/31，底部"总计"加权行 | 数据集 E/F/G |
| 入料明细 | 行=PKG Group，列=日期：FCST PLAN / 入料量 / 入料金额 / 累计金额 / receiving% | 数据集 A+E+F |
| 出货明细 | 同上，biling% | 数据集 B+F(改) |

单元格百分比格式：右击单元格 -> 格式 -> 百分比（如 `0.0%`），与 Excel 宏的 `0.0%` 一致。
"总计"行建议用帆软的"汇总"分组或 `SUM(...)/SUM(...)` 公式，避免简单平均。

---

## 10. 常见坑

1. **时区/日期**：`last_updated_time::date` 转日期；如需含时区时间用 `AT TIME ZONE 'Asia/Shanghai'` 先转再 cast。
2. **单价当天缺失**：Excel 宏是"向前沿用最近有效单价"，帆软侧若要一致，用窗口函数：
   ```sql
   SELECT product_name, price_date,
          COALESCE(unit_price,
                   LAST_VALUE(unit_price IGNORE NULLS) OVER (
                       PARTITION BY product_name ORDER BY price_date
                       ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW))
          AS unit_price
   FROM sdi_mes.sdi_price;
   ```
3. **首末 step 判定**：若库中每批次有多个 step 的 Pre/Post，且筛选条件按 step 精确定位，则在数据集 A/B 中追加 `AND step_name = '${step}'` 过滤；否则可能把中间 step 的 Pre 也算进入料。
4. **空计划除零**：达成率用 `CASE WHEN 计划=0 THEN NULL` 处理，报表里显示 `-`。
