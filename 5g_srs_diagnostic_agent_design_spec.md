# 5G NR SRS 基带问题定位专家 Agent 软件详细设计说明书（SDS 图文案例整合版）

修订日期：2026-10-26（SDS v3.3 评审定稿）。整合当前确认版与用户提供的 SDS v3.0 图文案例版本，保留已确认的数据规则与特性文档驱动诊断，新增 Markdown 案例包、图文检索、图表画布与本地双层恢复。

整合原则：案例源文件采用 Markdown + images；数据仍按 CRNTI 缺失判定流控、完整块全留，时间使用 uwData + uwTime，身份使用 logicCellId。相似案例不以全部判据通过为展示门槛；不沿用旧版自动定案、固定耗时承诺或重复引入的旧 SQL。

## 1. 目标、范围与业务约定

系统读取 CDT `NR_SRS.*.csv` 宽表，完成确定性清洗、指标统计、相似案例辅助解释、特性文档驱动诊断与交互下钻，以及经人工审核的案例固化。数据计算由程序执行，LLM 负责问题理解、查询规划、解释与草稿提炼。

### 1.1 已确认的业务边界

1. **流控只在单文件内处理**，不合并不同文件的块。各文件清洗结束后，可以共同参与会话构建、统计和查询。
2. 同文件中，按原始记录顺序划分连续相同 `uwTtiCnt1` 的块；不得先按计数值排序，不得合并不连续的同值块。
3. **流控仅判断原始 CRNTI 是否缺失。块内所有行 CRNTI 非空即为完整块，全部保留，包括单行块；存在 CRNTI 缺失行才执行截断。** TLV 打点的身份字段位于前几个 value，不额外通过尾部字段、日期格式或测量完整性判断流控。
4. `uwData` 表示日期（字段名保持原样），与 `uwTime` 共同确定日期时间；一般最多两天，不通过文件顺序或午夜跳变猜日期。
5. 除非用户特别说明，输入来自同一站点。小区身份使用 `logicCellId`，用户分析会话使用 `logicCellId + crnti + 会话开始时间`。`phyCellInst` 保留为辅助分析字段。
6. 可疑记录和非法流数记录整行退出全部业务统计，原始记录及质量审计保留。RSRP/NI 越界原因独立记录，缺测不等于可疑。所有业务入口执行统一准入，详见 §3.4。
7. `max_stream_gain_diff` 默认表示同一分析范围内各流均值的最大值减最小值。
8. RSRP、NI 默认以 dBm 输入，默认对 dBm 数值直接求算术均值；结果标明该口径。增益仍按字段定义使用 dB，差值单位为 dB。
9. 不考虑 RB 区间重叠。默认 30 kHz；实际流数以每行 ueLayerNum 为准（1/2/4，未来 8）；版本差异、非默认配置、多站点等特殊条件由用户在问题描述中说明，本期不实现混合版本自动拆分或多站点自动推断。
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
| LangGraph、LangChain Core、langgraph-checkpoint-sqlite | 多轮状态、SqliteSaver 持久化、诊断路由与审核流程 |
| Markdown + YAML Frontmatter + images/ | 人工维护的案例权威源文件，解析库可选 python-frontmatter |
| SQLite | 会话注册、检查点、证据元数据、审核记录与案例解析缓存/同步状态 |
| Qdrant、rank_bm25 | 可从权威存储重建的语义与关键词索引 |
| Pydantic v2 | 输入、输出与业务契约校验 |

实现时锁定一组经联调验证的依赖版本，提交锁文件；不以宽松的最低版本声明代替兼容性测试。性能指标通过真实数据基准给出，不预先承诺全量 ETL 毫秒级完成。

## 2. 工程结构与知识资产布局

~~~text
5g_srs_expert_agent/
├── app.py
├── .env.example
├── config/
│   ├── settings.py
│   ├── input_contract.yaml
│   └── query_templates/
├── knowledge_assets/                        # 可人工编辑、可纳入 Git 的知识源文件
│   ├── field_mappings/nr_srs_field_mapping.md
│   ├── features/                            # 特性 B 等文档及出处
│   ├── scenarios/scenario_metrics.yaml
│   └── cases/
│       └── CASE_SRS_001/
│           ├── CASE_SRS_001.md
│           └── images/
│               ├── reference_heatmap.png
│               └── reference_curve.png
├── data_pipeline/
│   ├── csv_loader.py
│   ├── schema_aligner.py
│   ├── etl_engine.py
│   └── anomaly_profiler.py
├── knowledge_base/
│   ├── models.py
│   ├── case_markdown_parser.py
│   ├── kb_sync_manager.py
│   ├── schema_kb.py
│   ├── case_kb.py
│   ├── scenario_kb.py
│   └── feature_kb.py
├── agent/
│   ├── state.py
│   ├── prompts.py
│   ├── sql_executor.py
│   ├── assertion_evaluator.py               # 可选定量验证
│   ├── query_planner.py
│   ├── query_compiler.py
│   ├── evidence_validator.py
│   ├── nodes.py
│   └── graph.py
├── ui/
│   ├── state_manager.py
│   ├── sidebar_panel.py
│   ├── chat_panel.py
│   ├── canvas_panel.py
│   └── chart_renderer.py
├── tests/
│   ├── fixtures/
│   ├── test_pipeline.py
│   ├── test_assertions.py
│   ├── test_case_assets.py
│   ├── test_session_restore.py
│   └── test_diagnostic_flow.py
└── workspace_storage/                       # 不作为知识源文件提交
    ├── session_registry.sqlite              # 注册信息及独立命名的检查点表
    ├── knowledge_registry.sqlite            # 审核/同步记录、解析缓存
    ├── qdrant_local_db/
    ├── datasets/{dataset_fingerprint}/
    │   ├── analysis.duckdb
    │   ├── input_manifest.json
    │   └── snapshot/
    └── sessions/{session_id}/
        ├── artifacts/                      # 完整表结果与图表
        └── drafts/                         # 审核前的图文草稿包
~~~

目录中的案例和图片名称仅为布局示例，不表示已有真实案例资产。字段映射与场景配置统一移到 knowledge_assets，避免与 config 内的旧副本产生双源。图表静态导出适配器与锁定版本联调；导出不可用时保留表格及交互图，不阻断其他诊断。

## 3. 数据底座 L1～L4

### 3.1 L1：单文件保序加载与输入契约

递归扫描生产文件名 `NR_SRS_XX-XX_XX-XX-XX.csv` 和 `NR_SRS(1)_XX-XX_XX-XX-XX.csv`（括号内允许任意非负整数）。使用大小写不敏感的全名匹配 `^NR_SRS(?:\(\d+\))?_\d{2}-\d{2}_\d{2}-\d{2}-\d{2}\.csv$`，兼容旧测试文件 `NR_SRS.*.csv`。目录扫描不接受 `NR_SRS_temp.csv`、备份扩展名或其他前缀；显式指定单个 CSV 路径仍允许任意名称。文件名时间不替代 CSV 内时间。`SYS_INFO.*.csv` 可作为版本说明的辅助信息；与用户说明冲突时显式呈现冲突，不自行选择。

CSV 读取器逐文件、按原始记录顺序解析，并在任何排序、过滤或并行计算之前固化：

| 字段 | 定义 |
| --- | --- |
| `source_file` | 数据集内规范化相对路径，保持唯一 |
| `source_file_hash` | 文件内容哈希 |
| `file_row_id` | 文件内从 1 开始的数据记录序号，不含表头 |
| `record_id` | 数据集 ID、来源文件与记录序号构成的唯一标识 |
| `csv_structure_complete` | 原始记录列数及解析结构是否符合表头 |
| `crnti_present` | 仅判断原始 CRNTI 经空白/空值编码规范化后是否非空，与数值转换成功与否无关 |

