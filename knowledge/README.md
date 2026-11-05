# 个人知识卡片

这里放**自己维护**的算子领域知识——官方文档里没有、或者写得不够细的东西：

* 用某个 API 绕开基础 API 不直接支持的操作；
* 官方文档没提的约束（对齐、大小上限、时序、隐式同步、bank 冲突）；
* 文档没写但实测存在的特性；
* 踩过的坑与规避方式；
* 选型结论（为什么用 A 不用 B）。

和 `asc-devkit/` 的关系：`asc-devkit/` 是**上游官方资料，只读、可整仓替换**；
本目录是**你的判断，可写、要进 git**。两者在回答里必须分开标注，因为官方文档
是外部事实，而卡片是可以写错、可以过期的。

## 文件即真相

一张卡 = 一个 Markdown 文件：`knowledge/cards/<id>.md`，YAML frontmatter + 正文。

没有数据库、没有第二份索引。理由：这些内容是人手写手改的，要能直接用编辑器改、
能 diff、能 code review、能提 PR。加一层数据库只会让「改一条」变成「走工具改」。

## 卡片结构

```markdown
---
id: datacopy-unaligned-tail
title: DataCopy 非对齐尾块必须走 DataCopyPad
type: constraint
status: verified
confidence: high
summary: 非 32B 对齐的尾块用 DataCopy 会越界读到相邻数据
scope:
  architecture: [2201, 3510]
  programming_model: [tensor_api]
  api: [DataCopy, DataCopyPad]
  dtype: [half, bfloat16_t]
tags: [对齐, 数据搬运, 越界]
evidence:
  - path: docs/zh/api/SIMD-API/basic_api/memory_vector_compute/data_move/DataCopyPad_GMToUB.md
    note: 文档只给了 Pad 的用法，没写非对齐尾块用 DataCopy 的后果
verified_on:
  chip: ascend910b4
  cann: 9.0.0
  date: 2026-11-02
relations:
  - type: refines
    target: memory-copy-alignment-basics
  - type: depends_on
    target: ub-bank-conflict-handling
author: wybxw
created: 2026-11-02
updated: 2026-11-05
---

## 背景
...
## 做法
...
## 实测
...
## 边界
...
```

字段说明：

| 字段 | 作用 |
| --- | --- |
| `id` | 稳定 slug，同时是文件名。**改文件名等于换一张卡**，别乱改 |
| `type` | `technique` / `constraint` / `feature` / `pitfall` / `decision` |
| `status` | `draft`（没验证）/ `verified`（有实测）/ `deprecated`（已过时） |
| `confidence` | `low` / `medium` / `high`，主观把握 |
| `summary` | 一句话结论，列表和检索命中时靠它判断相关性 |
| `scope` | 适用范围，见下节 |
| `evidence` | 证据出处：文档路径 / 源码 / 外部链接 |
| `verified_on` | 在哪个芯片、哪个 CANN 版本、什么时候验证过——判断过期的唯一依据 |
| `relations` | 与其它卡片的关系，见「关系设计」 |

## 作用域：四个正交维度

用户口中的「通用 / 不通用 / 架构特有 / 型号特有 / 某种编程语言特有」在这里拆成
**正交维度**，而不是一个单值枚举——因为它们可以叠加。例如「SIMT 编程模型下、
ascend950 上、fp8 dtype 的非对齐搬运约束」同时命中三个维度，单枚举会丢掉组合信息。

| 维度 | 取值 | 留空含义 |
| --- | --- | --- |
| `architecture` | NpuArch 架构号：`2201`（910B/A2/A3）、`3510`（950）、`1001`、`2002`、`3002` | 不限架构 |
| `soc` | SocVersion：`ASCEND910B`、`ASCEND950`… | 不限 |
| `chip` | 具体型号：`ascend910b4`、`ascend950pr_9599`… | 不限型号 |
| `programming_model` | `c_api` / `tensor_api` / `tpipe` / `simt` / `simd` / `scalar` / `aicpu` / `asc_stdlib` / `inline_asm` / `host` | 不限模型 |
| `api` | 相关 API 名，如 `Exp`、`DataCopyPad` | 不限 |
| `dtype` | 相关数据类型，如 `fp8_e4m3fn_t` | 不限 |

