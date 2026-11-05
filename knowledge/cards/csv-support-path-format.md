---
id: csv-support-path-format
title: 支持性 CSV 的 Path 列有两种格式，JOIN 前必须归一
type: constraint
status: verified
confidence: high
summary: api_310b_support.csv 的 Path 是相对 docs/zh/api/SIMD-API/ 的短路径，api_910b_support.csv 是 docs/zh/...
  全路径；直接 JOIN 会大面积对不上
evidence:
- kind: doc
  path: asc-devkit/api_310b_support.csv
  note: Path 列格式与 Status 取值差异可直接对照两个 CSV 看出
verified_on:
  cann: 9.0.0
created: '2026-11-05'
updated: '2026-11-05'
---

## 背景

仓库根目录下有两个支持性矩阵 CSV，但两者的 `Path` 列格式不同：

| 文件 | Path 示例 |
| --- | --- |
| `api_910b_support.csv` | `docs\zh\api\SIMD-API\basic_api\...\Exp.md`（全路径，反斜杠） |
| `api_310b_support.csv` | `adv_api\activation_functions\GeGLU_interface\GeGLU.md`（相对 `docs/zh/api/SIMD-API/` 的短路径） |

另外 `Status` 列的取值也不一样：910b 用中文（支持／不支持／无标注），
310b 用英文（yes／no／none）。

## 做法

短路径补前缀 `docs/zh/api/SIMD-API/`，反斜杠统一成正斜杠，两个文件都按
`utf-8-sig` 读（实测不是 GBK）。

## 实测

补前缀后 1203（310b）+ 1265（910b）条记录全部精确命中 `docs` 表，0 条落空；
而按 basename 兜底匹配会有 347 条落入歧义分支。

## 边界

`Status` 的两种取值要先归一（支持／yes → 同一状态）再比较，否则跨两个 CSV
统计支持率会算错。