使用支持 CSV 引号与嵌入换行的解析器，`file_row_id` 是逻辑记录序号；如需定位文本物理行，可另存起止行号。不得用无排序的 `ROW_NUMBER() OVER (PARTITION BY filename)` 替代原始记录编号。

分批写入 Parquet 或 DuckDB，避免全量宽表转为 Python 对象常驻内存。少列的可解析记录保留其原始结构缺陷；引号损坏等无法可靠确定记录边界的文件进入加载错误报告，禁止静默跳过。

#### 3.1.1 流控判定与其他数据异常分离

`input_contract.yaml` 显式声明：

- `date_format`、`time_format`、编码、分隔符、空值编码；

2026-10-25 现场修订：CSV 日期采用年-月-日，如 `2026-10-25`。Python 解析格式为 `%Y-%m-%d`（`%M` 是分钟，`%D` 不是此处所需的日格式）。标准契约和样例映射契约均使用该日期格式，时间格式仍分别为 `%H:%M:%S.%f` 和 `%H:%M:%S`。非法日期保留原始记录并标记 `INVALID_DATE`，不得静默推测格式。旧数据集保存原契约不变；修复历史日期解析需要使用新契约重新加载源 CSV。

补充字段查询性能约束（现场问题 AH-002）：

- 规划阶段只读取已保存的表头元数据和零行关系模式，不查询补充数值明细，不构建范围表或宽表。
- 执行阶段从 QueryPlan 的条件、分组、指标、比较字段、关联键及显式 `required_fields` 提取所需列。特性生效标志必须纳入 `required_fields`，确保完整证据和回放一致。
- 先构建范围内 resource，再以 record_id 半连接过滤补充数值表，并限定字段名，最后聚合并左连接；缺失/非法值仍为 NULL。默认四流统计不读取补充数值明细。
- 手工 SQL 从已校验的 AST 提取字段；`COUNT(*)` 不触发全字段展开，显式 `SELECT *` 仍表示全列输出，大范围全列查询应缩小范围或改为指定列。
- 保留 512 MB、2 线程及 30 秒连接操作预算，不以延长超时替代优化。已有 m1.3 数据集可直接使用新的查询路径，无需为性能修复重建数据。
- 440 字段、34,000,120 行合成补充数据的基准与复现方法见 `docs/ISSUES.md`。该基准验证准备阶段，不代表现场全链路延迟。
- 查询计划在执行前校验操作语义：CONDITION_COUNT 必须提供 conditions 字典（c0..c999，1–20 项），禁止以 condition 或 metrics 代替；同时校验其他操作必填参数、聚合输出冲突及时间范围。校验错误以位置和规则反馈模型，最多修复两次，失败使用描述性基础计划。
- AH-006：规划阶段依据真实字段目录，逐步推导各关系输出列。聚合仅保留分组列、sample_cnt 和指标输出；条件计数的汇总表和 `_rows` 明细分别建模。比较、分组、聚合、时间关联必须引用当前 source 的列，不能以 depends_on 代替 source 切换。错误反馈 FIELD_SOURCE_MISMATCH、位置和可用关系；执行入口再次校验。真正不存在的特性条件字段仍按 UNKNOWN 处理。
- 2026-10-26 用户确认：instBfSinrGain[0..3] 确定对应第一至第四流（STREAM），不适用其他短数组的 UNCONFIRMED_ARRAY 分类。通常按流序递减，倒序仅待核查，不能直接判定故障。增益全空仅报告缺测，不推断采集故障，不重复确认映射。更新知识版本，规划与解释系统规则采用同一口径；无有效增益样本时仅标记增益证据不足，不阻断 NI 等其他问题；解释及视觉比对由当前问题的必要证据决定。
- 解释校验拒绝需记录错误码、数组位置、尝试次数，并持久化到动作结果 validation_errors 及本地性能文本日志；向模型反馈具体位置、允许的数值列/单位及空列表降级方式。最终提示展示拒绝原因，仍禁止将未验证解释发布为事实。日志不保存原始模型响应，因此不保证能还原修复前的历史解释错误；参见 AH-005 / GitHub #1。
- 性能观测：加载、基础统计、范围准备、查询、模型调用和完整诊断的耗时及进程驻留内存写入独立 UTF-8 文本 `workspace_storage/logs/performance.txt`（每行 JSON）。阶段以 trace_id 关联，记录成功或异常类型；不写密钥、提示词、SQL 或原始业务记录。每 100 ms 采样的最大值为整个进程的采样峰值，不等同于单查询独占峰值。导入时累计补充行数，不新增全表扫描。日志故障不影响业务结果。字段和归档方式见 `docs/PERFORMANCE_LOG.md`。
- `required_identity_columns`：`uwTtiCnt1, logicCellId, crnti, uwData, uwTime`；
- `flow_presence_column`：固定为 `crnti`，只检查原始值是否缺失；
- `nullable_measurement_columns`：允许合法缺测的测量列；
- `stream_metric_families`：已确认按流解释的数组列族。

`crnti_present = 原始 crnti 经显式空值规范化后非空`。即使非空 CRNTI 无法转成数字，也保留该存在性标记，另报类型解析错误；不能因转换结果为 NULL 而触发流控。数值 0 不自动视为空值，除非输入空值编码明确如此规定。

完整块的唯一条件为所有行 `crnti_present=true`。CSV 列数、日期或身份格式错误、尾部测量缺失、越界值均不能触发流控截断。CRNTI 整列不存在属于文件契约缺失，报告文件失败；列存在但记录值全空，则按全空块处理。其他必需字段错误只影响其相关分析；无法建立块键或解析 CSV 记录边界时，记录输入错误而非虚构流控结论。

#### 3.1.2 固定输入、文件去重与部分加载

建立本次 `input_manifest`，将文件复制到会话输入快照，校验复制前后源文件与快照内容哈希一致；读取期间变化的文件重试，仍不稳定则列入失败清单。后续 ETL 只读已固定快照，源目录新增或修改不影响本次结果，重新加载产生新数据版本。

对快照按内容哈希与长度识别相同文件，确认字节内容相同后仅处理一份，记录所有重复来源路径及代表文件；去重先于 ETL 和样本计数。内容不同的文件不按时间戳擅自去重。

文件采用独立事务或临时输出：解析失败的文件不提交其半成品；其他成功文件继续分析。清单记录 `LOADED / DUPLICATE / FAILED`、源路径、快照哈希、重复指向、错误原因和可定位的行号。部分成功标记 `PARTIAL_READY`，报告说明基于哪些成功文件、多少记录及日期范围，以及失败文件和原因。全部失败标记 `FAILED`。无法归属时间的错误不猜测覆盖范围。

字段异常保留原始值及独立原因，如 `MISSING_VALUE / PARSE_ERROR / NON_FINITE / INVALID_DATE / CSV_STRUCTURE_ERROR / INVALID_BLOCK_KEY`；只有真实缺测计入缺测数，解析失败和非有限值另行计数，不能因统一投影为 NULL 而丢失原因。

### 3.2 L2：列标准化与时间坐标

