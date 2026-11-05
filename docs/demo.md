# 演示记录（离线模式）

下面 5 个问题由 `scripts/run_demo.py` 自动生成，**不需要任何 API key**。

说明：工具调用是真实的（真的走 MCP、真的查索引、真的读技能文件）；
只有「选哪个工具、怎么措辞」由 `MockClient` 的模板完成。
配置 `ASC_AGENT_API_KEY` 后同一套工具与提示词可直接换成真实模型。

```bash
python -m asc_agent index      # 建索引（约 3 秒）
python scripts/run_demo.py     # 重新生成本页
```

---

### Exp 在 910b 上支持吗

- 工作流: `chip_support` ｜ 轮数: 2 ｜ 工具: api_support

<details><summary>回答</summary>

> 离线演示模式：工具调用是真的，模型措辞由模板拼装；配置 API key 后可换成真实模型生成。

**来自工具 `api_support`**

**Exp**：文档标签支持：ascend310b、ascend310p、ascend9030、ascend910、ascend910b、ascend950、ascend_x90、atlas_a3；CSV 支持：ascend910b；CSV 不支持：ascend910b。注意该 API 有多篇文档/多行 CSV 记录，支持性需按目录分别看
- `docs/zh/api/SIMD-API/adv_api/math_compute/Exp_interface/Exp.md`：ascend310b 0/1；ascend310p 0/2；ascend910 0/1；ascend910b 1/1（支持数/原型数）
- `docs/zh/api/SIMD-API/basic_api/memory_vector_compute/basic_arithmetic/Exp.md`：ascend310b 1/2；ascend310p 1/4；ascend910 1/2；ascend910b 1/2（支持数/原型数）
- `docs/zh/api/SIMD-API/basic_api/reg_vector_compute/basic_arithmetic/Exp.md`：ascend310b 0/1；ascend310p 0/2；ascend910 0/1；ascend910b 0/1（支持数/原型数）
- CSV `ascend910b` = no（`docs/zh/api/SIMD-API/basic_api/reg_vector_compute/basic_arithmetic/Exp.md`）
- CSV `ascend910b` = yes（`docs/zh/api/SIMD-API/basic_api/memory_vector_compute/basic_arithmetic/Exp.md`）

<details><summary>原始返回（截断）</summary>

```json
{
  "api": "Exp",
  "chip": "all",
  "found": true,
  "by_chip": {
    "ascend310b": [
      {
        "path": "docs/zh/api/SIMD-API/adv_api/math_compute/Exp_interface/Exp.md",
        "prototype": "",
        "product": "Atlas 200I/500 A2 推理产品",
        "supported": false
      },
      {
        "path": "docs/zh/api/SIMD-API/basic_api/memory_vector_compute/basic_arithmetic/Exp.md",
        "prototype": "不传入config的原型",
        "product": "Atlas 200I/500 A2 推理产品",
        "supported": true
      },
      {
        "path": "docs/zh/api/SIMD-API/basic_api/memory_vector_compute/basic_arithmetic/Exp.md",
        "prototype": "传入config的原型",
        "product": "Atlas 200I/500 A2 推理产品",
        "supported": false
      },
      {
        "path": "docs/zh/api/SIMD-API/basic_api/reg_vector_compute/basic_arithmetic/Exp.md",
        "prototype": "",
        "product": "Atlas 200I/500 A2 推理产品",
        "supported": false
      }
    ],
    "ascend310p": [
      {
        "path": "docs/zh/api/SIMD-API/adv_api/math_compute/Exp_interface/Exp.md",
        "prototype": "",
        "product": "Atlas 推理系列产品AI Core",
        "supported": false
      },
      {
        "path": "docs/zh/api/SIMD-API/adv
```
</details>

</details>

### Cast 怎么用

- 工作流: `lookup_api` ｜ 轮数: 2 ｜ 工具: api_card

<details><summary>回答</summary>

> 离线演示模式：工具调用是真的，模型措辞由模板拼装；配置 API key 后可换成真实模型生成。

**来自工具 `api_card`**

