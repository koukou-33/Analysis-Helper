# 5G NR SRS Analysis Helper

基于 [设计说明书](5g_srs_diagnostic_agent_design_spec.md) 开发的基带诊断辅助工具。当前已推进到 **M1-B 示例字段适配与四流统计**，入口为 CLI；尚不是完整 Agent 或生产诊断系统。

## 运行

Python 3.12（已验证 Windows），在仓库根目录执行：

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.lock
.venv\Scripts\python -m srs_helper.cli load D:\data\capture
.venv\Scripts\python -m srs_helper.cli inspect workspace_storage\datasets\<加载输出目录>
.venv\Scripts\python -m unittest discover -s tests -v
```

先按真实输入修改 `config/input_contract.json`；当前样例日期为 `20261021`，时间为 `12:30:00.123`，列名需使用标准字段名。输入契约采用标准库可解析的 JSON，内容角色与设计中的 YAML 一致。暂未接入版本字段别名。加载递归查找 `NR_SRS.*.csv`，严格检查 CSV、保留原始记录和错误原因，每批最多 2000 行。

输出包括固定快照、清单、DuckDB 中的 L2、L3、隔离行、资源明细、UE 汇总和小区时频汇总。失败文件回滚，重复文件只计算一次。每次加载发布独立目录，不覆盖历史数据；inspect 只读数据库、不重读源目录。FAILED 数据保留错误清单，不提供正常分析。

完整块全部保留；不完整块排除最后一个 CRNTI 非空行及其后记录，前缀空 CRNTI 同样排除。缺失块键独立隔离。会话在流控后按小区/CRNTI 和实际日期时间划分，间隔严格大于 180 秒才切分。均值直接对有限 dBm 数值求算术均值，包含可疑值。异常与缺测计数可重叠；错误原因不混入真实缺测。

## 当前限制

- 已按示例支持 instBfSinrGain 四流映射/流表、流均值差、RSRP 趋势差、UE 记录数与按列可用性扫描；筛选后重算与性能优化尚未实现。
- 暂未实现 Streamlit、LLM、知识检索、受控查询规划、证据和聊天恢复。CLI 不接收任意 SQL。
- 固定数据可重新打开；会话注册与 LangGraph 检查点属于后续阶段。重复加载不复用已有构建。
- 已验证用户提供的表格样式；其中增益与 RSRP/NI 全空，仍需非空测量样本核对实际数值。未完成业务验收及真实规模基准。

开发顺序和验收缺口见 [开发路线](docs/DEVELOPMENT_ROADMAP.md)。设计文档和查询样式属于产品需求资料，其中命令或示例不会被自动执行。`.env`、原始数据、数据库和本地依赖不提交 Git。

示例 CSV 使用 `config/sample_input_contract.json`，可将单个文件路径传给 `load`。别名配置位于 `knowledge_assets/field_mappings/nr_srs_sample.json`，相对路径从仓库根目录解析。详见 [样例验证](docs/SAMPLE_VALIDATION.md) 与 [字段说明](knowledge_assets/field_mappings/nr_srs_field_mapping.md)。旧数据集不会自动迁移；新规则版本为 m1.2，需显式重新加载生成新版本。