1. 按已选择的字段映射对齐列名，类型转换使用显式规则；失败值记录解析异常，不静默改成正常值。
2. 在 L1 完整扫描/统计后可提前投影移除确认全空的可选列，使后续窗口计算与物化少携带空列；保留字段可用性元数据，区分“不存在”“全空”“部分缺测”“可用”。不能以少量采样推断全空，必选身份、CRNTI 存在性及来源键不裁剪。保序记录号必须先固化，缺失列按需要补类型化 NULL；前置投影主要降低后续内存与存储开销，不承诺免去 CSV 初次扫描。
3. `event_ts = parse(uwData + uwTime)`，保留原始日期时间及其真实精度；`time_sec` 为相对数据集最早有效时间的秒差。缺日期或时间无法解析时记录时间错误，不推测跨日。
4. 默认 `slot_in_frame = subFrmNum * 2 + slot`，范围 `0..19`；`freqRbEnd = freqRbStart + freqRbNum - 1`，要求长度为正。超出默认配置按用户说明处理或报告不支持。
5. `uwTtiCnt1` 缺失的记录不能加入邻近块，单独标记 `INVALID_BLOCK_KEY` 并隔离；计入加载/结构问题，不计作已确认流控截断。

L2 暂不生成最终 `ue_uid`；必须先完成文件内流控过滤，以免被截断的记录参与会话边界判断。

### 3.3 L3：完整块保留与不完整块截断

#### 3.3.1 流控规则

每个文件独立按 `file_row_id` 处理。`uwTtiCnt1` 相对前一记录变化时建立新块，块键为 `(source_file, block_seq)`，另保留原始计数值。缺失块键的隔离记录也打断相邻块。

1. 块内 `crnti_present` 全为真：全块 `VALID_FLOW`，包括单行块，不删除最后一行。
2. 块内至少一行 CRNTI 缺失：找到最后一个 `crnti_present=true` 的行，其从 0 开始的块内序号为 `last_good_idx`。该名称仅表示截断边界，不保证此行尾部测量完整。
3. 对上述块，只保留 `idx_in_block < last_good_idx AND crnti_present` 的行；边界行及其后续全部标记 `INVALID_FLOW_CTRL`。前缀中 CRNTI 缺失的行同样排除；其他字段问题不影响流控标签，另行记录其分析资格。
4. 不完整块内 CRNTI 全空：全块排除。单行不完整块同样排除。
5. 记录 `has_missing_crnti`、`last_good_idx`、块大小、剔除原因和每文件统计。原始行与标签保留，L4 通过过滤排除，不物理销毁源数据。

不再提供 `SINGLE_ROW_BLOCK_POLICY` 或 `keep_single_valid_row` 两套例外开关；是否完整就是唯一入口条件。

#### 3.3.2 行级质量与缺测

仅在 `VALID_FLOW` 行上执行质量检查：

- `usrRsrp` 非空且不满足 `-160 < usrRsrp < -60`，或 `srsUeNiDb` 非空且不满足 `-130 < srsUeNiDb < -80`：`is_suspicious = true`。
- 任一上述测量字段真实缺失：`has_missing_measurement = true`；缺测本身不作为越界可疑。解析失败、非有限值使用独立原因标记，不混入缺测数。
- `data_quality_tag` 优先级为 `INVALID_FLOW_CTRL`、`PARSE_ERROR`、`SUSPICIOUS`、`MISSING_DATA`、`VALID_NORMAL`。L2 产生独立 `has_parse_error`（包括数值、日期等解析失败与非有限数字），完整原因另存；布尔可疑/缺测标记不因显示优先级改变。另行隔离的块键错误不进入以下 SQL。
- 同一行可同时可疑和缺测：分别计入 `suspicious_cnt`、`missing_cnt`，界面说明两者可能重叠，不相加作为总行数。
- 独立记录 RSRP/NI 可疑子类别，可在质量明细查看原始数值。其他字段的缺测保留字段级计数供当前查询判断。完整异常原因保存在 `error_reasons`；解析失败必须在 UI 明确显示，不得呈现为正常。可疑记录或非法流数整行排除；其余记录再按字段级解析状态决定指标是否可计算。

以下 SQL 输入为具有已固化 `file_row_id`、非空 `uwTtiCnt1`、非空布尔 `crnti_present` 的 `l2_standardized`。隔离的块键错误不删除其记录序号，序号间隙用于打断块。可选测量列缺失时通过规范投影补充带类型 NULL，并同步记录不可用状态。L2 同时产生非空布尔 `has_parse_error`；另行产生的 `rsrp_missing / ni_missing` 布尔标记仅指真实缺测或列缺失。解析错误与非有限值另记原因并投影为类型化 NULL。

```sql
CREATE OR REPLACE TABLE l3_cleaned_srs AS
WITH prev_row AS (
    SELECT *,
        LAG(uwTtiCnt1) OVER w AS prev_tti,
        LAG(file_row_id) OVER w AS prev_row_id
    FROM l2_standardized
    WINDOW w AS (PARTITION BY source_file ORDER BY file_row_id)
),
block_mark AS (
    SELECT *,
        CASE WHEN prev_row_id IS NULL
               OR file_row_id <> prev_row_id + 1
               OR uwTtiCnt1 IS DISTINCT FROM prev_tti
             THEN 1 ELSE 0 END AS new_block
    FROM prev_row
),
block_id AS (
    SELECT *,
        SUM(new_block) OVER (
            PARTITION BY source_file ORDER BY file_row_id
            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
        ) AS block_seq
    FROM block_mark
),
numbered AS (
    SELECT *,
        ROW_NUMBER() OVER (
            PARTITION BY source_file, block_seq ORDER BY file_row_id
        ) - 1 AS idx_in_block
    FROM block_id
),
block_stats AS (
    SELECT *,
        COUNT(*) OVER w AS block_size,
        BOOL_OR(NOT crnti_present) OVER w AS has_missing_crnti,
        MAX(CASE WHEN crnti_present THEN idx_in_block END)
            OVER w AS last_good_idx
    FROM numbered
    WINDOW w AS (PARTITION BY source_file, block_seq)
),
flow_tagged AS (
    SELECT *,
        CASE WHEN NOT has_missing_crnti THEN 'VALID_FLOW'
             WHEN idx_in_block < last_good_idx AND crnti_present
                  THEN 'VALID_FLOW'
             ELSE 'INVALID_FLOW_CTRL' END AS flow_status
    FROM block_stats
),
quality_flags AS (
    SELECT *,
        flow_status = 'VALID_FLOW' AND (
            COALESCE(usrRsrp <= -160 OR usrRsrp >= -60, false)
            OR COALESCE(srsUeNiDb <= -130 OR srsUeNiDb >= -80, false)
        ) AS is_suspicious,
        flow_status = 'VALID_FLOW' AND
            (rsrp_missing OR ni_missing) AS has_missing_measurement
    FROM flow_tagged
)
SELECT *,
    flow_status = 'VALID_FLOW' AND COALESCE(ueLayerNum IN (1,2,4,8), false)
        AND NOT is_suspicious AS business_eligible,
    CASE WHEN flow_status <> 'VALID_FLOW' THEN 'INVALID_FLOW_CTRL'
         WHEN ueLayerNum IS NULL OR ueLayerNum NOT IN (1,2,4,8) THEN 'INVALID_LAYER_COUNT'
         WHEN has_parse_error THEN 'PARSE_ERROR'
         WHEN is_suspicious THEN 'SUSPICIOUS'
         WHEN has_missing_measurement THEN 'MISSING_DATA'
         ELSE 'VALID_NORMAL' END AS data_quality_tag
FROM quality_flags;
```

该 SQL 将窗口函数拆为独立阶段，禁止窗口函数嵌套。源值的非有限数字与解析错误应在 L2 标记并归入相应异常，不能作为正常测量输入。

#### 3.3.3 观测会话与唯一性

在流控清洗完成后，对有效且身份、时间可用的记录按 `(logicCellId, crnti)` 分组，以 `(event_ts, frmNum, slot_in_frame, source_file, file_row_id)` 稳定排序。相邻观测间隔 `>180` 秒时建立新观测会话，等于 180 秒不切分。

