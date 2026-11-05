---
id: exp-support-differs-by-impl-dir
title: 同名 API 在不同实现目录下支持矩阵不同（以 Exp 为例）
type: feature
status: verified
confidence: high
summary: Exp 在 regbase / membase / adv 三处各有一篇文档，910B 上 regbase 版不支持、membase 版支持，按 API 名查支持性会得出矛盾结论
evidence:
- kind: doc
  path: docs/zh/api/SIMD-API/basic_api/memory_vector_compute/basic_arithmetic/Exp.md
  note: 该文件夹与 reg_vector_compute 版的「产品支持情况」章节对照即可
verified_on:
  chip: ascend910b
  cann: 9.0.0
relations:
- type: related
  target: csv-support-path-format
  note: 两者都是资料库层面的坑
created: '2026-11-05'
updated: '2026-11-05'
---

## 现象

`docs/zh/api/SIMD-API/` 下有三篇 Exp 文档：

- `basic_api/reg_vector_compute/basic_arithmetic/Exp.md`
- `basic_api/memory_vector_compute/basic_arithmetic/Exp.md`
- `adv_api/math_compute/Exp_interface/Exp.md`

三者「产品支持情况」章节的支持矩阵互不相同。例如 ascend910b：

- regbase 版：不支持
- membase 版：支持

## 影响

只用 API 名做支持性查询会得到自相矛盾的结果。必须按文档（或按实现目录）分别陈述。

## 做法

查询时对每个文档单独取支持矩阵，聚合成 `{chip: [按文档的行]}`，回答里按文档分开说，
并提示「同一 API 有多篇文档，支持性需按目录分别看」。

## 这个坑的普遍性

不是 Exp 独有：`Cast`、`Add` 等基础 API 同样在 regbase 与 membase 各有一篇文档。
排查任何「支持性结论矛盾」的问题，先看是不是取到了不同实现目录的文档。
