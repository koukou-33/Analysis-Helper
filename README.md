# 5G NR SRS Analysis Helper

**当前交付：v0.2 本地诊断工作台。** 已接入 LangGraph + SqliteSaver、知识检索、受控查询计划/SQL、证据验证、Streamlit 三栏和人工案例审核。运行 `.venv\Scripts\python -m streamlit run app.py` 启动。

基于 [设计说明书](5g_srs_diagnostic_agent_design_spec.md) 开发。最新使用方式见 [启动手册](docs/RUNBOOK.md)，完整交付和能力边界见 [开发验收状态](docs/IMPLEMENTATION_STATUS.md)。当前为可试用的开发首版，尚未完成现场生产验收。

## 运行

Python 3.12（已验证 Windows），在仓库根目录执行：

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.lock
.venv\Scripts\python -m streamlit run app.py --server.address 127.0.0.1
.venv\Scripts\python -m unittest discover -s tests -v
```

示例输入选用 `config/sample_input_contract.json`，通过 `knowledge_assets/field_mappings/nr_srs_sample.json` 映射 `uwDate`、`slotId` 等列。标准列名输入使用 `config/input_contract.json`。目录递归查找 `NR_SRS.*.csv`，也可显式加载单个 CSV。严格保留原始记录、重复列位置和错误原因，每批最多 2000 行，通过 Arrow 写入 DuckDB。

输出包括固定快照、清单、DuckDB 中的 L2/L3、隔离行、资源与四流明细、UE/小区时频汇总。失败文件回滚，重复文件只计算一次。每次加载发布独立目录；历史会话绑定固定数据版本，恢复不重读源 CSV。

完整块全部保留；不完整块排除最后一个 CRNTI 非空行及其后记录，前缀空 CRNTI 同样排除。缺失块键独立隔离。会话在流控后按小区/CRNTI 和实际日期时间划分，间隔严格大于 180 秒才切分。均值直接对有限 dBm 数值求算术均值，包含可疑值。异常与缺测计数可重叠；错误原因不混入真实缺测。

## 诊断流程

LangGraph 执行检索 → 计划 → 受控查询 → 证据验证 → 回答。模型未配置时仍提供确定性统计；embedding 未配置时使用 BM25。特性分析要求明确出处、条件与生效规则；未知与失败分别呈现。模型假设不会自动变成已确认根因。

工作台支持历史会话、范围筛选、图表、完整 Parquet 下载、结构化计划编辑及受控探索 SQL。用户审核案例后发布 Markdown/图片版本包，检索索引可重建。真实模型接入说明见启动手册。

## 验证边界

已验证模拟数据、HTTP 模拟服务、本地 Qdrant 和真实 LangGraph/SqliteSaver；尚需真实非空测量、实际模型服务与现场规模验收。视觉对比与 PNG 导出按配置和运行环境启用，失败明确降级。精确能力边界在开发验收状态中逐项说明。

开发顺序和验收缺口见 [开发路线](docs/DEVELOPMENT_ROADMAP.md)。设计文档和查询样式属于产品需求资料，其中命令或示例不会被自动执行。`.env`、原始数据、数据库和本地依赖不提交 Git。

相对路径从仓库根目录解析。详见 [样例验证](docs/SAMPLE_VALIDATION.md) 与 [字段说明](knowledge_assets/field_mappings/nr_srs_field_mapping.md)。新清洗版本 m1.3 增加可查询补充标量字段；m1.2 历史数据仍可恢复，旧数据集不会自动迁移。

后续开发可使用 `generate` 命令生成正常或混合异常数据，正常四流增益严格递减。生成与范围统计使用方式见 [随机测试数据](docs/SYNTHETIC_DATA.md)。