`ue_uid = "{logicCellId}_{crnti}_{session_start_ts}"`，时间采用完整日期与源精度的规范格式。同一身份同一时间的记录属于同一观测会话；行唯一性由 `record_id` 保证，不能将时间戳当作明细主键。数据集内对会话标识施加唯一性检查。

流控有效但身份或时间不可用的记录仍保留在 L3 和质量明细中，ue_uid 允许为空，并记录 session_unassigned_reason。不得把空 ue_uid 合并为一个虚假 UE；UE 汇总排除无法归属会话的记录，时间过滤查询排除无法定位时间的记录并说明数量。其他分析仅检查其实际所需字段。基线分别列出流控有效数、会话可归属数和各指标实际统计数。

这是观测会话划分，不是物理 UE 身份确认；短时间内真实 CRNTI 复用不能单凭该规则识别。除非用户提供额外身份日志，不声称确认了新 UE。

多文件清洗后按上述时间共同构建会话，但绝不改变已经完成的单文件流控结果。默认单站点；用户声明多站点时，应要求分开分析或提供明确站点标识，禁止静默混合。

### 3.4 L4：四张分析表及统计口径

| 表 | 粒度 / 键 | 必须保留的内容 |
| --- | --- | --- |
| `l4_ue_resource_ts` | 每原始记录一行，`record_id` 唯一 | `logicCellId, phyCellInst, ue_uid, crnti, resourceId, uwData, uwTime, event_ts, time_sec, frmNum, subFrmNum, slot, slot_in_frame, freqRbStart, freqRbNum, freqRbEnd`、标量测量、质量标记、来源文件与记录号 |
| `l4_ue_stream_ts` | `(record_id, streamId)` 唯一 | 上述定位与质量字段，以及对应流的指标 |
| `l4_ue_summary` | `(logicCellId, ue_uid, resourceId)` | 起止时间、样本数、可疑数、缺测数、各指标有效样本数、RSRP/NI 统计、各流均值、`max_stream_gain_diff` |
| `l4_cell_tf_summary` | `(logicCellId, slot_in_frame, freqRbStart, freqRbNum)` | 样本数、观测会话数、可疑/缺测数及比例、各指标有效样本数、NI 均值/分位数/最大值、RSRP 均值 |

L3 审计明细保留可疑与缺测行；L4 业务明细只收入 VALID_FLOW 且流数合法、非可疑的记录。sample_cnt 指业务准入记录数；流控有效数与质量计数另列，不等同于某指标有效样本数。

测量聚合统一使用 EXCLUDE_ABNORMAL_RECORD：流控有效、ueLayerNum 属于 1/2/4/8 且非可疑的记录才能参与；任一可疑指标使整行退出，包括该行其他正常指标。准入后当前指标仍需为可解析有限数值；缺测不补零。质量计数基于原始流控有效行，单独披露分母。

当前分析范围由唯一数据集及用户选择的小区、UE、资源、时间条件确定。每组输出 sample_cnt、metric_valid_cnt、suspicious_cnt、suspicious_ratio、missing_cnt、missing_ratio、parse_error_cnt，以及实际起止时间。可疑比例与行级缺测比例以该组流控有效原始记录数为分母，两种计数可能重叠；指标级缺测率另列且命名明确。空分母返回 NULL。“异常比”在常规统计中指 suspicious_ratio，不等同于特性未生效率。

以下全范围示例运行时必须先套用用户过滤条件；进入 L4 的非法或非有限测量值已转换为类型化 NULL 并保留原始值/原因：
~~~sql
SELECT
    logicCellId, ue_uid, resourceId,
    COUNT(*) AS sample_cnt,
    COUNT(*) FILTER (WHERE is_suspicious) AS suspicious_cnt,
    AVG(CASE WHEN is_suspicious THEN 1.0 ELSE 0.0 END) AS suspicious_ratio,
    COUNT(*) FILTER (WHERE has_missing_measurement) AS missing_cnt,
    AVG(CASE WHEN has_missing_measurement THEN 1.0 ELSE 0.0 END) AS missing_ratio,
    COUNT(*) FILTER (WHERE has_parse_error) AS parse_error_cnt,
    COUNT(usrRsrp) AS rsrp_valid_cnt,
    AVG(usrRsrp) AS rsrp_avg,
    COUNT(srsUeNiDb) AS ni_valid_cnt,
    AVG(srsUeNiDb) AS ni_avg,
    QUANTILE_CONT(srsUeNiDb, 0.9) AS ni_p90,
    MIN(event_ts) AS start_time,
    MAX(event_ts) AS end_time
FROM l4_ue_resource_ts
WHERE ue_uid IS NOT NULL
GROUP BY logicCellId, ue_uid, resourceId;
~~~

流展开按已确认字段族同步构造四元素列表并 UNNEST；缺失下标补带类型 NULL，不能前移。标量统计从资源明细计算；流组内每条原始记录仅计一次，跨流累计时按 record_id 去重，不能使四倍展开改变异常比例。

同一数据集内比较 UE、资源或流时，各组独立计算均值及上述比例，**不强制取共同记录交集，不因时间覆盖不同丢弃样本**。同一 UE/资源/用户时间窗内，max_stream_gain_diff = max(各流均值) - min(各流均值)，至少两流有值才返回差值；报告列明哪些流有数据，不能把两流结果称为四流结果。资源是同一数据集中的不同 resourceId，不是版本或独立数据集。

时间、RB、样本量或覆盖不同可提示解释限制，但不阻止描述性出表。只有某项特性文档明确要求逐事件或时间关联时，才为该特定条件建立关联，不能将其变成所有均值比较的全局配对规则。

全周期汇总只在数据版本、过滤范围、指标和分组口径一致时复用；其他情况从明细重算，不能对均值再简单求均值，或从组分位数拼出总体分位数。缓存键包括数据指纹、过滤条件、指标定义和规则/计划版本。RB 不重叠，不实现区间拆分；默认描述为 RB 区间统计。


### 3.5 均值、波动与时间序列补充

l4_ue_summary 增加 rsrp_std、rsrp_trend_delta、ue_total_records 和指标有效计数。rsrp_std 使用样本标准差，不足两点为 NULL；rsrp_trend_delta 为当前对象有效时间覆盖中点之后的 RSRP 均值减之前的均值（dB），中点属于后半段；两侧无数据或起止时间相同时返回 NULL。不得将该指标单独解释为单调趋势。

ue_total_records 在当前过滤范围按 ue_uid 对资源明细的 record_id 去重计数，不能累加流展开行数。普通时序表使用含日期的 event_ts 按秒分桶，按 UE、resourceId、streamId（流表适用）分别输出均值、异常比、缺测计数及样本数；不能仅按 HH:MM:SS 把两天同一时刻合并。展示降采样/桶宽明确标记，图表预览不改变原始统计和证据。

### 3.6 本地数据集持久化与恢复

L2、L3、L4 和 _etl_meta 固化至数据集目录内 analysis.duckdb。_etl_meta 至少包含完整 dataset_fingerprint、输入清单哈希、映射/清洗/schema 版本、READY/PARTIAL_READY 状态、成功/失败来源、基线及构建完成时间。

数据指纹沿用已确认的内容哈希、配置和规则版本，路径/文件大小/mtime 只能作为查找提示，不能单独证明内容一致。恢复已保存会话时按其固定数据版本重新打开数据库，无需重新扫描原始 CSV；重新加载源目录则建立并核对新清单，不能把旧快照冒充最新源数据。

构建中数据库不对诊断开放；成功后发布完成元数据，部分成功记录覆盖清单。数据库不存在、构建未完成、损坏或 schema 不兼容时提示修复/重建，不能仅因文件存在即复用。相同数据版本的写入串行化，查询使用受控连接；重启恢复性能通过基准测试，不作“0 秒”或固定毫秒承诺。


