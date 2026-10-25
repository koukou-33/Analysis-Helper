# 性能记录

正常加载和分析会自动追加到项目根目录下的 `workspace_storage/logs/performance.txt`。这是 UTF-8 文本文件，每行一个 JSON 对象，可用文本编辑器打开或逐行解析。日志已被 Git 忽略，不随代码推送。

记录加载、质量统计、基础统计、字段模式获取、范围准备、结构化查询、手工 SQL、模型调用，以及 Agent 检索/规划/执行/验证/解释和完整诊断。阶段完成或异常退出时追加一行；强制结束进程或断电可能丢失未完成阶段的记录。

| 字段 | 含义 |
| --- | --- |
| timestamp_utc | 阶段开始时间，ISO UTC；北京时间加 8 小时 |
| trace_id / span_id | 嵌套操作共享追踪 ID / 阶段独立 ID |
| stage / pid | 阶段 / 进程 ID |
| elapsed_seconds | 耗时，包含子阶段，不可直接相加 |
| status / error_type | OK 或 ERROR；错误只记类型，不记异常消息 |
| rss_start_bytes / rss_end_bytes | 开始和结束时的整个进程驻留内存 |
| rss_sampled_peak_bytes | 开始、结束及每 100 ms 采样的最大驻留内存，可能漏掉短时峰值 |
| input_rows / input_bytes / max_input_columns | 成功加载的 CSV 行数 / 快照文件字节总数（含重复项）/ 最大文件列数 |
| supplemental_numeric_rows / max_supplemental_fields | 成功导入的补充长表行数 / 单文件最大补充字段数 |
| database_bytes | 加载完成后的 DuckDB 文件大小 |
| scoped_rows / requested_field_count | 基础统计范围行数 / 请求字段数（包括基础列；ALL 表示全列） |
| artifact_count / output_rows | 查询产物数 / 各产物行数之和，可能包含重复证据行 |
| dataset_id / session_id / action_id | 加载或完整诊断成功返回时的关联标识 |
| dataset_status / analysis_status | 实际业务状态；没有抛异常不代表全部子查询成功 |

内存包含 DuckDB 等原生分配以及同进程其他会话，不是单查询独占内存，也不包括独立图像导出子进程。Windows 使用系统进程计数器，Linux 使用 `/proc/self/statm`；无法读取时为 null。

补充行数在导入过程中累计，不新增全表扫描。旧数据集不会补写历史加载指标；日志只记录启用后的操作。不记录 API 密钥、提示词、模型输出、原始 CSV、SQL 或参数。

使用文件锁串行追加，支持多线程/多进程。写入失败时发出不含敏感信息的警告，保留业务结果。日志持续追加，长期运行可在停服后归档文件；后续操作自动重建。

加载真实数据后分别运行基础统计、单字段及多字段分析，按 `trace_id` 筛选日志，比较各阶段耗时、请求字段数、输出行数和内存。模型与数据库耗时分开查看，不累加嵌套阶段。
