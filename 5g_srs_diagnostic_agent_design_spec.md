# 5G NR SRS 基带问题定位专家系统详细设计说明书

设计基线：v3.4。更新日期：2026-11-10。

本文按功能主题完整描述当前设计、接口契约和实现方案。正文只保留有效规则；当前代码覆盖与待实施工作统一列在第 8 章，不应将设计要求直接视为已交付能力。讨论、方案演进、交付和验证记录见 [设计变更记录](docs/DESIGN_CHANGELOG.md)，整理前全文见 [历史快照](docs/history/DESIGN_SNAPSHOT_2026-11-10.md)。

主诊断流程为 `START → plan → agent loop → explain → report → END`。loop 完成必要取证和验证后冻结证据包；explain 综合形成解释与结论；report 关联每步分析、数据与结论。后两个阶段不返回 loop，新追问创建新动作。

目录：

1. [目标、范围与业务约定](#1-目标范围与业务约定)
2. [系统架构与工程实现](#2-系统架构与工程实现)
3. [数据底座与统计契约](#3-数据底座与统计契约)
4. [知识与领域分析规则](#4-知识与领域分析规则)
5. [专家分析流程与证据契约](#5-专家分析流程与证据契约)
6. [工作台、请求与数据管理](#6-工作台请求与数据管理)
7. [执行服务、预算与持久化](#7-执行服务预算与持久化)
8. [实现覆盖、实施方案与验收](#8-实现覆盖实施方案与验收)

## 1. 目标、范围与业务约定

系统读取 CDT `NR_SRS.*.csv` 宽表，完成确定性清洗、指标统计、相似案例辅助解释、特性文档驱动诊断与交互下钻，以及经人工审核的案例固化。专家分析采用统一 Agent Loop，LLM 负责问题理解、动态选择分析工具、提出与综合假设、解释及草稿提炼；数据计算、查询合法性、证据校验、版本与预算管理由程序执行。分析依据中间结果调整，不能仅凭节点执行成功或模型自述判定问题闭环。

### 1.1 业务边界

1. **流控只在单文件内处理**，不合并不同文件的块。各文件清洗结束后，可以共同参与会话构建、统计和查询。
2. 同文件中，按原始记录顺序划分连续相同 `uwTtiCnt1` 的块；不得先按计数值排序，不得合并不连续的同值块。
3. **流控仅判断原始 CRNTI 是否缺失。块内所有行 CRNTI 非空即为完整块，全部保留，包括单行块；存在 CRNTI 缺失行才执行截断。** TLV 打点的身份字段位于前几个 value，不额外通过尾部字段、日期格式或测量完整性判断流控。
4. `uwData` 表示日期（字段名保持原样），与 `uwTime` 共同确定日期时间；一般最多两天，不通过文件顺序或午夜跳变猜日期。
5. 除非用户特别说明，输入来自同一站点。小区身份使用 `logicCellId`，用户分析会话使用 `logicCellId + crnti + 会话开始时间`。`phyCellInst` 保留为辅助分析字段。
6. 可疑记录和非法流数记录整行退出全部业务统计，原始记录及质量审计保留。RSRP/NI 越界原因独立记录，缺测不等于可疑。所有业务入口执行统一准入，详见第 3 章。
7. `max_stream_gain_diff` 默认表示同一分析范围内各流均值的最大值减最小值。
8. RSRP、NI 默认以 dBm 输入，默认对 dBm 数值直接求算术均值；结果标明该口径。增益仍按字段定义使用 dB，差值单位为 dB。
9. 不考虑 RB 区间重叠。默认 30 kHz；实际流数以每行 ueLayerNum 为准（1/2/4/8；8 流仅有四列输入时只覆盖前四流）；版本差异、非默认配置、多站点等特殊条件由用户在问题描述中说明，本期不实现混合版本自动拆分或多站点自动推断。
10. 相似案例可直接提出，并说明相似点、差异、尚未验证项及可借鉴的推理过程；无需先满足全部定量判据。相似不等于根因确认。
11. 不同数据集独立加载和分析，禁止自动合并统计；同一数据集可包含用户明确归为同一次分析的多个文件。每次查询绑定唯一 dataset_id。

### 1.2 首轮问题上下文

界面提供问题描述及可选补充信息：软件版本、采集场景、非默认子载波间隔或流数、是否包含多个站点、已知现象与外部验证结果。默认条件无需反复确认。

只对实际影响当前分析的缺失信息追问；已明确超出支持范围的配置应告知限制并停止受影响分析，不静默套用默认规则。基础字段映射仍支持按用户描述选取既有版本映射，不建设混合版本解析系统。

### 1.3 技术栈

| 组件 | 职责 |
| --- | --- |
| Streamlit、Pandas、Plotly、Pillow | 三栏工作台、图表、图片读取与图文审核；图片尺寸与格式校验 |
| DuckDB 持久化数据库、PyArrow/Parquet | 数据集单文件数据库、ETL 中间产物与完整查询结果落盘 |
| LangGraph、LangChain Core、langgraph-checkpoint-sqlite | 多轮状态、SqliteSaver 持久化、单向诊断主图与循环子图 |
| Markdown + YAML Frontmatter + images/ | 人工维护的案例权威源文件，解析库可选 python-frontmatter |
| SQLite | 会话注册、检查点、证据元数据、审核记录与案例解析缓存/同步状态 |
| Qdrant、rank_bm25 | 可从权威存储重建的语义与关键词索引 |
| Pydantic v2 | 输入、输出与业务契约校验 |

实现时锁定一组经联调验证的依赖版本，提交锁文件；不以宽松的最低版本声明代替兼容性测试。性能指标通过真实数据基准给出，不预先承诺全量 ETL 毫秒级完成。

## 2. 系统架构与工程实现

### 2.1 分层职责

| 层 | 职责 | 实现边界 |
| --- | --- | --- |
| 数据底座 | 固定输入、CSV 契约、L1～L4 清洗、质量审计、会话及小区参数补齐 | 确定性处理；诊断只读取已发布数据版本 |
| 知识与规则 | 字段语义、特性规则、相似案例及图文资产 | 来源、版本和适用性可追溯；知识内容不作为系统命令 |
| 分析工具 | 受控查询、统计、条件、比较、时序/状态、检索和证据校验 | 工具按固定范围执行，完整结果落盘 |
| 专家分析 | plan、agent loop、explain、report | LLM 动态调度与综合解释；程序执行权限、预算、数据与证据校验 |
| 工作台与资产 | 问题输入、数据管理、图表、追问及人工案例发布 | 普通问题直接执行；案例发布是独立审核动作 |
| 存储与运行服务 | 数据库、事务状态、检查点、不可变证据、模型适配及日志 | 断点恢复、幂等、版本隔离；失败不覆盖成功证据 |

### 2.2 当前工程目录

以下对应仓库现有模块；统一 loop 的职责划分是重构方案，不要求为示意目录另建一套重复实现。

```text
Analysis-Helper/
├── app.py
├── .env.example
├── config/
│   ├── input_contract.json
│   ├── sample_input_contract.json
│   └── question_fields.json
├── srs_helper/
│   ├── pipeline.py / schema.py / database.py / cell_parameters.py
│   ├── knowledge.py / field_metadata.py / cases.py / case_assessment.py
│   ├── agent.py / verification_loop.py / prompts.py
│   ├── plans.py / sql_executor.py / queries.py / requirements.py
│   ├── state_analysis.py / peers.py / cross_layer.py
│   ├── provenance.py / evidence.py / result_summary.py
│   ├── explanation.py / reporting.py / visual.py
│   ├── question_scope.py / intents.py / canvas.py / chart_preview.py
│   └── sessions.py / progress.py / services.py / performance.py
├── knowledge_assets/
│   ├── field_mappings/
│   ├── features/
│   └── cases/                         # 按已发布案例建立目录
├── tests/
├── docs/
│   ├── DESIGN_CHANGELOG.md
│   └── history/
└── workspace_storage/                 # 运行资产，不提交原始数据或密钥
```

### 2.3 模块复用与重构分工

| 模块 | 复用能力与实现方案 |
| --- | --- |
| agent.py、verification_loop.py | 将分散的分析步骤组织为 plan 与循环子图，出口冻结证据包，再顺序解释、报告 |
| plans.py、sql_executor.py、queries.py | 保留 QueryPlan 编译、来源校验、参数绑定及受控只读执行；作为 loop 查询工具 |
| requirements.py、evidence.py、provenance.py | 将字段覆盖、业务目标核账、来源/完整性校验作为每轮与出口检查，保留不可变引用 |
| knowledge.py、field_metadata.py、case_assessment.py | 按当前假设和缺口动态检索/解析规则、评估案例；不另设固定的特性专用顶层流程 |
| state_analysis.py、peers.py、cross_layer.py、visual.py | 按范围和授权提供状态、比较及辅助取证工具；取证在 loop 内结束 |
| explanation.py、reporting.py、result_summary.py | 读取冻结材料，综合结论、确定性降级、逐步报告关联；禁止继续取证 |
| sessions.py、progress.py、services.py | 复用动作锁、事务状态、模型适配和预算，新图/包/输出结构独立版本化 |

### 2.4 运行资产布局与权威来源

```text
knowledge_assets/cases/CASE_ID/
├── CASE_ID.md                         # YAML Frontmatter + Markdown
└── images/                           # 图片、caption 和正文关联

workspace_storage/
├── session_registry.sqlite           # 会话、动作状态及独立命名检查点表
├── knowledge_registry.sqlite         # 审核、解析缓存与索引同步记录
├── qdrant_local_db/                   # 可重建检索索引
├── datasets/{dataset_version}/
│   ├── analysis.duckdb
│   ├── input_manifest.json
│   └── snapshot/
├── sessions/{session_id}/
│   ├── artifacts/                    # 完整表、摘要、图表；目标含冻结包与报告引用
│   └── drafts/                       # 审核前图文草稿
└── logs/performance.txt
```

路径中的 ID 仅示意，不表示已有真实案例或数据。数据目录身份由输入清单、数据指纹和注册表绑定，不以用户可编辑别名定位。案例 Markdown 与图片是知识权威源，SQLite 保存管理记录与缓存，BM25/Qdrant 可重建；完整结果与哈希是证据依据。设计中的 EvidenceBundle、ExplanationResult 和 Report 身份可以落在现有 artifacts 存储中，无需复制业务明细。

## 3. 数据底座与统计契约

数据处理按 L1 保序加载、L2 标准化、L3 单文件流控与质量标记、L4 分析关系构建执行。原始快照、业务数据、质量审计和推定参数分别保存；数据计算结果受固定数据版本、Scope 和统计口径约束。

### 3.1 L1：文件发现、保序加载与固定输入

#### 3.1.1 文件发现与来源标识

递归扫描生产文件名 `NR_SRS_XX-XX_XX-XX-XX.csv` 和 `NR_SRS(1)_XX-XX_XX-XX-XX.csv`，括号内允许任意非负整数。使用大小写不敏感的全名匹配 `^NR_SRS(?:\(\d+\))?_\d{2}-\d{2}_\d{2}-\d{2}-\d{2}\.csv$`，兼容测试文件 `NR_SRS.*.csv`。目录扫描不接受 `NR_SRS_temp.csv`、备份扩展名或其他前缀；显式指定单个 CSV 路径允许任意名称。文件名时间不替代 CSV 内时间。`SYS_INFO.*.csv` 仅作为版本说明的辅助信息；与用户说明冲突时呈现冲突，不自行选择。

CSV 逐文件按原始记录顺序解析，在排序、过滤或并行计算之前固化：

| 字段 | 定义 |
| --- | --- |
| `source_file` | 数据集内唯一的规范化相对路径 |
| `source_file_hash` | 文件内容哈希 |
| `file_row_id` | 文件内从 1 开始的逻辑数据记录序号，不含表头 |
| `record_id` | 数据集 ID、来源文件与记录序号构成的唯一标识 |
| `csv_structure_complete` | 原始记录列数及解析结构是否符合表头 |
| `crnti_present` | 原始 CRNTI 经显式空白/空值编码规范化后是否非空，与数值转换是否成功无关 |

解析器支持引号和嵌入换行；需要定位物理文本行时另存起止行号。不得用无排序的 `ROW_NUMBER() OVER (PARTITION BY filename)` 替代原始记录编号。分批写入 Parquet 或 DuckDB，避免全量宽表转为 Python 对象常驻内存。少列但可解析的记录保留结构缺陷；引号损坏等无法可靠确定记录边界的文件进入加载错误报告，不能静默跳过。

#### 3.1.2 输入契约与异常区分

当前工程使用 `config/input_contract.json` 和样例映射契约保存输入规则，契约至少包含：

- 日期和时间格式、编码、分隔符及空值编码。
- `required_identity_columns`：`uwTtiCnt1, logicCellId, crnti, uwData, uwTime`。
- `flow_presence_column`：固定为 `crnti`，仅检查原始值是否缺失。
- `nullable_measurement_columns`：允许合法缺测的测量列。
- `stream_metric_families`：已确认按流解释的数组列族。

CSV 日期使用 `%Y-%m-%d`；标准及样例契约时间格式分别为 `%H:%M:%S.%f`、`%H:%M:%S`。非法日期保留原始记录并标记 `INVALID_DATE`，不推测格式。旧数据集保留原契约；修正解析规则须重新加载生成新数据版本。

非空 CRNTI 转换失败不改变 `crnti_present`，另报类型解析错误；数值 0 不自动视为空值，除非契约明确将其声明为空值编码。CSV 列数、日期或身份格式、尾部测量缺失和越界值均不能触发流控截断。CRNTI 整列不存在属于文件契约缺失，报告文件失败；列存在但记录值全空则按全空块处理。无法建立块键或可靠解析记录边界时记录输入错误，不虚构流控结论。

字段异常保留原值和独立原因：`MISSING_VALUE / PARSE_ERROR / NON_FINITE / INVALID_DATE / CSV_STRUCTURE_ERROR / INVALID_BLOCK_KEY` 等。只有真实缺测计入缺测数，解析失败和非有限值另行计数；统一投影为类型化 NULL 后仍保留原因。

#### 3.1.3 快照、去重与部分加载

建立本次 `input_manifest`，将文件复制到固定输入快照，核对复制前后源文件与快照内容哈希；读取期间变化的文件有限重试，仍不稳定则列为失败。ETL 只读固定快照，源目录新增或修改不影响本次结果。

按内容哈希与长度识别相同文件，确认字节内容相同后只处理一次，记录全部来源路径和代表文件。去重先于 ETL 和样本计数；内容不同的文件不按时间戳擅自去重。

逐文件使用独立事务或临时输出，失败文件不提交半成品，其他成功文件继续分析。清单记录 `LOADED / DUPLICATE / FAILED`、来源、快照哈希、重复指向、错误和可定位行号。部分成功为 `PARTIAL_READY`，每份报告说明成功文件、记录数、日期范围和失败原因；全部失败为 `FAILED`。不能归属时间的错误不猜测其覆盖范围。

### 3.2 L2：列标准化、重复列与时间坐标

按选择的字段映射对齐列名，显式转换类型；失败保留解析异常，不改成正常值。CSV 精确同名列逐行合并：唯一非空值可用，相同非空文本去重，不同非空文本标记 `DUPLICATE_VALUE_CONFLICT`，不得任意选值。原始数组和逐列位置目录保留，冲突字段不参与数值判定。CRNTI 原始存在性仍取各同名列是否有非空值，冲突不当作缺失触发流控。

L1 完整扫描/统计后可提前移除确认全空的可选列，保留“不存在、全空、部分缺测、可用”元数据；不能以少量样本判定全空，身份、CRNTI 存在性和来源键不裁剪。保序编号先固化，缺失列按需补类型化 NULL。前置投影减少后续内存和物化开销，不免除 CSV 首次扫描。

| 坐标 | 定义与边界 |
| --- | --- |
| `event_ts` | `parse(uwData + uwTime)`，保留完整日期及源精度；缺日期或解析失败不推测跨日 |
| `time_sec` | 相对数据集最早有效时间的秒差 |
| `slot_in_frame` | 默认 30 kHz 下 `subFrmNum * 2 + slot`，范围 `0..19` |
| `freqRbEnd` | `freqRbStart + freqRbNum - 1`，要求长度为正 |

非默认配置按用户说明处理或说明不支持。`uwTtiCnt1` 缺失记录标为 `INVALID_BLOCK_KEY` 并隔离，不加入邻近块；其记录号间隙打断相邻块，计为输入/结构问题而非已确认流控截断。L2 不生成最终 `ue_uid`，先完成文件内流控过滤，防止被截断记录影响会话边界。

### 3.3 L3：单文件流控与行级质量

#### 3.3.1 完整块保留与不完整块截断

各文件独立按 `file_row_id` 顺序处理，连续相同 `uwTtiCnt1` 组成块，块键为 `(source_file, block_seq)`。不能先按计数值排序、合并不连续同值块或跨文件拼块；隔离记录的编号间隙也打断块。

1. 块内全部 `crnti_present=true`：全块 `VALID_FLOW`，包括单行块，不删除最后一行。
2. 块内存在 CRNTI 缺失：取最后一个 `crnti_present=true` 行的从 0 开始块内序号为 `last_good_idx`。它只表示截断边界，不保证尾部测量完整。
3. 不完整块只保留 `idx_in_block < last_good_idx AND crnti_present` 的行；边界行及后续行标记 `INVALID_FLOW_CTRL`，前缀中 CRNTI 缺失行同样排除。
4. 全空块、单行不完整块全块排除。
5. 保存块大小、`has_missing_crnti`、`last_good_idx`、逐行剔除原因和每文件统计。保留原始记录与标签，业务关系通过过滤排除，不销毁源数据。

实现窗口计算分阶段执行：前行 TTI/记录号 → 新块标记 → 累积 `block_seq` → 块内序号 → 块统计 → 流控标签；禁止嵌套窗口函数。输入要求非空布尔 `crnti_present`、已固化编号与可用块键。完整性是唯一流控入口，不提供额外单行块例外开关。

#### 3.3.2 可疑、缺测与解析异常

仅在 `VALID_FLOW` 行上判断测量质量：

- `usrRsrp` 非空且不满足 `-160 < usrRsrp < -60`，或 `srsUeNiDb` 非空且不满足 `-130 < srsUeNiDb < -80`，则整行 `is_suspicious=true`。等于边界也属于可疑。
- RSRP/NI 的真实缺失或列缺失设 `has_missing_measurement=true`；缺测不等于可疑。解析失败和非有限值标记独立原因并投影为类型化 NULL，不混入缺测数。
- 同一行可同时可疑和缺测，分别计数并说明可能重叠，不能相加当作总行数。
- 独立保存 RSRP/NI 可疑原因、原始数值、其他字段的缺测计数及 `error_reasons`。界面按整行标记可疑，解析失败必须可见。

`has_parse_error` 表示数值/日期等解析失败及非有限数字；不因单一显示标签而丢失原始原因。质量标签优先级为 `INVALID_FLOW_CTRL → INVALID_LAYER_COUNT → PARSE_ERROR → SUSPICIOUS → MISSING_DATA → VALID_NORMAL`；块键错误单独隔离。布尔可疑/缺测标记与质量审计不随显示优先级改变。

业务准入统一为：

```text
business_eligible = flow_status == VALID_FLOW
                    AND ueLayerNum in {1, 2, 4, 8}
                    AND NOT is_suspicious
```

可疑记录或非法流数记录整行退出全部业务统计；原始记录和质量审计仍保留。准入后每个指标还需满足自身字段的可解析、有限值及所需身份/时间条件。无关字段解析异常不自动使整行退出业务统计；缺测不补零。

### 3.4 观测会话、身份和状态事件

#### 3.4.1 会话构建与唯一性

流控清洗完成后，对有效且身份、时间可用的记录按 `(logicCellId, crnti)` 分组，以 `(event_ts, frmNum, slot_in_frame, source_file, file_row_id)` 稳定排序。相邻观测间隔 `>180` 秒建立新观测会话，等于 180 秒不切分。

`ue_uid = "{logicCellId}_{crnti}_{session_start_ts}"`，起始时间采用完整日期和源精度的规范格式。同一身份同一时间属于同一观测会话；明细唯一性由 `record_id` 保证，不能将时间戳当作明细主键。数据集内检查会话标识唯一性。

流控有效但身份或时间不可用的记录保留在 L3 和质量审计中，`ue_uid` 允许为空并保存 `session_unassigned_reason`。空 `ue_uid` 不能合并成虚假 UE；UE 汇总排除无法归属记录，时间筛选排除无法定位时间的记录并披露数量。其他统计仅检查自身所需字段。基线分别列出流控有效数、会话可归属数和各指标实际统计数。

这是观测会话划分，不是物理 UE 身份确认；短时间内真实 CRNTI 复用不能仅凭该规则识别。报告人数使用“用户观测会话数”，不声称跨会话真实独立用户数；没有额外身份依据不拼接会话。同一数据集的多文件可在独立清洗后共同构建会话，不改变单文件流控结果。默认单站点，多站点输入需分开分析或提供明确站点身份，不能静默混合。

#### 3.4.2 状态段、变化事件与解释边界

按会话内事件时间组织业务有效观测，构造生效、未生效、未知状态段及变化事件，保存起止观测、条目数、前后记录引用和覆盖限制。会话边界不产生跨会话转换；未知、质量排除、采样缺口和父群体之外的观测构成断点，不能填充为持续生效/关闭。观测跨度不等于持续生效时长，未知两侧状态不同不证明间隔内转换时刻。

只展示相邻可排序有效观测及其前后条件，不自动设置归因窗口。同时间多值冲突时断开，稳定 `record_id` 只能保证可重复显示，不能伪造时序；顺序不明确时仅报告观测差异。同时间不同资源状态不能未经规则合并，状态变化不能自动改变会话划分阈值。

变化核验区分“该记录条件不满足”和“条件在变化前后发生变化”。例如 SPF 核验读取同一事件前后开关、`isAiranActInd`、SINR 及同行门限：有依据时描述条件变化与状态变化一致；条件全满足而实际关闭为规则预期与实际不一致；缺条件则证据不足。保留 `<=` 边界，不能因简称变成 `<`。不自动推断更深层根因、责任或免责，也不以 `codeUsrSpfSw=0` 单独确定终端形态、服务用户或互斥原因。

### 3.5 L4：业务关系与质量审计

| 表 | 粒度 / 键 | 必须保留的内容 |
| --- | --- | --- |
| `l4_ue_resource_ts` | 原始记录粒度，`record_id` 唯一 | `logicCellId, phyCellInst, ue_uid, crnti, resourceId, uwData, uwTime, event_ts, time_sec, frmNum, subFrmNum, slot, slot_in_frame, freqRbStart, freqRbNum, freqRbEnd`、标量测量、质量标记、来源文件和记录号 |
| `l4_ue_stream_ts` | `(record_id, streamId)` 唯一 | 定位与质量字段，以及对应流指标 |
| `l4_ue_summary` | `(logicCellId, ue_uid, resourceId)` | 起止时间、业务样本数、质量计数、各指标有效样本数、RSRP/NI 统计、各流均值、`max_stream_gain_diff`、波动统计 |
| `l4_cell_tf_summary` | `(logicCellId, slot_in_frame, freqRbStart, freqRbNum)` | 样本数、观测会话数、质量计数及比例、指标有效样本数、NI 均值/分位数/最大值、RSRP 均值 |

L3 审计保留可疑、非法流数和缺测等记录；L4 业务明细只收入 `business_eligible=true` 的记录。`sample_cnt` 指业务准入记录数，不等于流控有效数或某指标有效样本数。

测量聚合采用 `EXCLUDE_ABNORMAL_RECORD`：任一可疑指标使整行退出，包括其他正常指标。质量计数仍基于同范围的流控有效原始记录。实现时分别从 L3 审计关系计算质量统计、从 L4 计算业务指标，再按同一分组关联；不能从已排除可疑行的 L4 反算可疑比例。

每组输出 `sample_cnt`、各指标 `metric_valid_cnt`、`suspicious_cnt/ratio`、`missing_cnt/ratio`、`parse_error_cnt` 和实际起止时间。可疑比例、行级缺测比例的分母为该组流控有效原始记录数，二者可能重叠；指标级缺测率单独命名。空分母返回 NULL。常规“异常比”指 `suspicious_ratio`，不等于特性未生效率。所有统计先应用唯一数据集及用户小区、UE、资源、时间 Scope；质量审计和业务结果的范围必须一致。

### 3.6 指标、比较和分母契约

#### 3.6.1 均值、差值与覆盖

RSRP、NI 默认以 dBm 输入，对 dBm 数值直接求算术均值，并标明口径；增益按字段定义使用 dB，差值单位为 dB。未知单位或缩放不得自行推断。

同一数据集内比较 UE、资源或流时，各组独立统计均值、有效样本和异常/缺测比例，不强制取共同记录交集，也不因时间覆盖不同丢弃样本。资源指同一数据集的不同 `resourceId`，不是版本或独立数据集。时间、RB、样本量及覆盖差异作为解释限制，不阻断描述性出表。只有具体规则明确要求逐事件/时间关联时才为该条件建立关联，不能变成全局配对要求。

同一分析范围内：`max_stream_gain_diff = max(各流均值) - min(各流均值)`，至少两流有值才计算，列明实际有值流；两流结果不能称为四流结果。该“均值之差”与逐记录成对差值不同，成对差值必须两侧同记录有效。

全周期汇总仅在数据版本、范围、指标及分组口径一致时复用；否则从明细重算。禁止简单平均组均值或从组分位数拼出总体分位数。缓存键包括数据指纹、筛选、指标定义和规则/计划版本。RB 区间不重叠，不做区间拆分。

#### 3.6.2 波动和时序

`rsrp_std` 使用样本标准差，不足两点为 NULL。`rsrp_trend_delta` 为当前对象有效时间覆盖中点之后 RSRP 均值减之前均值，中点属于后半段；任一侧无数据或起止时间相同为 NULL，不能单凭该值推断单调趋势。

`ue_total_records` 在当前范围按 `ue_uid` 对资源明细 `record_id` 去重，不累加流展开行数。需要时间聚合时，以含日期 `event_ts` 分桶，按 UE、`resourceId`、`streamId`（流表）分别统计均值、异常比、缺测和样本；秒级桶宽需在输出中标注，不能将两天相同 HH:MM:SS 合并。

自然语言趋势查询默认保留原始观测点，不自动分桶、平滑或插值。只有明确请求/计划要求聚合时才使用指定桶宽；图形降采样与查询聚合分开，均不改变已保存完整证据。相同时间多值保留冲突或使用明确的聚合口径，不用记录编号伪造先后。

#### 3.6.3 特性条目、会话和原因比例

SPF 主统计的 N 为当前 Scope 全部业务有效条目数；`N_on/N_off/N_unknown` 分别为 `codeUsrSpfFlag=1/0/缺失或非法` 的条目数。实际生效、未生效、未知比例分别为 `N_on/N`、`N_off/N`、`N_unknown/N`，未知包含在主分母内，`1−生效比例` 不能命名为未生效比例；N=0 返回 NULL/不适用。不按任务去重。可另展示 `N_on/(N_on+N_off)`，但必须命名为“已知状态条目中的生效比例”。

条目、观测会话、变化事件三种分析单位不可混用。无法归属会话的有效条目仍参与条目统计并另列数量。会话内交替状态重点分析变化，不以“曾未生效”替代整个会话状态；本期不自动启用用户级原因占比，其群体定义及原因归属分母待确认。

原因集合必须来自实际未生效群体。各原因可重叠，不要求占比相加为 100%；无明确优先级不生成互斥首因。比例通过 MetricSpec 绑定分子、分母产物、去重键、范围、单位、未知策略和公式，模型只引用程序计算值。特性规则与具体条件分析方法见知识与特性诊断章节。

### 3.7 实际流数与字段维度

`ueLayerNum` 是逐记录有效流数依据，`selLayerNum` 不替代。空、0、负数、非整数或不支持值为异常并整行排除；当前准入集合为 1/2/4/8。同一会话可出现 1→2→4。8 流配置但仅有四列输入时保留实际流数，并说明仅覆盖前四流；有效配置内空值和零值不推断为流未激活。

按已确认流字段族同步构造四元素列表并 UNNEST，缺失下标补类型化 NULL，不能前移；仅展开实际配置及已可观察的流。各流均值用独立有效分母，缺测分母只包含配置内可观察流。标量统计来自资源明细；流组内原始记录计一次，跨流计数按 `record_id` 去重，不能使展开改变异常比。

`instBfSinrGain[0..3]` 已确认对应第一至第四流（STREAM），不归入其他短数组的未确认分类。通常按流序递减，倒序只标记待核查，不直接判故障。全空仅说明增益缺测，不推断采集故障，也不阻断 NI 等独立问题。`isRsrp` 有效索引小于 `rxAntNum`。

`instBfSinrGain` 及其分子/分母属于 L1 测量；`userNum` 是任务内码分用户数，不是整个 Slot 用户总数。责任层、SCALAR/STREAM/BEAM 域和数据层级分别建模，L1 内部字段不全是算法决策；未确认责任或门限不能用于故障归因。

### 3.8 小区级参数连续补齐

字段语义目录的“数据层级”独立于 L1/L2/L3 责任归属，支持小区级、用户级、资源级、流级、波束级、记录级或待确认，空白不推断，兼容三列表。`srsSpfSwitch`、`spfPreSinrThd`、`spfPreSinr3D4DThd` 为已确认小区级字段；重复定义同步标注，层级冲突禁用该字段自动补齐并保留审计。

补齐采用 `cell-continuity.v2` 的 `BRACKET_EQUAL` 连续同值段规则，不使用全数据集众数覆盖。共享键仅 `logicCellId`，不区分载波/BWP，同数据集按 `event_ts` 排序，不设最大时间间隔。

- 前后最近可信值相同，才补齐其间原始缺失；同时间只有唯一有效值时允许共享给该时刻缺失记录。
- 不同值、同时间多值冲突或非法值形成边界，不能跨越，多数票不能覆盖冲突。
- 单侧缺失、无小区身份或无时间均不补齐；`srsSpfSwitch` 只允许 0/1 为可信来源，门限不缩放。
- 来源与接收采用 `BUSINESS_VALID_FIELD_VALID`：既定业务范围内按小区身份、时间、CSV 结构及目标字段质量判断。无关字段解析失败不阻止有效来源或目标缺失接收推定。
- 目标字段非法、非有限、重复冲突和 CSV 错位形成时间边界。核心字段已归一化为 NULL 时仍核对 `error_reasons` 和原 CSV 别名，不将非法值当普通缺失。

输入别名加载后生成策略快照，将字段层级哈希、确认字段、别名和独立补齐策略版本纳入输入契约及数据指纹。完成流控/质量处理后生成 `cell_parameter_fills`；来源不受后续 UE/时间筛选影响，只限固定数据集和小区。这是共享参数解析，不是自动同侪性能比较。

`cell_parameter_fills` 以 `(record_id, field_name)` 唯一，保存推定值、前后来源记录、时间、支持数和方法。查询仅为计划所需字段关联补齐，并导出 `__cell_raw_` 原值及 `__cell_fill_` 来源。原始快照、L2 补充数值和质量标记不改写，异常行不会因此恢复业务准入。

完整产物与验证结果记录使用推定值的记录数；SPF 保留原始条件计数、原始预期状态计数及补齐后结果。会话变化证据携带来源，不能称补齐条件变化为纯实测变化。原始行回查仅允许本轮记录和明确引用的小区参数来源，不开放任意其他用户记录。

模型不得自行填值、取全局众数或无限前向填充，含推定值的判断必须标注。更改字段层级或策略后重新加载生成新数据版本；旧数据及报告不自动补写。

### 3.9 持久化与按需查询物化

L2、L3、L4 和 `_etl_meta` 固化到数据集 `analysis.duckdb`。元数据保存完整指纹、清单哈希、映射/清洗/schema 版本、READY/PARTIAL_READY、成功/失败来源、基线和构建完成时间。路径、大小及 mtime 仅作查找提示，不能单独证明内容一致。

构建中的数据库不对诊断开放，成功后才发布完成元数据。恢复按会话固定版本重开数据库，不扫描原始 CSV；重新加载则核对新清单并生成新版本。缺失、损坏、构建未完成或 schema 不兼容时明确提示，不因文件存在就复用。相同数据版本写入串行化，查询使用受控连接，不预先承诺固定恢复耗时。

规划阶段只读取保存的表头元数据和零行关系模式，不扫描补充数值明细、不构建范围宽表。执行时从条件、分组、指标、比较、关联键和 `required_fields` 提取所需列；特性标志必须显式包含，保证证据和回放一致。

先建立范围内 resource，再以 `record_id` 半连接过滤补充数值表并限定字段，随后聚合、左连接；缺失/非法仍为 NULL。默认四流统计不读补充数值明细，只为使用 stream 来源的计划展开流表。手工 SQL 从已校验 AST 提取字段，`COUNT(*)` 不触发全列展开；显式 `SELECT *` 表示全列，大范围全列查询应收窄范围或指定列。

DuckDB 连接维持 512 MB、2 线程、单次操作 30 秒预算，使用短溢写目录以适配 Windows 深路径约束，不以延长超时替代按需物化。进程总 RSS 不等于 DuckDB 内存上限。性能验收与可复现基准见实施验收和运行观测章节，不将准备阶段基准当作现场全链路保证。

## 4. 知识与领域分析规则

### 4.1 字段目录与语义契约

FieldMeta 包含标准名、原始名映射、中文含义、类型、单位、所在表、数组族、可空性、有效范围及场景标签。target_l4_tables 可包含四张分析表。字段可用性以当前数据实际 schema 为准，文档提到的字段不代表数据里一定存在。

检索相关字段后合并该表适用的身份、时间、质量键。字段语义或单位不能可靠映射时记录待确认项，不自行猜测。无需为每种问题事先写一份 SQL。

字段 Markdown 是语义来源，经校验生成带源哈希、行号及冲突报告的结构化目录；JSON 管理解析类型和别名，不独立重复维护责任归属。空白不覆盖有效说明；重复同名条目冲突不能自动合并。范围写法统一展开，保留原出处，检索按字段或字段族组织。

责任层与 SCALAR/STREAM/BEAM 域独立。instBfSinrGain 及其分子/分母归 L1 测量；userNum 是任务内码分用户个数，不等同于整个 Slot 用户总数。L1 内部字段不全是算法决策，未填写责任归属标为待确认。isRsrp 有效索引小于 rxAntNum；实际流数及数组统计遵守第 3 章的数据契约。

责任主体只形成有依据的排查方向。未核查不等于正常或免责，四流倒序不直接判故障，全空不推断采集故障，未经确认的示例门限不用于判故障。

### 4.2 相似案例辅助解释

CaseCard 包含 case_id、revision、title、scenario_category、symptom_description、applicability、historical_observations、reasoning_chain_template、root_cause、troubleshooting_advice、source_refs、confirmation_requirements、review_status。verification_steps 和 assertions 为可选增强字段，缺少它们不阻止展示相似案例。

先基于问题、当前数据指纹和特性名称召回案例，再展示：

- 相似点：当前实测事实与历史现象分别引用来源；
- 差异点：实际观察到的不一致；
- 尚未验证项：字段或外部日志缺失、条件未检查；
- 可借鉴的排查过程：历史现象如何支持假设，需要哪些进一步证据；
- 当前结论：事实、假设与确认信息分开表述。

相似度只用于检索排序，不是根因概率。不得把案例的历史数值当成当前数据，也不能因无法验证而把案例写成已排除。多案例可并列，无须强选一个。

若案例已有明确可执行判据，可计算 MATCH / NO_MATCH / INSUFFICIENT 作为附加说明，不作为案例展示或解释的前置门禁。断言针对具体对象逐组返回；空结果、缺字段为 INSUFFICIENT。查询失败记录技术错误，不描述为证伪。门限必须来自文档或用户规则，不能为了运行模板临时编造。

### 4.3 知识权威源与检索

案例、字段、场景和特性文档分别索引；使用 Qdrant + BM25 融合召回，案例默认 Top-3 可配置。数据特征展示实测差异与异常比例，没有门限时不强行二分“正常/异常”。知识检索与必要案例核验在 agent loop 内按目标和缺口执行，Top-K 未命中不证明不存在相关知识。冻结证据包记录本轮采用的文档、案例和图像版本，explain/report 只读取包内固定材料。

案例的权威源是 knowledge_assets/cases 下的 Markdown 与图片包；SQLite 保存解析缓存、资产清单、审核记录及同步任务，Qdrant/BM25 为可重建索引，不允许数据库与 Markdown 各自维护一套正文。规则元数据保留原文出处与版本。相似案例入库不要求事先补齐数值阈值，须保留来源、适用条件及历史证据；未知根因作为探索记录/草稿保存。已附可执行查询应回放检查。JSON 仅作为导入导出格式，导入后转为统一案例包。

### 4.4 特性文档知识契约

FeatureSpec 承载特性说明，而不是只把文档作为散乱文本交给模型。支持用户提供文本/Markdown，其他格式经解析适配器转为可引用文本；文档保留版本、哈希、标题和段落定位。规则从文档提取后保留出处和解释，清晰条款可自动进入计划；影响结论的歧义或冲突才请求用户说明。

核心字段：

- feature_id、name、document_refs、applicability：特性、文档版本及适用范围；
- flag：标准字段、启用值、未启用值、缺失/非法值含义；
- conditions：condition_id、原文出处、条件表达式、所需字段、单位；
- condition_role：NECESSARY（必要）、SUFFICIENT（充分）、BLOCKING（禁止）、CORROBORATING（佐证）；
- activation_logic：AND/OR 组合及文档明确的优先级，不默认所有条件简单 AND；
- observation_grain：单记录、UE、资源或事件；temporal_relation：同记录/先后时间/持续时长；
- corroborating_metrics：佐证指标、预期关系和出处；
- unresolved_items：文档歧义、缺字段、缺配置、未覆盖条件。

仅满足必要条件不等于本应生效。只有充分条件或文档明确的完整生效规则成立，才能标记“按文档应生效”。没有说明触发概率、时间或顺序时不能补造。检索文档中的内容作为业务证据，不作为允许执行任意命令或改变系统规则的指令。

### 4.5 特性 B 未生效的分析方法

用户问“特性 B 未生效的主要原因”时，先找到 B 的定义及生效文档，再映射实际字段。下表为通用逻辑，不是假定 B 已有确定的业务条件：

| 条件与观测 | 输出 |
| --- | --- |
| 已知必要条件不满足或禁止条件成立，B=0 | 当前未满足文档要求，列出具体条件 |
| 完整生效规则成立，B=1，佐证一致 | 支持已生效 |
| 完整生效规则成立，B=0 | 预期与观测不一致，继续检查佐证及执行路径 |
| 仅必要条件满足，B=0 | 不能据此认定软件异常，检查其他触发条件 |
| B 与佐证不一致 | 标记观测冲突，检查含义、时间关系或路径 |
| 条件、B 或佐证缺失/非法 | 对相应判断标记 UNKNOWN，不能将缺失 B 当成 0 |

查询覆盖：B 取值分布；各前提不满足的数量/比例；应生效但 B=0 的对象与时间；佐证指标及关键时序。各条件使用 TRUE/FALSE/UNKNOWN 三值，缺失不当作 FALSE。AND 中有 FALSE 可确定整体 FALSE，无 FALSE 但有 UNKNOWN 则 UNKNOWN；OR 中有 TRUE 为 TRUE，无 TRUE 但有 UNKNOWN 则 UNKNOWN。

统计率必须在 MetricSpec 中明确分子、分母及未知策略。通用 B 分析可另展示“已知状态对象中的未启用率”B=0/(B=0+B=1)，与“预期不一致率”（应生效且 B=0 / 应生效且 B 可判断）分别命名；分母为零返回 NULL。非法或缺失 B 单独报告，已知状态率不能替代以全部业务有效条目为分母的主指标；SPF 主指标按 4.6 节定义。各条件展示 evaluated_cnt、unmet_cnt、unknown_cnt，未满足比例使用 unmet_cnt/evaluated_cnt；同时展示相对当前 B=0 分析对象全集的覆盖，不隐去未知样本。

多个条件可能同时不满足：默认提供各条件独立占比、共同评估范围内的重叠计数、涉及 UE 数及覆盖。不能把独立占比相加当作互斥原因分布。仅当文档明确优先级时才计算“首个阻止生效条件”；未知前置条件不得跳过后强行归因。

“主要原因”默认描述最常观察到的未满足条件及证据，不把频率直接当因果。候选覆盖不同且无法公平排序时明确限制；保留“现有条件无法解释”的残余对象。报告采用“文档条件 → 当前数据 → 佐证/反证 → 可能解释 → 下一步检查”的可核对推理过程，可并列引用相似案例。

### 4.6 表达式、SPF 规则与比例口径

Expression 的 eq/ne/gt/ge/lt/le 支持常量 value 或同行列 value_field；二者的非空值互斥。is_null 不接受右侧操作数，逻辑节点只接受 args。例如：`{op: le, field: preSinr, value_field: spfPreSinrThd}`。

左右字段都进入步骤来源校验和按需补充字段清单；同一关系之外的已知字段拒绝并报告 value_field 定位。真正不存在的特性条件字段按 UNKNOWN 处理，不编造值；右侧列名通过同一标识符白名单和转义规则进入 SQL，禁止任意 SQL 表达式、隐式跨表关联和跨数据集比较。类型、单位需兼容，不自动缩放。

SQL 与 Python 特性求值采用一致三值逻辑：比较任一侧 NULL 为 UNKNOWN；AND 遇 FALSE 为 FALSE，否则存在 UNKNOWN 为 UNKNOWN；OR 遇 TRUE 为 TRUE，否则存在 UNKNOWN 为 UNKNOWN。完整 activation_logic 仅能引用条件 ID，右侧引用亦受限制。实际标志非 0/1 为未知，不能计入“应生效但未生效”。

SPF 规则以 `knowledge_assets/features/SPF特性相关分析指南.md` 的版本化 FeatureSpec 为准。小区开关、模型激活、历史门限、用户形态开关、瞬时门限五个必要条件的 AND 为完整预期生效规则，实际状态取 codeUsrSpfFlag。逐行使用门限，精确保留 <= 边界；isAiranActInd=1 是必要条件，=0 时不得预期生效。结构化规则直接执行，不依赖模型提取。模型类型只在实际生效前提下解释，不将未生效时的 spfModelType=0 当作 3D 生效；预期与实际不一致不自动确认根因。

SPF 的版本化 `verification_rules` 定义进一步核验路径，由 agent loop 在相应触发群体存在时执行：

- codeUsrSpfSw 条件失败的条目，继续检查终端形态及 compFlag；若形态和服务身份均满足而开关仍关闭，保留该交集群体并报告互斥或上游决策判据缺口，不默认选择一个具体原因。
- 完整预期生效条件满足而实际关闭的残余条目，继续检查两个筛选阶段标志；保留完整预期条件与实际不一致的群体。筛选标志仅构成观测事实，不能单独证明根因。
- UNKNOWN 条件只有在阻碍完整规则判断时才自动补查，先区分原字段不存在、映射问题、解析无效和目标范围缺测，不因无关空值无限扩展任务。

SPF 条目主指标采用当前 Scope 内全部业务有效条目为总体 N：

| 指标 | 定义 |
| --- | --- |
| N_on / N_off / N_unknown | codeUsrSpfFlag=1、=0、缺失或非法的业务有效条目数 |
| 实际生效比例 | N_on/N |
| 实际未生效比例 | N_off/N |
| 未知比例 | N_unknown/N |
| 已知状态条目中的生效比例（可选） | N_on/(N_on+N_off)，不能替换主指标 |

N 包含 flag 未知的业务有效条目，N_on+N_off+N_unknown=N；1−生效比例不能命名为未生效比例。分母为零返回 NULL/不适用。异常及可疑行整体排除，排除数保留质量审计，不按任务去重。

原因比例须绑定实际未生效群体和完整结果，明确相对于未生效条目或总体的分母。各原因允许重叠，不能强制加和为 100%；无文档优先级不得生成互斥首因。用户级原因占比尚未启用：会话混合状态的归属与原因分母未确定时，不把条目比例改称用户比例。可发布条目比例、观测会话状态分布与变化事件；会话、条目及事件单位不能混用。

### 4.7 图文 Markdown 案例包与解析契约

一个案例一个目录：knowledge_assets/cases/{case_id}/{case_id}.md，图片使用包内 images/ 相对路径。支持手写文件和工作台固化，均经同一解析校验。字段名统一用 applicability、symptom_description 等契约名称；导入旧 applicable_condition 时映射为 applicability 并提示，不重复维护。

案例包含 YAML Frontmatter + Markdown 分节。以下为**格式示例**，未给出实际根因或固定 Slot/RB 范围，不能作为现成诊断规则：

~~~~markdown
---
case_id: CASE_SRS_001
revision: 1
title: SRS 底噪时频分布异常案例
scenario_category: 时频干扰
applicability: 待补充该案例的采集与配置条件
tags: [SRS, 底噪, 时频分布]
review_status: DRAFT
root_cause_status: UNCONFIRMED
symptom_description: 待填写历史观测现象
images:
  - image_id: reference_heatmap
    path: images/reference_heatmap.png
    role: HISTORICAL_MEASUREMENT
    caption: 待填写实际可见的条带、位置与变化特征
    caption_source: HUMAN
    source_ref: null
    evidence_artifact_id: null
---

# 案例说明

## 1. 故障表象与适用场景
填写实测现象、观测时间、小区配置及数据来源。

## 2. 典型图表与视觉特征
![历史底噪热力图](images/reference_heatmap.png)

说明坐标轴、单位、色阶、图上可见的分布特征；
原理示意图与历史实测图分开标注，不将示意图作为实测证据。

## 3. 排查过程与可选查询
说明每一步的问题、表格结果与推断。查询及定量判据可以不填。

## 4. 推理依据、根因状态及未验证项
区分历史已确认信息、推测和还需补充的证据。

## 5. 排查建议
填写可复用检查动作及适用限制。
~~~~

图片并非必填；无图案例可省略 images。有声明但图片缺失时明确显示缺失，不能生成虚假图片说明。多模态功能支持热力图、时序曲线、散点图及现场截图；不得硬编码某些 Slot 一定为 S 时隙，或所有数据都覆盖某固定 RB 数量，必须依据当前配置与数据。

案例解析器使用 Markdown 结构解析：保留 YAML、正文段落、可选 SQL 块、图像引用及相邻视觉说明的关联。CaseCard 为解析投影，增加 bundle_path、source_hash、images、section_refs。ImageAsset 包含 image_id、relative_path、sha256、mime_type、width、height、role、caption、caption_source、source_ref、evidence_artifact_id。caption_source 区分 HUMAN / MODEL，自动生成说明不冒充人工确认。

可执行 SQL 与叙述占位符分离：文字可保留泛化占位符，SQL 必须使用绑定参数并读取受控关系。不得照搬在汇总表上查询原始 srsUeNiDb 等不存在列的示例；可选 Slot 查询样式为：
~~~sql
SELECT slot_in_frame,
       COUNT(*) AS sample_cnt,
       COUNT(srsUeNiDb) AS ni_valid_cnt,
       AVG(srsUeNiDb) AS ni_avg,
       MAX(srsUeNiDb) AS ni_max
FROM scoped_resource_rows
GROUP BY slot_in_frame
ORDER BY slot_in_frame;
~~~
scoped_resource_rows 是执行器已限定数据集、小区、时间、用户筛选及业务准入的资源明细关系。此查询描述分布，无固定异常门限。可疑记录已整行退出该业务关系，可疑比应从同范围质量审计另行取得，不能从业务关系中计算恒为零的比例；可疑比是整行质量比例，不改称 NI 专属越界率。最小/最大 RB 只表示跨度，不能据此声称所有中间 RB 都被覆盖。

### 4.8 图文检索与模型能力分支

索引输入为 YAML 标题/标签/适用条件、故障表象、推理分节及图片视觉说明文本；按案例与章节/image_id 建立可追溯块，再做 BM25 与文本 embedding 混合检索。caption 的文本索引不等于直接对图片编码；不能保证只有图片、没有文字的特征一定能召回。

视觉取证和图文比对在 agent loop 冻结证据包前完成。召回图文案例后：

1. 支持视觉的已配置模型：适配器读取经校验的图片内容，使用服务支持的图像载荷发送历史图与当前查询结果生成的图，附数据表、单位与来源标签。不能仅把本地路径字符串发给远程模型就认为其看到了图片。
2. 纯文本模型或视觉调用失败：使用正文、人工 caption 与当前实测表解释，历史图片仍在界面展示供专家核对。没有 caption 时说明无法自动理解该图，不编造视觉特征。
3. 生成或比较当前图前，记录横纵轴、单位、桶宽、时间范围和色阶；可采用一致显示尺度，但不得通过缩放隐藏差异。模型指出相似形态、不同位置/幅度及未验证项，而非凭“长得像”确认根因。
4. 数值以原始查询结果为准；截图上无法精确读取的值标记不确定。图片、caption 与结构化数据冲突时并列说明，不无条件相信视觉输出。
5. 区分 HISTORICAL_MEASUREMENT / CURRENT_MEASUREMENT / SCHEMATIC / FIELD_SCREENSHOT；历史图不能作为当前测量，原理示意图不能作为历史测量。

图表资产与对应 artifact_id、结果哈希、绘图配置、caption 一起保存并纳入冻结证据包，报告可引用 [image_id] 和表号。explain/report 可以渲染已存结果，但不再调用视觉模型新增观测或检索其他案例。为模型设置图片数量、文件大小与分辨率上限；被截取或省略的图片列入覆盖说明。

### 4.9 图文固化、增量同步与版本一致性

案例固化是诊断图外的独立用户动作。用户触发提炼后，在会话 drafts/ 下生成 YAML + Markdown 草稿，关联关键证据表；将选中的 Plotly/Matplotlib 图导出 PNG 并复制用户上传的现场截图到草稿 images/，自动插入相对引用和说明。图表导出失败时保留表格、交互图与待导出提示，不把不存在文件写成完成资产。

审核表单预览正文、YAML 和图片，允许编辑 caption、删除不适用配图、上传截图；根因未知仍可保存探索草稿。保存操作依照 session/action ID 去重，正式提交须经用户审核，校验包内路径、图片可读性、字段、证据来源和可选 SQL；已附可执行查询按固定数据版本回放核对完整结果。路径必须解析在该案例目录内，不接受越界相对路径或用任意磁盘文件冒充图片；Markdown 内容、代码块和图片说明均为知识数据，不自动执行。

新案例先完整写入临时目录并校验，再发布为正式案例包；更新既有案例使用基于 source_hash 的冲突检查，保留旧版本快照，避免覆盖专家同期修改。文件发布后记录 SQLite 同步任务；若进程在两步之间退出，启动扫描可依据包哈希补建任务，不假定跨文件系统与 SQLite 存在原子事务。

知识同步器在启动、显式刷新或后台变更检测时，按 Markdown 与图片内容哈希增量同步：

- 新增/修改：重新解析受影响章节及 caption，生成新版本索引；
- 删除：当前检索不再返回旧索引，保留既有报告引用的历史版本；
- 解析失败：显示文件、错误和上次成功版本，不静默用陈旧内容当新内容；
- 重复 case_id：报告冲突，不随机覆盖；
- 索引失败：案例文件保持已保存，可幂等重试，UI 分别显示保存与索引状态。

SQLite 缓存、BM25 和 Qdrant 均能从案例包重建；审核记录、报告引用与证据元数据仍持久保存。已有报告绑定案例 revision、源文件哈希与图片哈希，后续人工改图不改变旧报告所依据的历史证据。

## 5. 专家分析流程与证据契约

### 5.1 主图与职责边界

~~~mermaid
flowchart TD
    S([START]) --> P[plan：明确问题 范围与完成标准]
    P --> L[agent loop：动态取证 验证与冻结证据包]
    L --> E[explain：综合解释与结论]
    E --> R[report：关联分析步骤 数据与结论]
    R --> F([END])
~~~

**流程入口**：数据加载与 ETL 在诊断图之外完成，构建或复用固定输入，支持 PARTIAL_READY，保留成功/重复/失败来源及质量计数。首轮与追问使用同一张诊断图，每次动作绑定一个数据集及固定 AnalysisRequest。普通问题采用第 6 章的默认直接执行，不新增逐工具人工确认。

**plan**：结合问题、实际字段目录、当前筛选及兼容的历史证据，明确本轮目标、统计单位、分母、解释深度与完成标准，形成初始 AnalysisPlan。初始计划不是一次性固定 SQL 清单；loop 可依据新证据修订步骤，但不能擅自扩大问题范围或改变统计口径。

**agent loop**：统一调度知识检索、字段检查、QueryPlan 查询、特性条件验证、时序/会话状态、授权范围内的比较和证据校验。每轮按实际结果更新目标、假设、矛盾和缺口，再决定下一步。SQL 语法/绑定错误最多修复两次，真实缺字段不盲目重试；保留成功结果和独立失败原因。出口完成问题覆盖与停止检查，冻结 AnalysisEvidenceBundle。

**explain**：仅基于冻结证据包，综合数据发现、规则和已有假设评估形成直接回答与解释。允许有依据的归纳推断，不新增观测事实、未经检验的关键前提或确定性归因；不查询业务数据、不检索新知识、不返回 loop。证据不足或存在冲突时明确限制，保留部分结论。

**report**：基于同一证据包与已校验的解释，展开各步分析目的、方法、数据、发现和结论关联，支持从结论回查完整产物及来源。包含有效排除结果、残余问题、停止原因和加载覆盖；不独立重判原因，不执行补查。输出失败可在相同证据包上有限重试或降级，不回流诊断。

**图外案例固化**：作为独立用户动作，将证据包、报告及讨论提炼为 Markdown 图文草稿，按第 4 章的案例发布契约归档并经用户审核保存。断言可选，未确认根因保留为探索记录；不能因本轮回答完成而自动发布案例。

“本轮能够拿到的所有数据和信息”指在固定数据、问题、口径及可用知识范围内，可能实质影响回答的必要证据已经取得或已确认其获取阻碍，不要求扫描全部列、穷举全部字段组合或检索全部案例。不将无限扩大问题当作深入分析，也不以减少节点数作为可靠性证明。预算耗尽、技术失败、无进展只表示未完成，不能声称已穷尽可用信息。

### 5.2 状态与上下文

`DiagnosticState` 至少包含：

| 类别 | 字段 |
| --- | --- |
| 数据 | `session_id, dataset_id, dataset_fingerprint, data_root_dir, problem_context, mapping_id, pipeline_status` |
| 基线 | `baseline_stats, field_availability, dropped_empty_columns, anomaly_fingerprint` |
| 交互 | `interaction_action, user_input, target_filters, turn_id` |
| 验证 | `candidate_cases, similar_cases, optional_verification_results, feature_spec, document_refs, query_plan, condition_results, sufficiency_results` |
| 累积 | `table_artifacts, chat_history, confirmation_records` |
| 输出 | `latest_output, case_draft, errors` |
| 本轮任务 | `analysis_request_ref, analysis_context, parent_analysis_id, parent_bundle_ref, goals, completion_criteria, plan_revision` |
| 循环 | `loop_state_ref, task_ledger, hypothesis_refs, missing_information, conflicts, budget, stop_reason` |
| 单向交接 | `graph_version, phase, evidence_bundle_ref, evidence_bundle_hash, freeze_status, explanation_ref, report_ref, output_status` |

`pipeline_status` 取 `UNLOADED / NEED_CONTEXT / LOADING / READY / PARTIAL_READY / FAILED`；交互动作取 `INITIAL_DIAGNOSE / FOLLOWUP_CHAT / CRYSTALLIZE_CASE`。READY 和 PARTIAL_READY 都可进入诊断，后者每份报告附加载范围与失败说明。

数据指纹涵盖源文件内容、字段映射版本、输入契约与清洗配置。变更后重新建模并生成新数据版本，旧证据不能继续用于新版本结论。筛选变化更新当前分析范围，不必重新执行全量清洗。

状态只保存引用及有界摘要；图版本与动作绑定，旧检查点不得按新图中的同名节点直接恢复。`phase` 顺序为 PLAN、ANALYZE、EXPLAIN、REPORT、DONE；ANALYZE 内允许循环，冻结后不得退回 ANALYZE。

### 5.3 plan：目标、范围与完成标准

plan 输入固定 AnalysisRequest、实际 schema/字段目录、当前界面范围、用户说明及显式继承的历史证据。必要的 schema/来源元数据可在入口读取；新的业务文档检索、数据取证及假设核验由 loop 执行，计划中的待确认规则不能当作事实。

AnalysisPlan 至少记录：

| 项目 | 契约 |
| --- | --- |
| 身份与上下文 | plan_id/revision、analysis_id、request_hash、数据及规则版本、父分析引用 |
| 本轮问题 | 原问题、补充问题、用户纠正、effective_question；区分新增目标与被修正的旧假设 |
| 范围和口径 | Scope、cohort、分析单位、MetricSpec、分母、质量策略、允许比较的对象 |
| 业务目标 | 每项 goal_id、要回答的内容、所需解释深度、完成标准及依赖 |
| 初始策略 | 待检索知识、待查指标、候选工具和依赖；未知字段/规则显式保留待解析 |
| 执行约束 | 工具能力目录、总预算、阶段预算、重试与产物限制 |

完成标准必须对应问题：均值问题要求范围、单位、样本和质量口径及已计算值；原因问题要求候选条件的证据、影响范围、比较口径、反证及未解释部分；根因问题需要相应的业务或因果判据。没有判据时不得将关联结论升级为根因。loop 可修订实现步骤与补充子目标，但不能以降低完成标准绕过原问题，也不能擅自增加用户未授权的分析范围。

### 5.4 agent loop：动态工具调度

内部以可持久化子图或等价状态机实现“盘点 → 选择行动 → 校验并执行 → 评估结果 → 更新目标与缺口 → 出口检查”。顶层一个 agent loop 不意味着把全部能力写成一次无检查点的长模型调用。

| 工具类别 | 能力与约束 |
| --- | --- |
| 知识检索 | 按当前问题、假设和缺口检索字段、特性文档、案例及图文资产；保留版本、出处、相似点和差异，Top-K 未命中不等于知识不存在 |
| 字段与覆盖盘点 | 查完整目录/映射及目标范围内有效性，区分未查询、缺失、全空、解析异常和覆盖不足 |
| 通用数据查询 | 复用 FILTER、GROUP_AGG、CONDITION_COUNT、COMPARE、TIME_SLICE、TEMPORAL_RELATE 及原有受控执行器；按需读取列，不局限于 CONDITION_COUNT |
| 特性及状态分析 | 执行可信 FeatureSpec、三值条件、残余群体分析、观测会话及变化核验；既有时间/身份边界不变 |
| 对象及指标比较 | 在请求授权范围内比较 UE、资源或流；沿用已确认的样本、单位、分母和同侪约束 |
| 图文辅助分析 | 若回答需要且服务可用，在 loop 内完成视觉比对；结果标明辅助证据等级，不能替代精确统计 |
| 证据校验与汇总 | 校验完整结果、数值、单位、范围、来源及统计一致性，生成目标级摘要与完整产物引用 |

每项任务保存 task_id、goal_id、可选 hypothesis_id、purpose、tool_name/version、inputs、依赖、scope/cohort、期望获得的证据、预算、指纹、执行状态、结果引用及错误。非查询任务同样记账；描述性任务无需虚构可证伪假设。通用目标可以基于当前范围建立受控群体，不依赖特性分析预先产生 feature.cohorts；新群体必须由已执行关系或明确范围派生。

LLM 选择候选行动并说明分析目的，程序校验能力、字段来源、权限、范围和预算后执行。结构化规则已能确定的步骤可直接执行，不必增加模型调用。计划合法性不等于业务结论成立；查询成功不等于假设成立；引用合法不等于因果关系成立。

每轮用新增证据重新评估业务目标、假设、残余群体、冲突和缺口。已支持的假设可以因反证降级，未完成目标也可因补查完成升级；不只记录静态的循环前 goals。原因分析必须考虑当前证据支持的替代解释和关键反证，不能找到一个支持案例就提前闭环；无需穷举与本题无关的所有可能原因。具体 SPF 排查规则由版本化领域规则承载，通用调度器不继续累加特性专属分支。

需求保存问题相关性、指标、维度、目的、范围、文档版本和出处。仅当前问题相关需求进入核账，RSRP/NI/增益均非强制项。Top-10 只影响显示，不能缩小问题 Scope。字段目录分别记录存在性、有效性和查询状态；全空、解析失败、范围无有效值、未查、失败、不支持不混同。

覆盖必须关联成功证据及核查目的，SQL 出现字段不代表完成分析。零行结果可标记执行成功但证据不足；缺少业务准入记录时仅返回质量事实。增益全空仅影响依赖增益的结论，不阻断其他目标。

仅用户明确要求时启用同侪比较，模型不得自行扩大。按 logicCellId、分钟窗、相同 RB 区间、Slot、起始符号 ueSymPos 匹配，排除目标 UE；候选同样执行业务准入。缺少维度不得放宽匹配。自然分钟分桶作为当前实施默认，明确事件时间基准；多符号覆盖不能确认时排除比较并披露原因。基线需列聚合方式及各 UE 样本量；空间位置差异未受控，比较不证明因果。

### 5.5 QueryPlan 与查询算子

AnalysisPlan 表达本轮业务目标与分析策略；下述 QueryPlan 是 loop 调用查询工具时的执行契约，两者分开版本管理。loop 可以生成多份有依赖的 QueryPlan，也可调用知识检索等非查询工具，不要求全部问题通过单份 QueryPlan 回答。

QueryPlan 的最小契约：

| 字段 | 定义 |
| --- | --- |
| plan_id、revision、dataset_id、dataset_fingerprint | 计划身份与固定数据版本 |
| question、scope | 问题、当前小区/UE/资源及左闭右开时间范围 |
| document_refs、case_refs | 文档段落及参考案例，含版本 |
| field_bindings | 术语到实际字段的映射、单位与可用性 |
| steps | 有序步骤，每步含 step_id、depends_on、purpose、operator、inputs、parameters、outputs |
| condition_refs | 步骤验证的文档条件及三值逻辑 |
| output_grain、result_key | 每行代表的对象及唯一键 |
| quality_policy | EXCLUDE_ABNORMAL_RECORD，整行排除可疑和非法流数记录 |
| expected_columns、units、denominators | 输出列、单位与比例分母定义 |
| limitations | 缺字段、规则不确定与覆盖限制 |

执行前校验操作语义：CONDITION_COUNT 必须提供 conditions 字典，键为 c0..c999、共 1～20 项，禁止用 condition 或 metrics 替代；检查各算子必填参数、聚合输出冲突及时间范围。错误以位置和规则反馈模型，最多修复两次；仍失败可降级为描述性基础计划，但原目标保持未完成，不能把降级统计当作原问题已闭环。

按真实字段目录逐步推导关系输出：聚合只保留分组列、sample_cnt 和指标输出；条件计数汇总及 `_rows` 明细分别建模。比较、分组、聚合和时间关联必须引用当前 source 的列，depends_on 不替代 source 切换。FIELD_SOURCE_MISMATCH 反馈错误位置与可用关系，执行入口再次校验；真正缺失的特性条件字段仍按 UNKNOWN 处理。

通用算子首版实现 FILTER、GROUP_AGG、CONDITION_COUNT、COMPARE、TIME_SLICE；文档有明确时序规则时使用 TEMPORAL_RELATE。字段、函数、运算符由白名单选择，值参数绑定；条件采用结构化表达式树，不 eval 模型生成的表达式。

CONDITION_COUNT 同时生成条件聚合摘要和内部逐记录/事件状态关系 condition_evaluated_rows，后者保留对象键、B 值与各条件三值状态，供后续筛选使用。计划依赖必须指向实际产出的关系，不能从聚合计数反推异常记录。具体填写样式见同目录 query_template_style.md。

执行器根据已实现算子编译参数化 SQL。FILTER 固定用户范围；GROUP_AGG 计算均值、计数和异常比；CONDITION_COUNT 计算 TRUE/FALSE/UNKNOWN；COMPARE 比较各组独立统计；TIME_SLICE 返回有界明细；TEMPORAL_RELATE 必须注明对象键、先后顺序、时间窗口和一对多处理方式，避免关联放大样本。缺时间规则时保留未知，不猜时间容差。

常见查询直接编译；复杂需求可让 LLM 生成单条只读 SQL，但只读取受控当前范围的关系。首版不承诺理解任意 SQL 的全部语义：超出已实现检查能力的结果可作为探索表，明确标注限制，不用于自动判定特性条件或确认根因。特性自动判定优先采用可编译计划。

示例查询次序：B 的取值分布 → 条件未满足与未知数量 → 有充分依据认定应生效但 B=0 的对象 → 佐证与时间切片。各步可按依赖执行并复用结果，设置单轮步骤数、时限与结果上限，防止无界自循环。查询计划摘要在界面可查看，常规查询无需逐条人工审批。

### 5.6 任务、假设与进展状态

LoopState 引用既有 AnalysisContext、CohortRef、MetricSpec。状态包含 goals、completion_criteria、plan_revision、tasks、validation_tasks、hypotheses、evidence_refs、residual_cohorts、missing_information、conflicts、query_ledger、budget、stop_reason、round 及 checkpoint 版本。tasks 覆盖检索、字段检查、统计、比较、时序和验证，validation_tasks 是其中的验证任务；不再限定为已有特性群体上的记录谓词。只持久化引用和有界摘要，不把全部明细复制进图状态或 prompt。

每个 ValidationTask 至少记录：task_id、goal_id、可选hypothesis_id、scope/cohort_id、所需字段与单位、来源关系、判据及版本/出处、预期支持与反驳结果、UNKNOWN处理、计划指纹、依赖任务、证据引用、执行状态及失败原因。没有来源的生产阈值、关联窗口或因果判据不得由模型编造；探索性描述可以执行，但不能将描述结果提升为根因确认。

假设状态与任务状态分开：

| 假设状态 | 含义与发布约束 |
| --- | --- |
| PROPOSED / TESTABLE / VERIFYING | 内部待验证，不作为已解释问题的最终结论 |
| SUPPORTED | 查询结果支持明确判据；注明适用群体及证据等级，不自动表示因果成立 |
| REFUTED | 当前范围证据不支持该假设；不能扩展为全局排除 |
| INCONCLUSIVE | 已执行验证，但结果不足以支持或反驳，记录具体原因 |
| BLOCKED_DATA / BLOCKED_CRITERIA | 经盘点确认缺少输入/映射或判据，列出缺口 |

任务另记录 PENDING、RUNNING、SUCCEEDED、FAILED、BLOCKED，并与目标完成状态关联。一个查询成功不代表假设成立；整个集合只部分满足时，应拆分支持、反驳和未知的子群体，不能把少量支持样本推广到全范围。

任务指纹包含目标、目的、可选量词与查询身份；查询指纹独立绑定数据/规则版本、范围、输入群体、算子、规范化参数和统计单位，不因不同目标复用同一计算而重复扫描。兼容的成功证据可被多个任务复用；重复请求相同已完成查询不得计为新进展。恢复检查点后核验文件哈希与版本，恢复待执行队列而不是重跑全部工作。

记录谓词任务支持 ALL/ANY：ALL 遇到 False 即反驳，有 Unknown 且无反例为未定；ANY 有 True 即支持，无 True 但有 Unknown 为未定；空群体不作空真推断。True/False/Unknown 全量计数及 record_id 子群体均保存，后续可选择残余群体。支持某个谓词仅支持其明确条件，不自动支持绑定的整个自然语言原因陈述。字段缺口核验保存完整文件头/映射检查、群体空值数和解析失败数；提示词未包含字段不构成缺失证据。

进展以新增有效证据、假设状态变化、残余群体细化或缺口得到证明衡量，不以模型文字变长或轮数增加衡量。查询失败可做有限修复；无进展时检查是否规划策略问题，不将其自动转为DATA_INSUFFICIENT。用户明确的范围、禁止跨会话拼接、同侪比较需明确请求等约束贯穿所有轮次。

### 5.7 停止条件与出口核验

在权限、范围和执行预算内，存在未完成且可执行、可能实质影响本轮答案的必要任务时继续循环；不会因为模型已经产出一段看起来合理的解释而停止。每轮依据新增证据重新计算目标状态，既能从 INSUFFICIENT 转为 VERIFIED，也能因反证降级，不只沿用循环前状态。规划器返回空任务、任务队列清空、字段覆盖完整均不能单独证明闭环；出口检查完成标准、关键冲突及残余群体。

| stop_reason | 判定依据 | 用户看到的含义 |
| --- | --- | --- |
| GOALS_VERIFIED | 本次问题必要目标全部有完整、兼容、通过校验的证据 | 正常完整回答；达到问题所需深度即可停止 |
| DATA_INSUFFICIENT | 完成相关目录、映射、群体和数据有效性盘点后，仍缺少必要字段、时间覆盖、有效样本或关联信息 | 回答已验证部分，逐项指出缺失数据及影响 |
| CRITERIA_REQUIRED | 有数值但没有可执行且可信的判据/口径，不能继续确认 | 说明缺少规则或业务定义；不能说成数据缺失 |
| CLARIFICATION_REQUIRED | 指代、分母、范围等存在无法安全消除的歧义 | 提出必要澄清，保留独立部分结果 |
| TECHNICAL_FAILURE / UNSUPPORTED_OPERATION | 模型、查询、算子或文件操作在有界修复后仍失败 | 明确技术未完成；不推断数据无法支持 |
| BUDGET_EXHAUSTED / NO_PROGRESS | 累计预算耗尽，或多次无新证据且无法生成不同的合法验证计划 | 发布不完整状态、待执行任务及恢复信息；不宣称证据穷尽 |
| USER_STOPPED | 用户主动停止 | 保留检查点和已验证事实 |

DATA_INSUFFICIENT 必须有缺口证据：不能因知识检索只返回Top-3、预览只显示10行、字段未放入prompt、一次查询零行或模型说“没有数据”就判为缺失。零行可能意味着该目标群体为空：比例应不适用，或假设在当前群体不获支持；需按判据解释，不一律要求用户补数据。

预算/轮数/单查询时限/内存/产物数仍需受控，不能为“持续深挖”无限执行。它们是执行保护而非业务证据。暂停恢复保留累计消耗；只有用户明确新增预算才能扩展预算，不能通过重试悄悄归零。预算阈值进入配置和运行记录，不在未实测时承诺固定轮数可以查清所有问题。

每项缺口记录：关联goal/task/hypothesis、字段名或尚未映射的信息类别、缺口类型、期望粒度/单位、目标会话/群体/时间范围、已检查的目录/查询/产物、缺失或无效样本数（可以计算时）、阻断的判据、需要补充的数据或业务说明。字段名未知时写“需要哪类信息并确认映射”，不能猜一个CSV字段让用户提供。

缺口类型至少区分 FIELD_ABSENT、MAPPING_UNRESOLVED、ALL_NULL、PARTIAL_MISSING、INVALID_VALUES、TIME_COVERAGE_GAP、JOIN_KEY_MISSING、CRITERION_UNDEFINED；技术失败、未查询和预算未完成不进入“数据缺失”计数。

loop 冻结前逐个问题目标检查：证据完整性、数据版本和范围、父群体、分子分母单位及去重键、判据出处、查询全量性、数值和业务一致性、仍可执行的待验证任务。引用有效仅是必要条件。影响回答的候选原因必须在 loop 内提出并验证或记录具体阻碍；不得推迟到 explain 再取证。冻结后生成器新增包内无依据的原因时，输出校验拒绝该陈述并在同一证据包上有限修复，仍失败则降级；不创建验证任务、不返回 loop、不改写原停止原因。

有多个阻碍时，保留每个目标/任务的原因列表；顶层主停止原因不能覆盖其他缺口或失败。空任务、重复查询、没有新想法和上下文截短不是“已获得全部信息”的依据。以新增有效证据、已证明缺口、冲突处理或目标/假设状态的实质变化衡量进展，重复检索同一版本材料不计新进展。

### 5.8 不可变证据与冻结证据包

`ExecutedTableArtifact`：

- `artifact_id`：新执行生成新 ID，不能覆盖历史结果；
- `dataset_fingerprint, turn_id, title, purpose, sql, parameters, filters, query_hash, input_manifest_id, included_files, quality_policy, quality_scope, suspicious_ratio, coverage_summary, plan_id, rule_version, document_refs`；
- `result_path, result_hash, total_row_count, preview_row_count, is_truncated`；
- `markdown_preview, column_units, metric_sample_counts, is_pinned_evidence, chart_refs`。

完整结果落盘，UI 分页读取，模型读取有界摘要及覆盖样例，具体限额见第 7 章；验证算子必须使用完整范围计算结果，不能从预览推断全表。

`EvidenceClaim` 绑定 `claim_id, artifact_id, result_hash, row_key, metric, value, unit, scope`。差值、比例等派生值必须绑定可追溯的已执行计算。`evidence_validator` 检查数值、单位、舍入容差、范围与结果哈希；关键数字通过结构化 claim 渲染，不能只检查报告里是否出现表号。叙述不得扩大 claim 支持的范围。

出口生成不可变 `AnalysisEvidenceBundle`（目标 schema：`analysis-evidence.v1`）：

| 内容 | 必需字段或含义 |
| --- | --- |
| 包身份 | bundle_id/schema_version、analysis_id/action_id、request_hash、graph_version、created_at、bundle_hash |
| 范围及来源 | 数据指纹、输入清单与成功/失败文件、Scope/群体、字段映射/规则/质量版本、统计单位及口径 |
| 问题与完成情况 | 原始及有效问题、AnalysisPlan 修订、goals/completion_criteria、最终目标状态和逐项目依据 |
| 分析步骤 | step/task_id、目的、工具及参数引用、依赖、来源关系、输入输出产物、状态、可公开的分析依据 |
| 观测与计算 | fact/claim_id、MetricSpec、完整结果及摘要的路径/哈希、范围/样本/覆盖/单位、来源行回查引用 |
| 假设与冲突 | hypothesis_id、支持/反驳/未定状态及依据、残余群体、矛盾与影响、明确的未知项 |
| 知识与案例 | 文档/规则/案例的版本、哈希及定位，引用片段或固定副本，适用性、相似点/差异和未验证项 |
| 结束与复用 | stop_reason、各项阻碍、待执行任务、预算消耗、父证据包、复用产物及兼容性检查 |

证据包冻结不是把所有行放进 JSON。完整结果继续保存在不可变产物中，包保存引用、哈希及目标级汇总；explain/report 可按目标分页读取包内已保存材料。需要额外聚合、跨表关联、字段查询、外部检索或视觉取证的操作均属于分析，必须在冻结前执行。图表可依据已有结果渲染，但新统计、推断或改变分组口径不能以“画图”为名放到 report 中。

冻结前先校验引用并写入不可变包，再事务提交包引用、哈希和 phase；提交成功后才允许 explain 读取。中断重试依据动作和内容身份复用已提交包，未提交文件不算冻结成功；保存失败标记 STORAGE_FAILURE，不继续输出伪装成完整的证据包。冻结后不追加/替换内容，旧文档更新也不改变包内知识依据。

### 5.9 explain：综合解释与结论

输入只能是冻结证据包及其中列出的产物，不具有业务数据库查询、新知识检索或补查任务提交权限。综合输出 `ExplanationResult` 至少包含 explanation_id、bundle_id/hash、direct_answer、conclusions、unresolved_items、limitations 和 output_status。

每项 conclusion 记录 conclusion_id、goal_id、text、level、fact_refs、analysis_step_refs、knowledge_refs、支持与反驳依据、适用范围及限制。允许在已计算事实和可信规则上归纳、比较与组织推断，不要求逐字复述工具结果；新增综合结论必须能展开为包内已有事实与规则的明确关系，不引入新的观测事实、关键前提、门限、统计量或确定性归因。

例如，包内已有各未满足条件的覆盖比例、重叠与剩余样本，解释可综合回答“主要限制条件是什么”；覆盖不同无法公平排序时明确限制，不能仅因某组数字最大就确认主因。相似案例仍只提供参考，不升级为当前根因。OBSERVATION/HYPOTHESIS/CONFIRMED 沿用 本章证据与结论等级契约，每条陈述分别定级。

解释发现遗漏、未解决冲突或无法支持的推断时，限制或删除相应陈述，记录输出侧问题及影响，不改写冻结包、目标状态或停止原因，不重新取证、不返回 loop。即使包中记录 GOALS_VERIFIED，若输出检查发现关键材料不足，也必须在 output_status 标记 PARTIAL/VALIDATION_FAILED 等并披露差异，不能对用户发布无条件完整答案。

结构、数值、引用、范围及结论依据检查失败时，最多两次有界修复，使用同一证据包且计入剩余预算。仍失败或模型不可用时生成确定性部分解释，保留已验证事实、缺口和停止原因；不为了完成文本而扩大推断。`suggested_followup_queries` 只说明补充资料后的验证或新的问题方向，不能将本轮仍可执行的必要任务转交用户而声称完成。

`SIMILAR_CASE_REFERENCE` 表示引用相似案例，不表示当前根因已匹配；特性分析使用 FEATURE_DIAGNOSIS。输出事实与假设分别标记，报告级别不得把所有陈述统一升级。`CONFIRMED` 必须有满足定案要求的外部证据或专家确认记录；专家确认标明身份/来源、时间、原话和依据，不能伪装成由 SRS 自动推导的结论。

### 5.10 report：逐步分析报告

report 输入相同冻结包与通过校验的 ExplanationResult，输出报告身份、版本、bundle/explanation 哈希、正文、图表/表格引用和关联索引。只细化表达与组织证据，不重新判定原因、不提升结论等级、不补查数据。模型不可用时采用确定性模板；不因缺少润色而丢失事实报告。

报告正文包括：直接答案与完成状态；输入文件/数据范围与质量口径；各项目标及分析方法；每步有效分析的发现、数据与规则引用；支持和排除的候选解释；未解释群体、冲突与限制；停止原因和需要补充的信息。记录失败或跳过步骤对结论的影响；重复请求、结构修复等运行细节放到附录/日志，不伪造为新的有效分析。提供可核查的分析依据，不展示模型内部思考文本。

关联索引至少支持 `conclusion_id → goal_id → analysis_step_ids → fact/claim_ids → artifact_id/hash → record_id/source_file/file_row_id`，以及相应 document/rule 引用。聚合结论需保留过滤、公式、分子分母及完整结果；无单行归属时引用完整关系和来源清单，不伪造行号。排除结果、未定项与复用父分析证据同样可回查，派生证据可通过父引用逐级定位。

摘要与详细报告使用同一组 conclusion_id、证据包和统计值；图表与表格注明单位、样本、降采样/预览范围。校验发现无来源数字、缺失关联或结论不一致时，在冻结输入上修复排版/引用或删除不支持内容并标记输出降级；不得静默改成另一份分析。UI 继续简短展示结论，详细报告及证据默认折叠，不新增普通用户的逐步确认负担。

`DiagnosticOutput` 为 report 完成后的最终输出封装：

- `mode`：`SIMILAR_CASE_REFERENCE / FEATURE_DIAGNOSIS / EXPLORATORY_ANALYSIS / DATA_INSUFFICIENT`；
- `conclusion_level`：`OBSERVATION / HYPOTHESIS / CONFIRMED`；
- `similar_case_ids, similarities, differences, unverified_items, condition_results`；
- `claims, reasoning_or_analysis_markdown, suspected_directions, case_asset_refs, chart_artifact_refs`；
- `missing_data_and_limitations, suggested_followup_queries, confirmation_record_ids`。
- `analysis_id, evidence_bundle_id, evidence_bundle_hash, explanation_id, report_id, goal_statuses, stop_reason, output_status`：绑定同一冻结证据包；取证完成状态与解释/报告生成状态分别记录。

### 5.11 连续追问与证据复用

每次用户追问创建新的 analysis_id/action_id，携带原始问题、追加问题、用户纠正、parent_analysis_id/bundle_ref、明确的群体/事件，以及经校验的历史事实、假设状态、缺口和查询账本，再走 `plan → agent loop → explain → report`。有效问题由本轮目标决定，不机械重跑所有旧问题；用户纠正应记录 supersedes 关系，不能把被纠正的假设继续当作前提。

历史文字结论只是可查看的解释，复用依据必须是结构化事实与证据。复用前检查数据指纹、范围/群体、质量和统计口径、字段/规则版本及产物哈希；范围收窄需要重算比例或其他统计时，在新 loop 中执行，不能直接沿用旧数值。版本或规则变化使受影响结论重新评估，保留原分析不变；不同数据集分别分析，不合并成一个证据总体。指代有多个有效候选或范围确实冲突时才澄清，不增加例行确认。

同一动作的重试与新追问分开：重试复用原请求、图版本、累计预算和阶段；冻结后重试 explain/report 不能触发 loop。新追问使用独立预算并显式链接父分析，不冒充原动作恢复，也不自动靠生成新动作绕过预算。已发布报告保持不可变。

证据链为原始快照及行位置 → 标准化 record_id → 业务有效总体 → 观测会话/群体 → 状态段与事件 → 条件结果 → 分子与分母 → 比例及结论。每个派生产物保存父 artifact_id、输入群体、算子/表达式及参数、版本、完整结果位置和哈希。原因分析必须引用已生成的实际未生效群体，不由模型独立重建近似筛选。跨轮引用只能通过显式继承并验证兼容性后纳入本轮证据清单，不能放开任意历史 ID。

比例使用 MetricSpec 绑定单位、分子产物、分母产物、去重键、过滤条件、未知策略和公式。模型仅引用程序计算值。条目、会话、转换事件三种单位不可混用。各原因允许重叠，原因占比不强制相加为100%；无明确优先级不得生成互斥首因。用户级“未生效群体”是否包含混合状态会话、原因如何归属会话，尚待确认；不能静默采用“曾未生效”作为最终分母规则。

### 5.12 默认探索对象与四类表

没有特定任务要求时，指定 ue_uid 就分析该会话全部符合筛选的数据；仅指定 crnti 时，在当前小区和时间范围内列出其对应会话并分别统计，不能把复用 CRNTI 混为同一 UE。不对已明确选择的对象再套 Top-10 限制。

未指定用户时，按当前范围内流控有效且可归属会话的原始记录数选择 Top-10 活跃 ue_uid，记录数相同按 ue_uid 稳定排序。此策略为展示优先级，不是异常严重程度排名；报告覆盖人数与总人数，不能据此声称全体用户正常。特性 B 的原因比例等用户要求的全范围统计不套用此默认 Top-10，必要时全范围聚合、Top-10 仅用于展开明细。

| 表 | 范围与内容 |
| --- | --- |
| 小区 Slot/RB 分布 | 当前显式筛选范围内的均值、异常比与覆盖，不默认放大到用户未选的小区/UE |
| UE/资源均值与波动 | 目标会话的均值、极值、rsrp_std、rsrp_trend_delta、异常/缺测比例 |
| UE/资源/流对比 | 各流独立均值、样本数、流均值之差与异常比 |
| UE/资源/流时序 | event_ts 原始观测变化，表格与折线图切换；明确指定时间桶时记录聚合口径 |

四类表按实际字段可用性生成，不凑齐空表。全量分析指使用全部符合范围的数据计算，不要求把所有明细传给模型或一次渲染；分页/图表抽样必须注明。

### 5.13 业务一致性与输出校验

只对当前任务的实际必需条件设门禁。描述性均值和异常比无需异常门限即可展示；缺测、少量样本或覆盖差异如实提示，不因可疑比例高而阻断出表。

特性规则要求的字段缺失时，其条件为 UNKNOWN，不影响其他可判断条件；只满足必要条件不能宣布特性本应生效。需要具体时间关联而规则缺失时，不作该关联判断。数据为空时仍可说明文档和案例参考，但不能编造当前实测推断。

无 CRNTI 的截断行无法归属某 UE 时，只报告文件/块级清洗情况。不同分析的分母必须明确，各条件观察覆盖不同不能隐去。

覆盖按业务目标核账，例如实际生效比例、未生效条件分布、变化事件解释、原因占未生效群体/总体的比例，不能以字段查到或均值算出判为全部完成。每项目标保存 VERIFIED、INSUFFICIENT、FAILED 或 NOT_RUN 状态及证据引用。v3.4 在 loop 每轮及出口依据新证据重新核账，状态可升级或降级，记录理由与依据；冻结后不得由解释文字改写目标状态。

发布前检查：N_on+N_off+N_unknown=N；原因集合属于对应未生效集合且后者属于总体；分子分母单位、范围、版本和去重键一致；五条件 AND 的真数量不得大于任一必要条件的真数量；预期真/假/未知数量守恒；实际与预期交叉统计边际一致；比例可从完整结果复算。完整 activation_logic 同样检查与必要/阻断条件的冲突，不能只验证条件 ID 合法。预览、Top-N 或抽样不能冒充全范围计算。

失败仅阻断依赖该证据的结论，已核验事实可发布；保留失败位置和未完成目标。校验引用与数值不等于语义因果证明，不承诺消除全部模型幻觉。模型不可用时，确定性报告仍应给出已验证的 SPF 业务结论、范围、分母、未知项及限制，而非只有质量摘要。

prompt 独立版本管理，将任务职责/输出 Schema、领域约束、结构化上下文、允许证据清单和校验反馈分离。引用材料作为数据处理；解释读取完整目标级汇总、MetricSpec、状态和限制，不只读取表格预览。结构修复反馈包含有界的错误输出片段和定位，不增加新的分析事实。

## 6. 工作台、请求与数据管理

### 6.1 三栏布局与默认交互

左栏提供历史诊断会话、目录加载、已有数据管理、问题上下文、流水线状态及基线。会话列表显示标题、数据版本、最后访问时间与完整/部分加载状态；筛选支持 `logicCellId / ue_uid / crnti / 时间范围 / resourceId`，`phyCellInst` 可作辅助条件。基线展示流控有效记录、流控剔除率、可疑行数和缺测行数，可展开逐文件/块明细；结构和解析错误与流控问题分别呈现。

中栏接收问题、自然语言追问和快捷下钻，并提供“提炼案例草稿”。点击“开始分析”后，解析问题并结合当前界面范围生成固定 AnalysisRequest，直接执行；`execution_mode=AUTO_FROM_QUESTION`，不伪记为手动确认。编辑问题或刷新界面不触发执行。只有文本/界面冲突、多对象歧义、未解析时间等使范围无法唯一确定时，才显示修正入口。

中栏用一句由实际请求生成的“查询逻辑”说明对象、时间范围、指标、业务有效口径及适用的父群体、同侪或时序分段规则。执行中只显示简短状态；节点、任务、耗时及证据详情保留在日志和右栏。预期查询行为不能写成已有结论，不展示模型内部思考。输出区分观察、假设、确认和相似案例参考；无证据、部分缺失、技术错误分别提示。历史回答保留原范围、正文及未完成提示，范围、引用和校验详情默认折叠。

右栏通过“选择画布内容”切换统计与图表、查询证据、特性条件和案例审核。焦点使用经过版本验证的可控组件，不能假定设置 `active_right_tab` 就能控制 `st.tabs`。主结论与详细报告采用同一组结论和证据身份；详细报告按第 5 章关联分析步骤、数据与结论。

### 6.2 问题语义、对象与不可变请求

字段语义目录支持 canonical_field、中文名、提问别名、已确认含义、单位、缩放和出处；CSV 输入别名与提问别名分别管理。TA 候选映射到 `taValue`，须检查实际字段可用且语义无冲突；单位/缩放未知时显示原始数值，不换算时间或距离。映射参与请求版本绑定；新指标通过确认后的配置接入，不为每个指标硬编码查询或仅靠模型猜字段。

“1911这个用户”“CRNTI为1911”等明确表达解析为 `crnti=1911`；孤立数字、多字段候选或不明确指代进入修正。十进制与显式十六进制保留原表达及转换结果，并检查取值范围。文本未指定的小区和时间继承界面范围；明确冲突展示两个来源，不静默覆盖或取交集。相对时间缺少基准、时区不明或日期不完整时不能补造时间。当前时间解析不覆盖任意自然语言日期，无法解析时由用户填写完整时间。

CRNTI 不是全局永久用户标识。同一 CRNTI 跨小区或多个 `ue_uid` 时分别分析，在请求摘要/修正界面显示小区和观测会话数量；不能拼成一条用户轨迹。数据集、时间、对象不匹配时报告零样本，不扩大范围寻找结果。

固定 AnalysisRequest 保存原问题、解析 revision_id、解析器版本、字段映射版本/哈希、数据集 ID/指纹、Scope、指标与真实字段、单位/缩放、质量与统计口径、分组、图形规格、未决项、execution_mode、生成时间及 request_hash；手动修正另记录确认时间和来源。基础统计、计划、补查、循环、图表与报告共享同一请求，模型不得重新解释用户编号。用户修改范围或语义后生成新请求/动作，旧请求不受后续界面状态覆盖；重试复用原请求。范围内常规补查无需反复确认，确需扩大范围或引入未确认语义时展示差异并由用户决定新的请求。

意图识别采用统一 `question-intents.v1`，覆盖趋势、变化、随时间、走势、波动、切换及局部否定表达；请求中的 trend 是执行与展示依据。“突变”不构成异常门限或因果判据。旧直接 API 与高级查询路径保持兼容，但仍受固定数据、范围、质量与证据约束；追问绑定父分析的明确群体/事件。

### 6.3 本次分析的用户身份说明

用户明确陈述“所有CRNTI实际都是一个用户”时，可直接绑定为本次请求的 `subject_assertion`：类型 `USER_ASSERTED_SINGLE_SUBJECT`，source 为 `EXPLICIT_USER_STATEMENT`，包含用户原句、固定 Scope 和数据指纹，并参与请求哈希。无需再次勾选；疑问、否定或假设表达不自动转为身份事实。存在冲突时仍可在修正入口显式确认。

声明只改变本次分析视角，不改写 CRNTI、ue_uid 或会话划分，不自动清除已有 CRNTI 筛选，父群体约束继续生效。时序保留原始 crntis 和 record_ids，各小区/接入会话分别展示，段间不连线，不推断未观测区间的轨迹或切换时刻。报告明确身份来自用户声明，不能说成数据证明。新分析不默认继承该声明；范围变化时需有对应的新陈述或明确确认。

### 6.4 数据命名、重载与删除

加载 CSV 可输入 1～100 字符数据名称，空名称采用输入文件/目录名加时间。名称存入 `registry_datasets`，以绝对数据目录为键；不写入不可变 input_manifest，不改变 dataset_id、fingerprint 或证据哈希。同名数据用版本短码区分；未命名版本可直接改名，无须重载，新会话默认采用数据名称作为标题。

已有数据管理支持创建会话、改名、重新加载和删除。重新加载可指定新名称及输入契约；默认沿用父版本编码、日期格式及固定字段别名，应用当前清洗规则和字段语义/小区参数策略。更新 CSV 别名需明确选择对应输入契约。

重载只读取父版本清单中的 CSV 快照，逐个验证 SHA-256，不读取原目录后续新增或修改的文件；支持任意文件名的显式单 CSV 加载版本。快照缺失、内容变化或复制失败时终止，不静默丢弃输入；无快照时需重新提供原始文件。父版本操作期间持有生命周期锁。成功后创建新 dataset_id、独立数据库，记录 `reload_of` 父身份/指纹及 `VERIFIED_SNAPSHOT` 来源，自动选择新版本并建立新会话；父版本及报告保持不变。全部解析失败不切换，部分成功明确展示清单。重载执行完整 ETL，需相应时间和新数据库/快照空间；失败构建保留在隐藏构建目录用于诊断。

删除前显示关联会话数量并要求勾选确认。删除仅覆盖本工作区 datasets 直属版本目录中的数据库、输入快照及该版本文件，不删除外部 CSV，也不删除历史会话、报告和已生成证据。历史会话保留显示名并标记数据不可用，禁止新查询或原始行回查；需重新加载才可恢复分析。

创建会话、诊断、改名和删除使用同一数据版本生命周期锁，分析占用时拒绝删除。删除前再次核对 dataset_id 与直属管理目录；根目录、外部目录、隐藏构建目录及含符号链接/Windows 重解析点的目录拒绝递归删除。失败明确提示，不宣称成功；外部程序占用须先释放。文件系统删除不具备跨文件事务保证。

### 6.5 证据画布、图文比较与案例审核

查询卡片默认展示由已保存结构化计划生成的中文说明，按产物自身 query_plan/plan_id 匹配步骤，避免不同计划复用步骤号时错配。语义不确定的探索查询只说明结果范围，不让模型猜测 SQL 含义。SQL、参数、计划编辑及探索入口由“显示技术详情与高级查询”展开；表格同时保留范围、样本数、单位和质量口径。

业务表只含准入数据；原始/质量审计视图中的可疑行整行高亮，缺测和解析错误分别呈现。支持明确控件排序/筛选及 Plotly 折线图、热力图切换，图轴、单位、桶宽和数据版本可见。修改 SQL 生成新 artifact 并记录来源，不覆盖报告已引用结果。置顶表示用户认为重要，不提升为定案证据。

一致性校验通过的特性结果可显示实际生效、未生效、未知条目占比，分母为当前范围全部业务有效记录；零分母或校验失败不绘制比例。条件不满足图同时显示相对未生效条目及全部有效条目的比例、分子、分母；原因允许重叠，不称为用户级原因比例或已确认根因。条件表使用中文描述及满足/不满足/未知计数，完整结构留在详情。

案例图文与当前实测图可并排展示，明确区分“历史参考/当前实测/原理示意”。图片缺失或解码失败显示原因，纯文本模型仍允许人工看图。审核表单支持 YAML 元数据、Markdown 正文编辑/预览、相似现象、分析依据、出处、适用条件、根因状态、图表导出、截图及 caption；查询和结构化判据可选。提交前校验案例包及附带查询，按知识资产契约保存；分别显示文件保存与索引同步状态。

### 6.6 时序、完整结果与预览规则

趋势以时间折线图为默认展示，仅明确状态保持语义时使用阶梯线。按 event_ts 排序、按 logicCellId/ue_uid 分组；缺测、异常、质量排除与会话断点分段，同时间冲突不以 record_id 伪造先后。采样稀疏和缺口须注明，连线不能证明间隔内变化；不自动插值、平滑或推断移动，单位未知不输出物理含义结论。

完整时序在最终导出时显式按 logicCellId/ue_uid/event_ts 排序，绘图再按对象、segment 和时间稳定排序；窗口 ORDER BY 不替代导出排序。同时间质量阻断点 value 为 NULL，不计入有效点。完整证据保存原始 record_ids，统计提供有效样本、首末观测/有效值、极值与可核验变化，不凭预览判断全范围上升或下降。当前实现按原始观测点展示，不自动生成升降分类或聚合桶；若以后启用时间桶，桶宽和聚合口径须绑定请求与图例。

表格结果不超过 100 行时完整显示；超过时最多选择 25 个稳定哈希分组，每组保留首末观测和两个哈希样例，总数不超过 100 行。分组键取可用的小区、会话、状态，preview_policy 记录方法与覆盖；无法低成本得知组总数时标记未知。覆盖样例不是无偏随机样本，不用于估计总体比例。

图形先选对象，支持会话编号筛选，每次最多列出 500 个匹配对象，超过时提示收窄筛选。每对象不超过 2000 点时完整绘制，超过时按 segment 内有序观测分桶保留首末及极值；状态图复用 segment_index。段数超额时记录遗漏段数。降采样不改变完整 Parquet 或统计值，不能跨缺口和会话边界连线。

会话状态图读取通过哈希校验的 SESSION_OBSERVATIONS，显示未知、冲突、断点及预览上限，完整证据可下载；观测点不连成连续状态。趋势/变化/切换/时序问题在证据存在时默认展示对应图，缺证据明确提示。时序报告正文最多展开 20 个会话，画布保留完整分组统计表。

## 7. 执行服务、预算与持久化

### 7.1 查询执行边界

模型 SQL、模板 SQL 和手工 SQL 统一进入受控执行器：

1. 只接受单条只读查询及受控 CTE；表、字段、函数白名单化，拒绝写入、DDL、ATTACH、扩展安装/加载及外部读取。
2. 参数值绑定，标识符来自真实 schema；仅暴露当前 dataset_id 与用户筛选范围内的受控关系。
3. ETL 与业务查询连接权限分离，关闭查询连接不需要的外部访问并固定配置。
4. EXPLAIN 检查语法、绑定与计划，不替代安全和业务语义检查。
5. 限制时间、内存、结果规模、并发和重试；完整统计使用全范围计算，预览截断不得改变统计口径。
6. 保存计划、SQL、参数、实际范围、来源文件、结果哈希及错误，报告范围取执行元数据，不由模型自报。
7. 自动条件判断仅使用支持的算子及已校验结果；自由探索的能力边界明确，执行成功不等于根因确认。

QueryPlan 在执行前后续关系推导中检查字段来源、操作必填参数、聚合输出冲突、单位与时间范围。结构/绑定错误最多修复两次；真实缺字段保留 UNKNOWN 或缺口，不盲目重试。仅使用 stream 来源的计划展开流表；补充字段按需过滤物化，不默认展开全部宽列。DuckDB 使用短溢写目录以避免 Windows 深路径问题。

### 7.2 模型服务配置与输出契约

模型由用户在 `.env` 配置，提供 `.env.example`，不预置真实密钥或擅自选择模型。LLM 与 embedding 分别配置 PROVIDER、BASE_URL、MODEL、API_KEY、TIMEOUT_SECONDS、MAX_RETRIES；具体键名见示例。服务默认超时 60 秒、最多重试 2 次，受分析剩余预算约束；无密钥本地服务按适配器能力处理，不强制填写密钥。

`LLM_SUPPORTS_VISION` 默认 false；独立视觉服务使用 VISION_PROVIDER、BASE_URL、MODEL、API_KEY、TIMEOUT_SECONDS、MAX_RETRIES 对应配置。适配器实际验证能力，不凭模型名猜测；不可用时退回文本、caption 和人工查看。图像来自本地资产并转换为服务支持载荷；新的视觉取证仅允许在 agent loop 内，explain/report 不开展视觉补查。

`.env` 不入库，日志、错误信息、模型上下文和 UI 不泄露密钥。保存非敏感模型标识及版本；embedding 变更后重建兼容索引，不混用不同向量空间。

模型输出仅接受原始 JSON 对象或完整单个 JSON Markdown 代码块，去壳后严格按契约解析。`artifact:` 或 `artifact：` 只作引用包装，规范化后仍须完整匹配本轮允许的 ID，校验行键、单位、数值；表号仅作显示别名，不猜截短 ID。规范化及拒绝位置记入诊断。修复反馈包含具体位置、允许列/单位及空列表降级方法，不能靠重复催促形成“成功”。

Prompt 分别管理职责/Schema、领域规则、结构化上下文、允许证据与校验反馈；检索内容作为业务材料，不作为系统指令。规划、验证、解释各用途可追踪，同一固定动作使用对应版本。

### 7.3 完整证据、摘要与模型上下文

通用 FILTER、GROUP_AGG、CONDITION_COUNT、COMPARE、TIME_SLICE、TEMPORAL_RELATE 及指标时序生成 `evidence-summary.v1`。摘要在已执行关系上计算，包含字段类型、列单位、来源关系、父产物、分组/步骤、全量计数/范围/缺口及 limitations；独立 `.summary.json` 的内容哈希与原 Parquet 哈希绑定并在读取时核验。分组摘要最多 200 组，截短显式标记，完整结果保留。

摘要及确定性报告遵循算子实际语义：FILTER/TIME_SLICE 的分母为直接来源关系行数；CONDITION_COUNT 分开 True/False/Unknown；COMPARE 为组内 max-min；TEMPORAL_RELATE 表示对象键和前向窗口内是否存在观测，不是配对数量或根因。GROUP_AGG 不平均各组均值冒充总体均值。模型缺失或失败时，仍能以这些摘要生成有依据的结果说明。

数值可通过 `artifact_id + row_key.fact_id + column=value` 引用摘要事实，也可通过唯一原始行键及 column_units 引用完整产物；核验双哈希、单位与数值。单位依据源字段及算子传播，count 使用 count、差值继承源单位；未经确认或仍需缩放的字段使用 raw，不随意标成 dB。无摘要历史产物按其兼容契约读取。

模型 evidence 使用列表入口，每个产物提供完整摘要及最多 6 个覆盖样例；证据上下文默认 32000 字符，完整解释主上下文默认 64000 字符。这是字符限制，不是精确 token 上限，服务商限制仍由适配器处理。消除重复大型内容，显式记录省略字段，省略不代表缺失；冻结包保留完整引用和目标级摘要，explain/report 可按目标读取包内材料，但不能因上下文截短重新查询。样例不能替代全范围结论。

报告与证据详情显示计划限制；模型计划自述的 limitation 与经核验缺口分别标注。专用特性、会话及验证结果与通用摘要共用来源链，不因生成通用报告丢弃业务含义。

### 7.4 动作状态、事务恢复与证据冻结

`st.session_state` 管理界面选择及已提交操作 ID；LangGraph 管理诊断进度；持久化存储管理证据和案例。不得无条件互相覆盖。session_id、action_id 使用完整 UUID，消息/证据按 ID 去重；Streamlit 重跑不能重复 SQL、动作或入库。

`session_registry.sqlite` 保存会话、数据指纹/路径、manifest 和 schema 版本。关键动作由 action_progress 按 session_id/action_id/request_hash/revision 绑定，先事务提交再更新内存；动作锁与修订检查防止并发覆盖。SqliteSaver 检查点使用 `session_id:action_id` 隔离，检查点表独立命名；内存保存器仅用于开发。状态只保存计划、目标、预算和产物引用/小摘要，完整表、图、图片不复制到每轮状态中。

恢复顺序为会话注册 → 固定数据版本和 ETL 状态 → 数据库 → 对应图版本检查点与筛选 → 证据哈希校验与分页加载。恢复不重新清洗、不读取源目录后续变化，不重置累计预算。完成任务通过账本复用；中断步骤标为可重试，不伪造成功。

目标架构持久化 graph_version、计划修订、任务账本、预算、证据包冻结引用及 explain/report 提交状态。冻结前保存并校验产物与包，事务提交包引用/哈希/phase 后才允许解释阶段读取。冻结后的输出重试只复用原包，不重新取证；输出失败与取证已完成分别记录。历史最终结果只读兼容，未完成的不同图版本动作使用对应兼容执行器恢复，或显式新建分析，不能静默迁移检查点。

SQLITE_BUSY/LOCKED 最多三次短重试；其他存储异常报告 STORAGE_FAILURE。预算预留、任务队列、产物引用及冻结交接不能静默丢失；保存失败停止并保留已落盘证据，不宣称成功。阶段耗时属于可降级遥测，保存失败不得遮蔽原业务异常。SQLite 不能兜底持续锁、权限或磁盘故障。

动作事务状态为恢复依据，不生成 progress_*.json。兼容已有 JSON 时，按原动作文件名和 registry 请求绑定迁移并核验产物哈希，损坏拒绝恢复，预算不归零。最终结果以不可变文件及注册表哈希保存。

图表、结果或证据缺失/损坏时保留可用聊天和独立证据，隔离受影响结论并明确不完整，不自动新查并冒充旧证据。需要重建时须有原固定数据并以明确的新分析/产物记录来源，不改变冻结包。数据版本删除后的历史报告仍可看，依赖数据库的查询和原始行回查不可执行。

### 7.5 全链路预算与运行参数

目标架构从 plan 入口计量总预算，覆盖计划、检索、工具调度、查询、模型重试、解释及报告。按阶段记录模型/工具/查询次数和耗时；为 explain/report 预留可配置额度，loop 不得侵占。子预算取所属阶段与总剩余额度的较小值，不能延长父 deadline。累计消耗随动作持久化，恢复和重试不清零；只有显式新增预算才能扩大额度。

以下为当前实现已有的默认参数及边界。全工具循环的统一计数和输出阶段预留仍按第 8 章实施，不能把现行追加查询次数称为整次分析的 SQL 总数。

| 参数/资源 | 当前默认与范围 | 计量说明 |
| --- | --- | --- |
| SRS_ANALYSIS_BUDGET_SECONDS | 120 秒 | 整体调度预算；目标架构统一从 plan 入口起算 |
| SRS_VERIFY_MAX_QUERIES | 12，范围 1～12 | 现行记录条件循环追加 QueryPlan；基础统计、特性、状态及目录查询另有日志 |
| SRS_VERIFY_MAX_PLANNER_CALLS | 6，范围 1～6 | 验证规划调用及其契约修复计入累计额度 |
| SRS_ANALYSIS_MAX_MODEL_CALLS | 12，范围 1～24 | 规划、验证规划和解释共享；目标架构还纳入报告模型调用 |
| SRS_RETRIEVAL_SEMANTIC_SECONDS | 10 秒，范围 0～60 | 可选语义检索子预算；取 loop 可用预算及总剩余时间约束 |
| DuckDB 查询资源 | 单连接操作最多 30 秒、512 MB、2 线程 | 不等于进程总 RSS 限额，不以延长超时替代查询优化 |

语义检索超时保留 BM25 并标明降级，不能让向量冷缓存占满后续预算。嵌套服务 HTTP 重试与空输出恢复受当前调用和剩余总预算约束；本地索引操作在阶段边界检查预算，不能宣称任意本地操作均可即时中断。

次数用满后仍可检查是否确无后续任务，新增查询继续受限；已落盘成功结果可在恢复时评估，不重复扣成一次新扫描。预算不足的必要任务保留 BUDGET_EXHAUSTED，不冒充 DATA_INSUFFICIENT。无模型额度时 explain/report 用确定性整理和最小状态报告，不发起新模型调用或业务分析；USER_STOPPED 后不启动解释模型。

`budget_exhaustion` 保留 TIME、QUERY_COUNT、PLANNER_CALLS、MODEL_CALLS、MODEL_CALL_OR_SERVICE、UPSTREAM_BACKFILL 等明确维度、剩余秒数、次数及待办。`budget_diagnostics` 保存总上限、累计墙钟、剩余预算、首次进入 loop 的余量、模型次数、检索子预算及阶段状态/累计耗时。重试等待消耗墙钟预算，节点耗时范围不一定相加等于总耗时；不得仅凭停止枚举判断性能根因。

### 7.6 日志、性能与输出校验诊断

加载、基础统计、范围准备、查询、模型调用、artifact_export、evidence_summary 和完整诊断的耗时及进程驻留内存写入 UTF-8 `workspace_storage/logs/performance.txt`，每行 JSON，阶段以 trace_id 关联。记录成功或异常类型，不写密钥、提示词、SQL 或原始业务记录。查询计划/SQL 的业务审计保存在受控证据元数据中，不混入普通性能日志。

每 100 ms 采样的最大值为整个进程采样峰值，不是单查询独占峰值；导入时累计补充行数，不为观测额外扫描全表。日志故障不影响业务结果，归档与字段说明见 `docs/PERFORMANCE_LOG.md`。模型调用可记录用途、HTTP 状态、请求 ID、attempt、耗时、finish_reason、内容长度和 token usage 等非业务正文信息，按适配器实际能力记录。

解释/计划校验拒绝记录错误码、位置、尝试次数，持久化到动作的 validation_errors 与性能日志；反馈模型具体错误和允许修复范围。最终提示展示失败原因，未验证文本不发布为事实。日志不保存原始模型响应或内部推理正文，不保证能够还原修复前的全部输出。阶段状态与累计耗时写入 stage_status/stage_seconds，失败动作保留以便绑定原请求重试。

### 7.7 异常提示与降级输出

| 异常 | 系统行为与用户提示 |
| --- | --- |
| LLM 未配置 | 提示填写 .env；数据加载与确定性统计、事实报告仍可使用 |
| 鉴权失败 | 提示检查密钥及地址，不回显密钥，不反复重试鉴权错误 |
| 超时、限流、服务不可用 | 有界重试，保留清洗数据和成功结果，说明模型分析未完成 |
| embedding 未配置/不可用 | 降级 BM25，声明语义检索未完成，不声称遍历全部案例 |
| 模型输出结构错误或证据不符 | 最多两次修复，仍失败保留事实表及失败原因；冻结后修复不触发 loop |
| 输入部分失败 | 列明成功文件、记录/日期覆盖、失败来源及原因，结果标记部分加载 |
| 可疑或非法流数记录 | 整行退出业务统计，保留质量审计并说明有效样本数 |
| 分组覆盖不同 | 展示各组独立统计及覆盖差异，不暗中丢弃或配对 |
| 图像缺失、解码或导出失败 | 保留正文和数据表，说明受影响图片，不伪称已完成视觉比较 |
| 视觉服务不可用 | 使用文本与既有 caption，明确本轮未做视觉比对 |
| 数据/证据不可用或存储失败 | 恢复可用部分，标记缺失/故障，不自动生成新证据冒充旧结果 |
| 无预置模板 | 在可信文档与真实 schema 上组合通用算子；必要语义不明时说明缺口 |
| 预算耗尽、无进展或工具失败 | 记录具体维度、未完成目标和已有事实，不能改写为没有异常或数据不足 |

失败不清空独立成功结果。分析完成度、证据包状态、解释生成状态和报告生成状态分别呈现；解释或报告失败不自动推翻已有事实，取证未完成也不能因报告生成成功而升级为问题闭环。

## 8. 实现覆盖、实施方案与验收

### 8.1 当前实现覆盖与待实现能力

本表为设计基线的实现边界，不代表生产验收。代码已具备的基础能力可以复用；未列为完成的新架构功能仍需实施和验证。

| 能力 | 当前覆盖 | 实施工作或边界 |
| --- | --- | --- |
| 数据底座与质量 | 固定快照、单文件流控、补充字段、实际流数、cell-continuity.v2、数据版本管理已有实现 | 保持业务规则；现场规模和真实样例持续验证 |
| 知识与案例 | 字段/特性文档、图文案例、检索、审核发布已有模块 | 视觉与 embedding 按配置可选；检索不保证穷尽所有材料 |
| 查询与证据 | 通用算子、受控 SQL、完整产物、evidence-summary.v1、来源链和事务进度已有实现 | 复用校验与存储，补齐冻结证据包及结论关联索引 |
| 请求与交互 | 固定 AnalysisRequest、默认直接执行、范围冲突修正、群体/事件追问已有实现 | 不重引入普通问题二次确认；仍不能任意推断时间或单位 |
| 动态专家循环 | expert.v1 已接入动态知识检索、通用 QueryPlan、任务账本及现有条件验证；追加查询后重算已有指标目标 | 目标识别仍复用字段/特性机制，状态与同侪等采用确定性准备步骤；任意问题的完备语义判据不作保证 |
| 单向解释与报告 | expert.v1 四节点主图、冻结证据包、解释/报告 JSON、关联下载与冻结后恢复已实现 | 模型输出受现有规则和数值证据约束，报告以确定性结果为主体；详细实现与限制见 docs/EXPERT_LOOP_IMPLEMENTATION_PLAN.md |
| 自主分析效果 | 已有本地与模拟数据测试基础 | 复杂模型输出、真实测量与现场规模仍需按本章评估，不承诺生产诊断准确率 |

### 8.2 实施步骤与交付边界

| 步骤 | 主要改动 | 完成条件 |
| --- | --- | --- |
| 1. 固定契约 | 定义 AnalysisPlan、工具任务、LoopState、AnalysisEvidenceBundle、ExplanationResult、Report 及版本 | 每阶段输入输出、不可变引用、错误状态可独立校验 |
| 2. 封装分析能力 | 将检索、字段盘点、通用查询、特性/状态/比较及视觉取证封装为受控工具 | 不改变已确认数据规则；统一权限、范围、来源和预算检查 |
| 3. 建立循环 | plan 形成初始目标，循环按证据调整行动，逐轮重新评估目标/假设，出口核验并冻结 | 简单问题不过度查询，复杂问题可按依赖补证，停止原因可核查 |
| 4. 单向输出 | explain 读取冻结包综合结论，report 关联步骤/数据/规则，输出校验和确定性降级 | 输出阶段无新取证权限与返回分析的边，摘要和报告一致 |
| 5. 追问与恢复 | 绑定父包、原问题/补充问题/纠正、兼容性检查、事务交接和阶段重试 | 新追问新动作；冻结后重试仅恢复输出，不改写旧证据或重置预算 |
| 6. 集成验收 | 数据/知识回归、问题集、同义改写、故障与恢复、规模和成本评估 | 满足确定性门禁，记录自动化与现场验证差异后再发布 |

复用现有模块与成功证据，不并行维护另一套清洗或统计实现。新图、状态与提示词独立版本化，旧最终结果只读兼容；未完成动作使用其绑定的兼容图恢复，或显式另建分析。具体旧图、阶段交付与测试结果留在设计变更记录及关联交付文档，不作为当前流程的另一条可选规范。

### 8.3 确定性质量门禁

| 门禁 | 强制要求 |
| --- | --- |
| 文件与块边界 | 单文件、原始记录顺序、连续块；不跨文件截断 |
| 完整性 | 完整块全留；不完整块才截断；合法可空字段不构成流控证据 |
| 数据充分性 | 首轮、追问和手动查询解释均按当前任务与范围检查 |
| 质量统计 | 各组仅纳入业务准入记录中的有限值；异常/缺测/解析错误单列审计 |
| 案例参考 | 相似即可提出；列明相似、差异与未验证项，不要求全部判据通过 |
| 数字证据 | 校验真实数值、单位、范围、结果版本及派生计算 |
| SQL | 统一受控只读执行与资源限制 |
| 案例回流 | 人工审核、确认来源、可选查询回放、版本化与索引恢复 |
| 特性分析 | 文档出处、必要/充分条件、三值判断、分母与重叠原因均须明确 |
| 数据隔离 | 每个查询绑定单一数据集，不自动合并独立数据 |
| 图文证据 | 历史/当前/示意图区分，表图哈希与版本绑定，caption 不替代精确数值 |
| 案例单一源 | Markdown + 图片为权威源，SQLite/向量索引不独立修改正文 |
| 恢复一致性 | 固定数据版本、已完成 ETL、检查点与证据引用一致才复用 |
| 循环出口 | 每轮重新核账业务目标，区分完成、资料受限和执行未完成；任务为空不等于闭环 |
| 单向交接 | 证据包冻结后 explain/report 不查询、不追加证据、不返回 loop；新问题生成新动作 |
| 报告溯源 | 结论关联分析步骤、证据与规则；摘要和详细报告绑定相同证据包及结论版本 |

### 8.4 数据、知识与恢复验收矩阵

| 场景 | 预期 |
| --- | --- |
| 完整多行块 / 完整单行块 | 全部保留，末行不删除 |
| 不完整块含尾部空 CRNTI | 按最后非空 CRNTI 边界截断，边界行也删除 |
| 不完整单行块 / CRNTI 全空块 | 全块排除 |
| 前缀存在不完整行 | 该行不能被保留 |
| 同文件计数序列 `10,10,11,10` | 三个连续块 |
| 两文件同计数值 | 分别处理，不串块 |
| 可选测量为空但结构完整 | 不触发流控，进入缺测统计 |
| 测量越界但 CRNTI 非空 | 不触发流控截断；整行退出业务统计，保留质量审计 |
| 同行缺测与可疑 | 两种计数均计入，标签优先可疑 |
| 无某指标有效样本 | 均值/分位数 NULL，有效计数为 0 |
| 两天跨午夜、文件顺序颠倒 | 由 uwData + uwTime 得到一致时间 |
| 日期无法解析但 CRNTI 非空 | 报告时间问题，不触发块流控或误删其他行 |
| 会话间隔等于 / 大于 180 秒 | 分别不切分 / 切分；不宣称 UE 身份改变 |
| 时间或 UE 筛选后的汇总 | 从明细重算，不能复用不适用的全周期汇总 |
| 流均值为 1、2、4、8 dB | 均值之差为 7 dB；缺流时附覆盖限制 |
| 预览未包含决定性记录 | 验证仍依据完整计算结果 |
| 缺字段 / 查询失败 | INSUFFICIENT 或技术错误，不写成证伪 |
| 报告数字或单位与表格不符 | 阻止该结论输出并要求修复 |
| SQL 重跑、数据重载 | 新证据/数据版本，旧引用不可被覆盖 |
| 审核保存后索引失败 | 保留权威记录并可重试，不重复创建案例 |
| 可疑比例不同的多组 | 统一整行排除可疑记录，不按比例切换口径；逐组输出审计异常比 |
| 非空 CRNTI 数值解析失败 | 单独报告解析错误，存在性仍为真，不触发流控 |
| 仅尾部测量缺失、CRNTI 全非空 | 全块流控有效，不增加其他完整性条件 |
| 各流样本时间不同、无共同记录 | 直接展示各流独立均值、差值与异常比，说明覆盖限制，不强制配对 |
| 完全相同文件位于不同路径 | 仅计算一份，保留重复路径清单 |
| 部分文件加载失败 | 分析成功数据，报告成功文件和失败原因 |
| 源文件在快照期间变化 | 重试或报告失败，不发布不一致快照 |
| 缺测、非法数字和非有限值 | 保留不同原因，后两类不混入缺测计数 |
| 模型配置缺失或 API 调用失败 | 展示明确异常提示，保留已完成的数据与查询结果 |
| 非空 CRNTI 但日期或身份解析失败 | 不触发流控，保留质量明细；无法归属会话时不合并为空 UE |
| 相似案例没有结构化判据 | 正常展示相似点、差异、未验证项与排查过程 |
| 没有预置特性查询模板 | 从文档和字段映射生成计划并编译支持的查询 |
| 必要条件满足但 B=0 | 不直接判为软件异常；检查充分条件和其他触发要求 |
| 特性条件或 B 缺失 | 标记 UNKNOWN，不当作条件不满足或 B=0 |
| 两个条件同时不满足 | 各自计数并展示重叠，不将独立比例相加归因 |
| 文档未明确条件优先级 | 不生成互斥的首因归类 |
| 普通比较与特性时序规则 | 普通比较不强制配对；特性明文要求的关联独立执行 |
| 不同数据集 | 查询与证据隔离，不联合求均值 |
| Markdown 案例无图或无判据 | 仍可检索和展示参考，不要求补齐才启用 |
| 修改 Markdown、caption 或图片 | 只更新受影响索引，报告仍引用原版本 |
| 图片缺失/越界路径/无法解码 | 明确报错或降级，不读取任意本地文件 |
| 纯文本模型 | 使用说明和表格，前端仍展示原图，不声称模型看过图 |
| 图像与表格数字冲突 | 标记冲突，精确数值以已保存且校验通过的查询结果为准 |
| 案例保存后索引失败或中途退出 | 文件包保持有效，同步任务可恢复重试 |
| 恢复昨天的固定数据集 | 不重读原始 CSV，不重复 ETL，恢复原会话与证据 |
| 未指定用户的默认探索 | Top-10 活跃会话展开，标明并非异常排名 |
| 特性原因全范围查询 | 不套用默认 Top-10 限制 |
| 多天同一时分秒的时序图 | 由 event_ts 区分日期，不合并两天数据 |
| 图表预览降采样 | 明示粒度，完整统计不被预览改变 |

模拟数据负责精确边界测试；真实脱敏数据负责确认“完整/截断”输入契约、清洗误删情况及专家诊断一致性。未完成真实样例验证前，不宣称生产级诊断准确率。

### 8.5 特性原因分析验收主线

以“SPF生效比例是多少，未生效原因是什么，占未生效用户和总用户的占比是多少”为例：

1. 锁定全部业务有效条目总体，核验实际1/0/未知及生效比例，不将未知并入未生效。
2. 创建实际未生效群体，逐条验证已确认五条件，分别核算各条件失败数量、重叠及相对未生效条目/总体的占比，绑定明确分子分母。
3. 对“已知条件失败”的记录说明规则层面解释；对“条件缺失”的记录进一步核实是原字段不存在、映射问题、解析无效还是本范围缺测，列明缺失项。
4. 对“五条件满足但flag=0”群体复核是否同条记录、同行门限、版本和质量口径一致，检查可用的相关阶段标志及已有明确判据。不能忽略残余群体，也不能仅凭不一致断定算法故障。
5. 问题需要解释会话变化时，在单个观测会话内核验变化前后条件；未知、冲突和已确认断点继续遵守第 3 章的会话与状态契约，不自行跨会话、扩张时间窗或开展同侪比较。
6. 对codeUsrSpfSw=0等进一步解释，只在已有字段和可信规则能区分原因时继续细分；否则明确还缺终端形态、服务用户或互斥判据中的哪类证据，不默认选择一个原因。
7. 本期不启用用户级原因占比。自动循环不得暗定分母或把条目比例改名为用户比例；回答明确该项目待口径确认，不影响已授权目标继续验证。

### 8.6 专家循环与效果评估

| 场景 | 验收要求 |
| --- | --- |
| 单向拓扑和权限 | 顶层为 plan、agent loop、explain、report 四个业务阶段；explain/report 无返回 loop 的边，无新查询/检索工具权限 |
| 简单统计与复杂原因 | 均值问题适度完成；特性、比较、时序、复合问题可动态组合工具，不受已有 feature 群体或 CONDITION_COUNT 限制 |
| 动态完成状态 | 补充证据能完成原缺口，反证能降级已有判断；空队列、空模型任务或字段齐全不直接等于问题闭环 |
| 候选原因与残余群体 | 支持证据、关键反证及未解释样本均有去向；案例相似不自动确认根因，必要条件不误当充分条件 |
| 资料受限与执行失败 | 缺数据/规则/指代、预算、无进展、技术故障分别呈现，保留多原因及已有结果，不伪称信息穷尽 |
| 冻结及恢复 | 在包写入、提交、解释、报告各阶段中断后可恢复；不换包、不重跑完成查询、不清零预算，损坏可检出 |
| 解释边界 | 可综合已核验结果；新数字、新关键假设或不支持的因果陈述被拒绝；输出修复不触发 loop |
| 大结果与上下文 | 全量证据保留，允许按目标读取冻结材料；预览截短不误报缺失，报告渲染不暗中新增统计 |
| 报告溯源 | 每项结论可关联步骤、数据、规则及来源；排除项、未定项和父分析复用均可查；摘要与正文数值一致 |
| 追问与纠正 | 原问题和新目标均保留，群体明确、旧证据按兼容性复用，纠正不被旧结论覆盖；不同数据集不合并 |
| 降级输出 | 未配置模型、预算耗尽或输出校验失败仍保留确定性事实/部分报告；不把输出失败等同于取证失败 |
| 业务回归 | 既有流控、质量、日期、单位、分母、会话、输入去重及部分加载规则不因编排迁移改变 |

采用同一组脱敏真实问题和可复算样例对比新旧架构，涵盖同义改写、不同问题类型、多轮追问、缺测与冲突；记录目标完成度、专家核对正确性、无依据结论、证据关联完整率、重复查询、耗时与模型成本。同一配置下重复运行检查结果一致性，判断同义问题的目标与统计口径是否一致，不要求工具调用顺序完全相同。先建立旧版基线和验收阈值再评估新实现，未验证前不宣称可靠性必然提升。