## 4. 知识库、相似案例与特性文档

### 4.1 字段元数据

FieldMeta 包含标准名、原始名映射、中文含义、类型、单位、所在表、数组族、可空性、有效范围及场景标签。target_l4_tables 可包含四张分析表。字段可用性以当前数据实际 schema 为准，文档提到的字段不代表数据里一定存在。

检索相关字段后合并该表适用的身份、时间、质量键。字段语义或单位不能可靠映射时记录待确认项，不自行猜测。无需为每种问题事先写一份 SQL。

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

### 4.3 检索与案例固化

案例、字段、场景和特性文档分别索引；使用 Qdrant + BM25 融合召回，案例默认 Top-3 可配置。数据指纹展示实测差异与异常比例，没有门限时不强行二分“正常/异常”。

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

原始特性未启用率 B=0/(B=0+B=1) 与预期不一致率（应生效且 B=0 / 应生效且 B 可判断）分别命名；分母为零返回 NULL。非法或缺失 B 单独报告。各条件展示 evaluated_cnt、unmet_cnt、unknown_cnt，未满足比例使用 unmet_cnt/evaluated_cnt；同时展示相对当前 B=0 分析对象全集的覆盖，不隐去未知样本。

多个条件可能同时不满足：默认提供各条件独立占比、共同评估范围内的重叠计数、涉及 UE 数及覆盖。不能把独立占比相加当作互斥原因分布。仅当文档明确优先级时才计算“首个阻止生效条件”；未知前置条件不得跳过后强行归因。

“主要原因”默认描述最常观察到的未满足条件及证据，不把频率直接当因果。候选覆盖不同且无法公平排序时明确限制；保留“现有条件无法解释”的残余对象。报告采用“文档条件 → 当前数据 → 佐证/反证 → 可能解释 → 下一步检查”的可核对推理过程，可并列引用相似案例。


### 4.6 图文 Markdown 案例包与解析契约

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

case_markdown_parser.py 使用 Markdown 结构解析：保留 YAML、正文段落、可选 SQL 块、图像引用及相邻视觉说明的关联。CaseCard 为解析投影，增加 bundle_path、source_hash、images、section_refs。ImageAsset 包含 image_id、relative_path、sha256、mime_type、width、height、role、caption、caption_source、source_ref、evidence_artifact_id。caption_source 区分 HUMAN / MODEL，自动生成说明不冒充人工确认。

可执行 SQL 与叙述占位符分离：文字可保留泛化占位符，SQL 必须使用绑定参数并读取受控关系。不得照搬在汇总表上查询原始 srsUeNiDb 等不存在列的示例；可选 Slot 查询样式为：
~~~sql
SELECT slot_in_frame,
       COUNT(*) AS sample_cnt,
       COUNT(srsUeNiDb) AS ni_valid_cnt,
       AVG(srsUeNiDb) AS ni_avg,
       MAX(srsUeNiDb) AS ni_max,
       AVG(CASE WHEN is_suspicious THEN 1.0 ELSE 0.0 END) AS suspicious_ratio
FROM scoped_resource_rows
GROUP BY slot_in_frame
ORDER BY slot_in_frame;
~~~
scoped_resource_rows 是执行器已限定数据集、小区、时间和用户筛选的资源明细关系。此查询描述分布，无固定异常门限；可疑比是整行质量比例，不改称 NI 专属越界率。最小/最大 RB 只表示跨度，不能据此声称所有中间 RB 都被覆盖。

### 4.7 图文检索与模型能力分支

索引输入为 YAML 标题/标签/适用条件、故障表象、推理分节及图片视觉说明文本；按案例与章节/image_id 建立可追溯块，再做 BM25 与文本 embedding 混合检索。caption 的文本索引不等于直接对图片编码；不能保证只有图片、没有文字的特征一定能召回。

召回图文案例后：
1. 支持视觉的已配置模型：适配器读取经校验的图片内容，使用服务支持的图像载荷发送历史图与当前查询结果生成的图，附数据表、单位与来源标签。不能仅把本地路径字符串发给远程模型就认为其看到了图片。
2. 纯文本模型或视觉调用失败：使用正文、人工 caption 与当前实测表解释，历史图片仍在界面展示供专家核对。没有 caption 时说明无法自动理解该图，不编造视觉特征。
3. 生成或比较当前图前，记录横纵轴、单位、桶宽、时间范围和色阶；可采用一致显示尺度，但不得通过缩放隐藏差异。模型指出相似形态、不同位置/幅度及未验证项，而非凭“长得像”确认根因。
4. 数值以原始查询结果为准；截图上无法精确读取的值标记不确定。图片、caption 与结构化数据冲突时并列说明，不无条件相信视觉输出。
5. 区分 HISTORICAL_MEASUREMENT / CURRENT_MEASUREMENT / SCHEMATIC / FIELD_SCREENSHOT；历史图不能作为当前测量，原理示意图不能作为历史测量。

图表资产与对应 artifact_id、结果哈希、绘图配置、caption 一起保存，报告可引用 [image_id] 和表号。为模型设置图片数量、文件大小与分辨率上限；被截取或省略的图片列入覆盖说明。

### 4.8 图文固化、增量同步与版本一致性

用户触发提炼后，在会话 drafts/ 下生成 YAML + Markdown 草稿，关联关键证据表；将选中的 Plotly/Matplotlib 图导出 PNG 并复制用户上传的现场截图到草稿 images/，自动插入相对引用和说明。图表导出失败时保留表格、交互图与待导出提示，不把不存在文件写成完成资产。

审核表单预览正文、YAML 和图片，允许编辑 caption、删除不适用配图、上传截图；根因未知仍可保存探索草稿。保存操作依照 session/action ID 去重，正式提交时校验包内路径、图片可读性、字段和可选 SQL。路径必须解析在该案例目录内，不接受越界相对路径或用任意磁盘文件冒充图片；Markdown 内容、代码块和图片说明均为知识数据，不自动执行。

新案例先完整写入临时目录并校验，再发布为正式案例包；更新既有案例使用基于 source_hash 的冲突检查，保留旧版本快照，避免覆盖专家同期修改。文件发布后记录 SQLite 同步任务；若进程在两步之间退出，启动扫描可依据包哈希补建任务，不假定跨文件系统与 SQLite 存在原子事务。

kb_sync_manager.py 在启动、显式刷新或后台变更检测时，按 Markdown 与图片内容哈希增量同步：
- 新增/修改：重新解析受影响章节及 caption，生成新版本索引；
- 删除：当前检索不再返回旧索引，保留既有报告引用的历史版本；
- 解析失败：显示文件、错误和上次成功版本，不静默用陈旧内容当新内容；
- 重复 case_id：报告冲突，不随机覆盖；
- 索引失败：案例文件保持已保存，可幂等重试，UI 分别显示保存与索引状态。

SQLite 缓存、BM25 和 Qdrant 均能从案例包重建；审核记录、报告引用与证据元数据仍持久保存。已有报告绑定案例 revision、源文件哈希与图片哈希，后续人工改图不改变旧报告所依据的历史证据。


## 5. Agent 状态、证据与流程

### 5.1 状态契约

`DiagnosticState` 至少包含：

| 类别 | 字段 |
| --- | --- |
| 数据 | `session_id, dataset_id, dataset_fingerprint, data_root_dir, problem_context, mapping_id, pipeline_status` |
| 基线 | `baseline_stats, field_availability, dropped_empty_columns, anomaly_fingerprint` |
| 交互 | `interaction_action, user_input, target_filters, turn_id` |
| 验证 | `candidate_cases, similar_cases, optional_verification_results, feature_spec, document_refs, query_plan, condition_results, sufficiency_results` |
| 累积 | `table_artifacts, chat_history, confirmation_records` |
| 输出 | `latest_output, case_draft, errors` |