**匹配规则**：查询给了某维度就要求有交集；**卡片比查询更具体时不算命中**
（查「通用做法」不该返回某型号专有的卡）。

不要为了「保险」把所有维度都填上——作用域越窄，检索时越容易被漏掉。

## 关系设计

知识天然是网状，而且不是一种边。这里只保留**六种语义互斥**的关系，
避免出现十条边都叫 `related` 的情况。

| 关系 | 语义 | 反向 | 什么时候用 |
| --- | --- | --- | --- |
| `refines` | 本卡是对方的**特化** | `generalizes` | 通用做法 → 「在 950 上要这么改」 |
| `generalizes` | 本卡比对方更**通用** | `refines` | 从具体坑反推出通用规律 |
| `depends_on` | 本卡成立**需要先满足**对方 | `dependency_of` | 解释时沿链展开前提 |
| `supersedes` | 本卡**取代**对方 | `superseded_by` | 结论更新；旧卡标 `deprecated`，**不要删** |
| `contradicts` | 与对方**结论冲突** | `contradicts`（对称） | 两条经验打架且都没被证伪 → **必须人工裁决** |
| `alternative` | 同一目标的**另一做法** | `alternative`（对称） | 选型对比 |
| `related` | 弱关联 | `related`（对称） | 只是延伸阅读 |

两条关键约定：

1. **关系只写在声明方一次**，反向在读取时推导（`card_get` 会同时给 `outgoing`
   和 `incoming`）。双向各存一份必然漂移，这是卡片系统最容易腐化的地方。
2. **`supersedes` 不许代替删除。** 旧卡留着并标 `deprecated`，否则半年后有人
   照着旧卡写代码。`card_check` 会检查「取代了但对方没标 deprecated」。

`contradicts` 是最需要纪律的一条：agent 遇到它**必须两边都报出来**，不许自己挑
一个看起来对的。这属于需要人来裁决的状态。

## 怎么用

对 agent 说人话即可：

```
把这条经验记下来：DataCopy 处理非 32B 对齐的尾块会越界，必须换 DataCopyPad
查一下我们关于对齐的结论
这条约束在 950 上也成立吗
这两条是不是矛盾了
```

对应的工具与工作流（`--workflow knowledge_card`）：

| 动作 | 工具 | 说明 |
| --- | --- | --- |
| 查 | `card_search` | 支持按架构/型号/编程模型/API/类型/状态过滤 |
| 看 | `card_get` | 全文 + 关系图（出边/入边） |
| 记 | `card_save` | 新建或更新；**更新只改传入字段**，省略即保留，传空串才清空 |
| 连 | `card_link` | 建立/删除关系；`remove=true` 删除 |
| 删 | `card_delete` | 需 `confirm=true`；有卡片引用时会列出悬空引用 |
| 检 | `card_check` | 悬空关系、自环、取代未标 deprecated、未知编程模型、verified 缺验证记录 |

agent 侧的纪律（写在系统提示词里）：

- **写之前先查重**，已有覆盖同一作用域的卡就更新，别新建重复卡；
- 引用卡片时必须写明「来自个人卡片 `<id>`（status/confidence）」，不能当官方口径；
- 卡片与官方文档冲突时**两边都报**。

## 手工维护

直接用编辑器改文件就行，工具只是另一条路径。改完跑一次体检：

```bash
python -m asc_agent cards            # 列出全部卡片
python -m asc_agent cards --check    # 体检（悬空关系 / 取代未标 deprecated / 缺验证记录）
```

在对话里直接说「检查一下知识库」也可以（走 `card_check`）。

提交前建议 `git diff knowledge/` 过一眼——知识卡片的价值就在于每个结论都能追溯到
是谁、什么时候、在什么硬件上验证的。
