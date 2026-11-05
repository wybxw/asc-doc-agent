---
id: deinterleave-on-910b-via-gathermask
title: 910B 上做 deinterleave 优先用 GatherMask 内置模式 1/2
type: technique
status: draft
confidence: high
summary: DeInterleave 在 2201 不支持；用 GatherMask 的 src1Pattern=1(偶)/2(奇) 直接拆奇偶，比 Gather 偏移表更省
scope:
  architecture:
  - '2201'
  programming_model:
  - tensor_api
  - c_api
  api:
  - GatherMask
  - DeInterleave
  - Interleave
tags:
- deinterleave
- 奇偶分离
- GatherMask
- 数据重排
evidence:
- kind: doc
  path: docs/zh/api/SIMD-API/basic_api/memory_vector_compute/compare_and_select/GatherMask.md
  note: 「功能说明」里 src1Pattern 模式 1/2 明确写的就是取偶数/奇数索引元素
relations:
- type: related
  target: exp-support-differs-by-impl-dir
  note: 都涉及同名 API 按实现目录区分支持性
created: '2026-11-05'
updated: '2026-11-05'
---

## 背景

`DeInterleave` / `Interleave` 在 2201（910B / Atlas A2、A3）上**不支持**：
三份同名文档的 npu 标签都是「不支持」，支持性 CSV 同样 `status=no`，
两路证据一致。原生接口只有 Ascend 950 支持。

要在 910B 上把交织数据拆成偶/奇两路，最省事的是 **`GatherMask` 的内置固定模式**，
而不是自己构造偏移表走 `Gather`。

## 做法

`GatherMask`（`basic_api/kernel_operator_vec_gather_mask_intf.h`）的
`src1Pattern` 内置模式中，有两个正是为奇偶分离准备的：

| src1Pattern | mask 位图 | 语义 |
| --- | --- | --- |
| 1 | `0101…0101` | 每个 repeat 取**偶数索引**元素 |
| 2 | `1010…1010` | 每个 repeat 取**奇数索引**元素 |

910B 支持模式 1–7（310p 只支持 1–6）。Normal 模式（`reduceMode=false`）下
每次 repeat 处理 256Bytes，`mask` 参数无效、建议置 0。

```cpp
// 偶/奇各调用一次；src1Pattern 取 1 / 2
AscendC::GatherMask(dstEven, src, 1, false, 0, params, rsvdCnt);
AscendC::GatherMask(dstOdd,  src, 2, false, 0, params, rsvdCnt);
```

## 为什么优于 Gather 偏移表

- 不用预先生成 `srcOffset` 常量表（省一次 UB 驻留 + 一次搬运）；
- `Gather` 是索引式离散读，每元素一次地址计算；`GatherMask` 是掩码硬选，
  访问模式规整，带宽和 bank 冲突都更可控；
- `Gather` 要求 `srcOffset` 以**字节**为单位且不得越出 UB，构建偏移表本身就是
  一个容易写错的点。

## 边界

- 本卡按官方文档语义推导，**未在真机实测**；上板后建议改 status=verified 并补
  `verified_on`。
- 输出在 repeat 之间是否紧凑连续取决于 `GatherMaskParams` 的 stride 配置，
  写代码前按实际 `repeatTimes` 核一遍。
- 若交织单元本身就是 32B 整数倍，`DataCopyPad` 带 `srcStride`/`dstStride`
  也能搬（GM 侧 stride 单位字节、UB 侧单位 32B），但元素级（2/4 字节）做不到紧凑。

## 相关

`Gather`（membase 版）在 910B 上支持，reg 版不支持——这与
`exp-support-differs-by-impl-dir` 记的「同名 API 支持性按实现目录不同」是同一现象。