`pipeline_status` 取 `UNLOADED / NEED_CONTEXT / LOADING / READY / PARTIAL_READY / FAILED`；交互动作取 `INITIAL_DIAGNOSE / FOLLOWUP_CHAT / CRYSTALLIZE_CASE`。READY 和 PARTIAL_READY 都可进入诊断，后者每份报告附加载范围与失败说明。

数据指纹涵盖源文件内容、字段映射版本、输入契约与清洗配置。变更后重新建模并生成新数据版本，旧证据不能继续用于新版本结论。筛选变化更新当前分析范围，不必重新执行全量清洗。

### 5.2 输出与不可变证据

`ExecutedTableArtifact`：

- `artifact_id`：新执行生成新 ID，不能覆盖历史结果；
- `dataset_fingerprint, turn_id, title, purpose, sql, parameters, filters, query_hash, input_manifest_id, included_files, quality_policy, quality_scope, suspicious_ratio, coverage_summary, plan_id, rule_version, document_refs`；
- `result_path, result_hash, total_row_count, preview_row_count, is_truncated`；
- `markdown_preview, column_units, metric_sample_counts, is_pinned_evidence, chart_refs`。

完整结果落盘，UI 分页读取，模型可看前 30 行预览及摘要；验证算子必须使用完整范围计算结果，不能从预览推断全表。

`EvidenceClaim` 绑定 `claim_id, artifact_id, result_hash, row_key, metric, value, unit, scope`。差值、比例等派生值必须绑定可追溯的已执行计算。`evidence_validator` 检查数值、单位、舍入容差、范围与结果哈希；关键数字通过结构化 claim 渲染，不能只检查报告里是否出现表号。叙述不得扩大 claim 支持的范围。

`DiagnosticOutput`：

- `mode`：`SIMILAR_CASE_REFERENCE / FEATURE_DIAGNOSIS / EXPLORATORY_ANALYSIS / DATA_INSUFFICIENT`；
- `conclusion_level`：`OBSERVATION / HYPOTHESIS / CONFIRMED`；
- `similar_case_ids, similarities, differences, unverified_items, condition_results`；
- `claims, reasoning_or_analysis_markdown, suspected_directions, case_asset_refs, chart_artifact_refs`；
- `missing_data_and_limitations, suggested_followup_queries, confirmation_record_ids`。

`SIMILAR_CASE_REFERENCE` 表示引用相似案例，不表示当前根因已匹配；特性分析使用 FEATURE_DIAGNOSIS。输出事实与假设分别标记，报告级别不得把所有陈述统一升级。`CONFIRMED` 必须有满足定案要求的外部证据或专家确认记录；专家确认标明身份/来源、时间、原话和依据，不能伪装成由 SRS 自动推导的结论。

### 5.3 状态机与节点

~~~mermaid
flowchart TD
    Start[问题与当前数据集] --> Profile[加载或复用固定输入和基线]
    Profile --> Route{用户动作}
    Route -- 提炼 --> Draft[证据与案例草稿]
    Route -- 首轮或追问 --> Retrieve[检索字段 特性文档 相似案例]
    Retrieve --> Plan[LLM 生成结构化查询计划]
    Plan --> Check{计划可执行?}
    Check -- 部分字段缺失 --> Partial[标注未知并执行可用步骤]
    Check -- 必需信息无法确定 --> Missing[说明不足或请求必要澄清]
    Check -- 是 --> Execute[编译 SQL 并受控执行]
    Partial --> Execute
    Execute --> Evidence[事实与条件结果校验]
    Evidence --> Explain[解释现象 条件与相似案例]
    Explain --> Output[返回证据与下一步建议]
    Missing --> Output
    Draft --> Output
~~~

**加载节点**：构建或复用固定数据快照，支持 PARTIAL_READY，展示成功/重复/失败来源及质量计数。一个计划只绑定一个数据集。

**知识检索节点**：同时检索当前问题需要的字段、特性条款与相似案例。即使当前缺少验证数据，也可展示历史参考，但必须声明哪些相似仅来自用户描述，哪些已由实测支持。

**计划节点**：结合真实 schema、文档条件、当前筛选及历史对话，生成结构化 QueryPlan。首轮和追问走同一套计划与执行检查；不要求用户预先提供完整查询模板。已有模板可复用，无模板时组合通用算子。

**执行节点**：校验字段、单位、范围、条件出处和操作类型后执行。SQL 语法/绑定错误最多修复两次；实际缺字段不盲目重试。部分步骤失败时保留成功结果，受影响条件标记 UNKNOWN 或技术错误。

**解释节点**：展示事实、相似点/差异、条件未满足分布、尚未验证项及下一步动作。所有当前数字来自实际结果；历史案例与文档引用分开。普通数据探索可展示 Slot/RB、UE/资源、流均值等表，缺某切面不影响其他切面。

**固化节点**：将当前证据、文档出处、查询计划与讨论提炼为 Markdown 图文草稿，按 §4.8 归档图表及现场截图，经用户审核保存；保留事实和推测边界。断言可选，不要求先填写门限。未确认根因保留为探索记录。


#### 5.3.1 默认探索对象与四类表

没有特定任务要求时，指定 ue_uid 就分析该会话全部符合筛选的数据；仅指定 crnti 时，在当前小区和时间范围内列出其对应会话并分别统计，不能把复用 CRNTI 混为同一 UE。不对已明确选择的对象再套 Top-10 限制。

未指定用户时，按当前范围内流控有效且可归属会话的原始记录数选择 Top-10 活跃 ue_uid，记录数相同按 ue_uid 稳定排序。此策略为展示优先级，不是异常严重程度排名；报告覆盖人数与总人数，不能据此声称全体用户正常。特性 B 的原因比例等用户要求的全范围统计不套用此默认 Top-10，必要时全范围聚合、Top-10 仅用于展开明细。

| 表 | 范围与内容 |
| --- | --- |
| 小区 Slot/RB 分布 | 当前显式筛选范围内的均值、异常比与覆盖，不默认放大到用户未选的小区/UE |
| UE/资源均值与波动 | 目标会话的均值、极值、rsrp_std、rsrp_trend_delta、异常/缺测比例 |
| UE/资源/流对比 | 各流独立均值、样本数、流均值之差与异常比 |
| UE/资源/流时序 | event_ts 时间桶内变化，表格与折线图切换 |

四类表按实际字段可用性生成，不凑齐空表。全量分析指使用全部符合范围的数据计算，不要求把所有明细传给模型或一次渲染；分页/图表抽样必须注明。


### 5.4 LLM 查询规划与实现边界

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

通用算子首版实现 FILTER、GROUP_AGG、CONDITION_COUNT、COMPARE、TIME_SLICE；文档有明确时序规则时使用 TEMPORAL_RELATE。字段、函数、运算符由白名单选择，值参数绑定；条件采用结构化表达式树，不 eval 模型生成的表达式。

CONDITION_COUNT 同时生成条件聚合摘要和内部逐记录/事件状态关系 condition_evaluated_rows，后者保留对象键、B 值与各条件三值状态，供后续筛选使用。计划依赖必须指向实际产出的关系，不能从聚合计数反推异常记录。具体填写样式见同目录 query_template_style.md。

执行器根据已实现算子编译参数化 SQL。FILTER 固定用户范围；GROUP_AGG 计算均值、计数和异常比；CONDITION_COUNT 计算 TRUE/FALSE/UNKNOWN；COMPARE 比较各组独立统计；TIME_SLICE 返回有界明细；TEMPORAL_RELATE 必须注明对象键、先后顺序、时间窗口和一对多处理方式，避免关联放大样本。缺时间规则时保留未知，不猜时间容差。