**Cast**（`docs/zh/api/SIMD-API/basic_api/memory_vector_compute/type_conversion/Cast.md`）
- 原型：
```cpp
cpp
  template <typename T, typename U>
  __aicore__ inline void Cast(const LocalTensor<T>& dst, const LocalTensor<U>& src, const RoundMode& roundMode, const uint32_t count)
```
- 同名文档还有：`docs/zh/api/SIMD-API/basic_api/reg_vector_compute/type_conversion/Cast.md`

<details><summary>原始返回（截断）</summary>

```json
{
  "name": "Cast",
  "path": "docs/zh/api/SIMD-API/basic_api/memory_vector_compute/type_conversion/Cast.md",
  "category": "SIMD-API/basic_api/memory_vector_compute/type_conversion",
  "chips": [
    "ascend950",
    "atlas_a3",
    "ascend910b",
    "ascend310b",
    "ascend310p",
    "ascend910",
    "ascend_x90",
    "ascend9030"
  ],
  "headers": [],
  "summary": "头文件路径为 `\"basic_api/kernel_operator_vec_vconv_intf.h\"`。  根据源操作数和目的操作数tensor的数据类型进行精度转换。",
  "support": [
    {
      "chip": "ascend950",
      "prototype": "",
      "product": "Ascend 950PR/Ascend 950DT",
      "supported": true
    },
    {
      "chip": "atlas_a3",
      "prototype": "",
      "product": "Atlas A3 训练系列产品/Atlas A3 推理系列产品",
      "supported": true
    },
    {
      "chip": "ascend910b",
      "prototype": "",
      "product": "Atlas A2 训练系列产品/Atlas A2 推理系列产品",
      "supported": true
    },
    {
      "chip": "ascend310b",
      "prototype": "",
      "product": "Atlas 200I/500 A2 推理产品",
      "supported": true
    },
    {
      "chip": "ascend310p",
      "prototype": "",
      "product": "Atlas 推理系列产品AI Core",
      "supported": true
    },
    {
      "chip": "ascend310p",
      "prototype":
```
</details>

</details>

### Ascend 950 的架构号是多少

- 工作流: `自动` ｜ 轮数: 2 ｜ 工具: chip_facts

<details><summary>回答</summary>

> 离线演示模式：工具调用是真的，模型措辞由模板拼装；配置 API key 后可换成真实模型生成。

**来自工具 `chip_facts`**

**ascend910b1**：`__NPU_ARCH__` = 2201，SocVersion = ASCEND910B，实现目录 `ascendc_case_ascend910b1`

<details><summary>原始返回（截断）</summary>

```json
{
  "chip": {
    "chip": "ascend910b1",
    "normalized": "ascend910b1",
    "found": true,
    "enum_name": "ASCEND910B1",
    "npu_arch": "2201",
    "soc_version": "ASCEND910B",
    "arch_dir": "ascendc_case_ascend910b1"
  }
}
```
</details>

</details>

### UT 怎么生成

- 工作流: `gen_ut` ｜ 轮数: 2 ｜ 工具: read_skill

<details><summary>回答</summary>

> 离线演示模式：工具调用是真的，模型措辞由模板拼装；配置 API key 后可换成真实模型生成。

**来自工具 `read_skill`**

技能 `asc-api-ut-gen`（14058 字符）开头：
```markdown
---
name: asc-api-ut-gen
description: AscendC API 单元测试技能。当用户需要为新 API 编写 UT、补充现有 API 测试覆盖、验证 API 分支路径，或根据 build/cov_report 覆盖率报告补齐低覆盖用例时使用。覆盖高阶API、membase基础API、regbase基础API、C API、SIMT API、工具类API 的 UT 生成，包含分支覆盖分析、参数化测试设计和精度验证。
---
# API 单元测试技能
## 1. 概述
本技能帮助开发者编写 AscendC API 的单元测试（UT），涵盖：
- 测试框架使用（Google Test + mockcpp）
- 环境配置与编译执行
- 高阶API / membase基础API / regbase基础API / C API / SIMT API / 工具类API UT 生成
- 参数化测试设计与精度验证
```

<details><summary>原始返回（截断）</summary>

```json
{
  "skill": "asc-api-ut-gen",
  "file": "SKILL.md",
  "found": true,
  "chars": 14058,
  "body": "---\nname: asc-api-ut-gen\ndescription: AscendC API 单元测试技能。当用户需要为新 API 编写 UT、补充现有 API 测试覆盖、验证 API 分支路径，或根据 build/cov_report 覆盖率报告补齐低覆盖用例时使用。覆盖高阶API、membase基础API、regbase基础API、C API、SIMT API、工具类API 的 UT 生成，包含分支覆盖分析、参数化测试设计和精度验证。\n---\n\n# API 单元测试技能\n\n## 1. 概述\n\n本技能帮助开发者编写 AscendC API 的单元测试（UT），涵盖：\n- 测试框架使用（Google Test + mockcpp）\n- 环境配置与编译执行\n- 高阶API / membase基础API / regbase基础API / C API / SIMT API / 工具类API UT 生成\n- 参数化测试设计与精度验证\n- 基于 `build/cov_report` 覆盖率报告的低覆盖 UT 补齐与回归验证\n\n### 1.1 UT 代码生成方式\n\n**本技能使用 Python 脚本自动生成 UT 代码**，核心脚本位于 `scripts/` 目录：\n\n| 脚本文件 | 功能 |\n|---------|------|\n| `ut_generator_cli.py` | CLI 入口，提供命令行接口 |\n| `ut_generator.py` | UT 代码生成核心逻辑，包含模板系统和生成器 |\n\n脚本运行时必须加载结构化 reference 约束：\n\n| 约束文件 | 用途 |\n|---------|------|\n| `../asc-npu-arch/references/npu-arch-facts.json` | 芯片调用名、`__NPU_ARCH__`、SocVersion、测试目录前缀、dtype 大小和通用模板可直接初始化规则 |\n| `references/foundations/generation-constraints.json` | API 类型列表及 regbase / SIMT 等生成限制 |\n\n### 1.2 API 类别说明\n\n| API 类别 | 目录位置 | 说明 |\n|---------|---------|------|\n| **高阶API** | `asc-devkit/include/adv_api` | 高级封装 API，提供常用算子的高级
```
</details>

</details>

### 有没有 cast 的样例

- 工作流: `locate_code` ｜ 轮数: 2 ｜ 工具: find_examples

<details><summary>回答</summary>

> 离线演示模式：工具调用是真的，模型措辞由模板拼装；配置 API key 后可换成真实模型生成。

**来自工具 `find_examples`**

找到 5 个样例工程：
- `01_simd_cpp_api/04_advanced_api/08_transpose`（11 个文件）
- `01_simd_cpp_api/03_basic_api/00_data_movement`（6 个文件）
- `01_simd_cpp_api/03_basic_api/01_memory_vector_compute`（6 个文件）
- `01_simd_cpp_api/03_basic_api/02_reg_vector_compute`（5 个文件）

<details><summary>原始返回（截断）</summary>

```json
{
  "keyword": "cast",
  "found": true,
  "count": 5,
  "projects": [
    {
      "project": "01_simd_cpp_api/04_advanced_api/08_transpose",
      "files": 11,
      "samples": [
        "01_simd_cpp_api/04_advanced_api/08_transpose/add_broadcast/add_broadcast.asc",
        "01_simd_cpp_api/04_advanced_api/08_transpose/add_broadcast/data_utils.h",
        "01_simd_cpp_api/04_advanced_api/08_transpose/add_broadcast/README.md"
      ]
    },
    {
      "project": "01_simd_cpp_api/03_basic_api/00_data_movement",
      "files": 6,
      "samples": [
        "01_simd_cpp_api/03_basic_api/00_data_movement/broadcast_ub2l0c/broad_cast_vec_to_mm.asc",
        "01_simd_cpp_api/03_basic_api/00_data_movement/broadcast_ub2l0c/data_utils.h",
        "01_simd_cpp_api/03_basic_api/00_data_movement/broadcast_ub2l0c/README.md"
      ]
    },
    {
      "project": "01_simd_cpp_api/03_basic_api/01_memory_vector_compute",
      "files": 6,
      "samples": [
        "01_simd_cpp_api/03_basic_api/01_memory_vector_compute/cast/cast.asc",
        "01_simd_cpp_api/03_basic_api/01_memory_vector_compute/cast/data_utils.h",
        "01_simd_cpp_api/03_basic_api/01_memory_vector_compute/cast/README.md"
     
```
</details>

</details>