常见查询直接编译；复杂需求可让 LLM 生成单条只读 SQL，但只读取受控当前范围的关系。首版不承诺理解任意 SQL 的全部语义：超出已实现检查能力的结果可作为探索表，明确标注限制，不用于自动判定特性条件或确认根因。特性自动判定优先采用可编译计划。

示例查询次序：B 的取值分布 → 条件未满足与未知数量 → 有充分依据认定应生效但 B=0 的对象 → 佐证与时间切片。各步可按依赖执行并复用结果，设置单轮步骤数、时限与结果上限，防止无界自循环。查询计划摘要在界面可查看，常规查询无需逐条人工审批。

### 5.5 数据充分性与解释边界

只对当前任务的实际必需条件设门禁。描述性均值和异常比无需异常门限即可展示；缺测、少量样本或覆盖差异如实提示，不因可疑比例高而阻断出表。

特性规则要求的字段缺失时，其条件为 UNKNOWN，不影响其他可判断条件；只满足必要条件不能宣布特性本应生效。需要具体时间关联而规则缺失时，不作该关联判断。数据为空时仍可说明文档和案例参考，但不能编造当前实测推断。

无 CRNTI 的截断行无法归属某 UE 时，只报告文件/块级清洗情况。不同分析的分母必须明确，各条件观察覆盖不同不能隐去。

### 5.6 SQL 执行边界

模型 SQL、模板 SQL 和手动 SQL 统一走执行器：

1. 解析并限制为单条只读查询及受控 CTE；允许的表/字段/函数白名单化，拒绝写入、DDL、ATTACH、扩展安装加载及外部读取。
2. 参数值绑定，标识符来自实际 schema；只暴露当前 dataset_id 与用户筛选范围内的受控关系。
3. ETL 与查询连接权限分离，查询连接关闭不需要的外部访问并固定配置。
4. EXPLAIN 检查语法/绑定与计划，不替代安全或业务语义检查。
5. 限制运行时间、内存、结果规模、并发和重试次数。预览截断明确标记，完整统计通过聚合计算。
6. 记录计划、SQL、参数、实际范围、来源文件、结果哈希及错误。报告范围来自执行元数据，不由模型自报。
7. 自动条件判断仅接收支持的算子/已检查查询结果；自由探索 SQL 的能力边界明确，不把“执行成功”当作“根因确认”。

## 6. Streamlit 三栏工作台

### 6.1 左栏：数据与靶点

左栏增加历史诊断会话下拉框，显示会话标题、数据版本、最后访问时间与完整/部分加载状态。选择后按 §6.4 恢复；另提供目录加载、问题上下文、流水线状态、基线及 `logicCellId / ue_uid / crnti / 时间范围 / resourceId` 筛选。`phyCellInst` 可作为附加筛选。

基线卡片显示“流控有效记录”“流控剔除率”“可疑行数”“缺测行数”，可展开逐文件与块级明细。分别展示结构/解析错误，不能与流控混算。

### 6.2 中栏：解释与交互

流式展示实际执行节点和状态；输出可核对的诊断依据，不展示无证据的内部猜测。引用案例显示“相似案例参考”，同时显示观察/假设/确认级别。无证据、部分缺失与技术错误采用不同提示。

支持自然语言追问、快捷下钻和“提炼案例草稿”。用户改变靶点后，在新回答中明确当前范围；历史回答保留原范围。

### 6.3 右栏：表格画布与审核

表格卡片展示来源范围、样本数、单位、质量口径和 SQL。可疑整行高亮，缺测与解析错误另行呈现；排序/筛选通过明确控件或所选版本支持的组件实现。支持切换 Plotly 折线图/热力图，图轴、单位、桶宽与数据版本保持可见。

图文案例召回后，中栏可显示历史参考图和文字，右栏显示当前实测图；也可在画布内并排展示，并明确“历史参考/当前实测/原理示意”。图片缺失或不能解码时显示具体原因，纯文本模型下仍允许人工看图。

修改 SQL 后生成新的 artifact，并记录来源 artifact，不覆盖已被报告引用的表。置顶只表示用户认为重要，不自动升级为定案证据。

图文审核表单提供 YAML 元数据、Markdown 正文编辑及渲染预览，编辑相似现象、推理依据、出处、适用条件与根因确认状态；支持关键图导出、现场截图上传、caption 编辑。查询和结构化判据可选。提交前校验包及可选查询，按 §4.8 写入案例包，区分“文件已保存”“索引待同步/已就绪”。

右栏焦点状态使用经锁定版本验证的可控组件实现；不能假定设置 `active_right_tab` 就会自动控制 `st.tabs`。

### 6.4 状态一致性与持久化

`st.session_state` 保存 UI 选择和已提交操作 ID；LangGraph 状态保存诊断进度；持久化存储保存证据和案例。明确字段归属，不进行两个状态字典的无条件相互覆盖。

使用完整 UUID 作为 `session_id/thread_id`。列表增量按消息/证据 ID 去重；每次用户动作生成操作 ID，Streamlit 重跑不能重复执行 SQL 或重复入库。使用 LangGraph SqliteSaver 保存检查点，内存保存器仅用于开发验证。

session_registry.sqlite 保存会话与 dataset_fingerprint/数据库路径、manifest、schema 版本的关联，检查点表独立命名。DiagnosticState 只保存消息、计划、条件状态和 artifact 引用/小预览，不把完整宽表、所有 JSON 行或图片 base64 复制进每轮检查点。完整表和图表保存在会话 artifacts/，由 SQLite 元数据引用。

恢复顺序：读取会话注册 → 确认固定数据版本与 ETL 完成状态 → 连接 analysis.duckdb → 恢复检查点与筛选 → 校验并分页加载表格/图表引用。不会重新清洗 CSV，也不会把当前源目录变化自动混进旧会话。新加载数据建立新数据版本与显式上下文边界。

已完成的 SQL/入库操作按 action_id 查询状态，恢复不能重复执行；中途失败的步骤标记可重试，不伪造成功。缺少图表或结果文件时保留聊天和其他证据，提示缺失；仅在对应固定数据版本仍可用时允许显式重建，并生成新 artifact。实际恢复耗时取决于数据与资源，不承诺零耗时。

### 6.5 模型配置与异常提示

模型服务由用户在 `.env` 自行填写，提供 `.env.example`，不预置真实密钥或擅自选模型。LLM 和 embedding 分别配置 `PROVIDER / BASE_URL / MODEL / API_KEY / TIMEOUT_SECONDS / MAX_RETRIES`，完整键名见示例。默认超时 60 秒、最多重试 2 次，可由用户修改。是否必需密钥按服务适配器判定，支持无密钥本地服务时不强制填写。

可选视觉能力也由 .env 配置：LLM_SUPPORTS_VISION 默认 false；如单独使用视觉服务，可配置 VISION_PROVIDER / BASE_URL / MODEL / API_KEY / TIMEOUT_SECONDS / MAX_RETRIES。适配器验证请求能力，不凭模型名猜测。未配置或不支持视觉时退回文本、caption 与人工图片查看；图像输入使用本地资产读取后的服务支持载荷。

实际 `.env` 不入库，日志、错误信息、模型上下文和 UI 不显示密钥。记录非敏感模型标识与版本；embedding 模型变更后重建兼容索引，不能混合使用不同向量空间。

| 异常 | 用户提示与系统行为 |
| --- | --- |
| LLM 未配置 | “尚未配置诊断模型，请填写 .env；数据加载与确定性统计仍可使用。” |
| 鉴权失败 | “模型服务鉴权失败，请检查 API 密钥及服务地址。”不回显密钥，不反复重试鉴权错误 |
| 超时、限流、服务不可用 | “本次模型分析未完成，已保留清洗数据和查询结果，可重试。”按配置有限重试 |
| embedding 未配置或不可用 | 提示语义检索不可用，可使用 BM25 检索并明确标注降级；不能称已检索全部可能案例 |
| 模型输出结构无效或证据不符 | 最多两次修复，仍失败则显示事实表与失败原因，不发布未经校验的结论 |
| 输入部分失败 | “结果基于以下成功加载文件……”并列出失败文件、原因、成功记录及覆盖范围 |
| 存在可疑数值 | “可疑记录已整行排除，质量统计保留，业务有效样本为 N。” |
| 分组覆盖不同 | 正常展示各组独立统计，注明时间和样本覆盖差异，不强制丢弃或配对 |
| 图片缺失或导出失败 | 保留正文/数据表，说明受影响图片，不能假装已完成图形比对 |
| 视觉服务不可用 | 退回文本与已有 caption，并明确本轮未执行视觉对比 |
| 会话关联的数据或证据不可用 | 恢复可用历史，标记缺失，不自动重新分析成新结果冒充旧结果 |
| 无预置模板 | 根据文档和真实 schema 规划通用查询，不要求先填门限；必需字段或条件含义不明才提示 |

服务失败不得清空已有成功结果；失败状态与“没有异常”必须区分。

## 7. 确定性质量门禁

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

## 8. 实施顺序与验收

1. **数据底座**：先实现输入契约、稳定记录编号、日期解析、单文件块处理、质量统计、会话和四张表。使用真实脱敏样本与人工期望结果核对。
2. **知识与计划**：实现 Markdown/YAML/图片解析、案例增量同步、文本与 caption 混合检索，再接入特性文档、通用查询算子和证据校验；定量断言可选。视觉模型按配置增强，不阻塞文本流程。
3. **Agent 闭环**：覆盖相似案例参考、无模板自动规划、特性条件分析、证据不足、追问、查询失败及案例固化流程。
4. **UI 与持久化**：完成四类探索表、表图切换、图文并排和 Markdown 审核；验证 DuckDB 数据复用、SqliteSaver 恢复、不可变证据、重跑去重及索引重试，再测试真实规模的内存与响应时间。

最低验收矩阵：

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
| 图像与表格数字冲突 | 标记冲突，精确统计回到查询结果 |
| 案例保存后索引失败或中途退出 | 文件包保持有效，同步任务可恢复重试 |
| 恢复昨天的固定数据集 | 不重读原始 CSV，不重复 ETL，恢复原会话与证据 |
| 未指定用户的默认探索 | Top-10 活跃会话展开，标明并非异常排名 |
| 特性原因全范围查询 | 不套用默认 Top-10 限制 |
| 多天同一时分秒的时序图 | 由 event_ts 区分日期，不合并两天数据 |
| 图表预览降采样 | 明示粒度，完整统计不被预览改变 |

模拟数据负责精确边界测试；真实脱敏数据负责确认“完整/截断”输入契约、清洗误删情况及专家诊断一致性。未完成真实样例验证前，不宣称生产级诊断准确率。


## 17. v3.3 增量架构与执行契约

本节与已修订质量条款组成新版本设计；实现进度见 docs/DESIGN_V33_IMPLEMENTATION_PLAN.md，不能将设计状态当作已经交付。

### 17.1 字段目录与实际流数

用户 Markdown 作为字段语义输入，经校验生成带源哈希、行号及冲突报告的结构化目录；JSON 仍管理解析类型和别名，不独立重复维护责任归属。空白不覆盖有效说明；重复同名条目冲突不能自动合并。范围写法统一展开，保留原出处，检索按字段或字段族组织。

ueLayerNum 为有效流筛选依据，selLayerNum 不替代。空、0、负数、非整数、不支持值属于异常数据并整行排除。8 流四列输入保留实际流数并报告“仅覆盖前四流”。有效配置内的空值和零值不推断未激活；逐记录筛选，支持同一会话 1→2→4。各流均值独立分母，成对差值必须同记录成对有效。缺测分母只含配置内可观察流。

instBfSinrGain 及其分子/分母归 L1测量；userNum 是任务内码分用户个数，不等同于整个 Slot 用户总数。责任层与 SCALAR/STREAM/BEAM 域独立；L1内部字段不全是算法决策。未填写归属标为待确认，未经确认的示例门限不自动用于判故障。isRsrp 有效索引小于 rxAntNum。

### 17.2 问题需求与覆盖

需求保存问题相关性、指标、维度、目的、范围、文档版本和出处。仅当前问题相关需求进入核账，RSRP/NI/增益均非强制项。Top-10 只影响显示，不能缩小问题 Scope。字段目录分别记录存在性、有效性和查询状态；全空、解析失败、范围无有效值、未查、失败、不支持不混同。

覆盖必须关联成功证据及核查目的，SQL 出现字段不代表完成分析。零行结果可标记执行成功但证据不足；缺少业务准入记录时仅返回质量事实。旧的增益全空全局解释阻断移除。

### 17.3 受控补查与案例路径

LangGraph 扩展为检索→需求整理→计划与字段来源校验→执行→覆盖核账→必要补查→验证→解释。使用现有 QueryPlan 算子及安全 SQL 边界。限制总时间、轮数、字段批量、产物数；结构修复与补查分别计数，共享总预算。无进展、预算耗尽、重复失败或不支持时终止，发布部分事实及未覆盖原因。

检查点只保存需求、预算和不可变产物引用，恢复不重置预算或重复已完成工作。相似案例先展示再验证必要判据；不符合或证据不足转探索，不自动确认根因。保留特性、人工审核与回放能力。版本冲突时由用户确认，不静默覆盖人工指定。

### 17.4 同侪比较及责任解释

仅用户明确要求时启用同侪比较，模型不得自行扩大。按 logicCellId、分钟窗、相同 RB 区间、Slot、起始符号 ueSymPos 匹配，排除目标 UE；候选同样执行业务准入。缺少维度不得放宽匹配。自然分钟分桶作为当前实施默认，明确事件时间基准；多符号覆盖不能确认时排除比较并披露原因。基线需列聚合方式及各 UE 样本量；空间位置差异未受控，比较不证明因果。

四类责任主体只形成有依据的排查方向。未核查不等于正常/免责，四流倒序不直接判故障，全空不推断采集故障。

### 17.5 输出兼容、版本与性能

模型输出仅允许原始 JSON 对象或完整单个 JSON Markdown 代码块；去壳后严格解析。artifact: 或 artifact：仅作引用包装，规范化后仍须匹配本轮完整 ID 并校验数值、单位和行键。表号只作显示别名，禁止猜测截短 ID。记录规范化动作及拒绝位置。

新 ETL 与统计策略使用独立版本及内容身份，旧数据/会话只读保留，新结果重新加载计算。按需过滤补充字段再聚合，禁止默认全量 pivot。以 440 字段、约 3400 万行长表验证耗时、RSS、超时及恢复，不承诺未经验证的固定性能。具体分步验收和发布回退见实施计划。


### 17.6 本轮实现边界与验证

本轮实现与测试记录见 docs/V33_DELIVERY.md。核账目前针对明确指标及相关结构化 required_metrics，不声称自动理解全部自然语言判据。案例 criteria 使用带出处的 FeatureCondition；没有可执行判据时仅作参考。补查累计预算不包含独立模型 HTTP 重试的端到端保证。同侪只核对起始符号，已知多符号不纳入；缺少符号数量仍不能证明完整覆盖一致。所有未确认门限保持未知。
