# 启动、使用与恢复

所有命令从仓库根目录运行。推荐 Python 3.12；锁文件按 Windows / Python 3.12 实际安装环境生成。

## 安装与启动

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.lock
.venv\Scripts\python -m streamlit run app.py --server.address 127.0.0.1
```

本机开发环境已安装在 `.venv`；可以直接运行 `scripts/Start-Workbench.ps1`。访问 http://127.0.0.1:8501 。工作台仅监听本机；不建议在缺少访问控制的情况下对外暴露。

## 首次使用

1. 左侧选择本地 CSV 文件/目录和输入契约，加载一个固定数据版本；也可从已有数据集新建会话。
2. 查看质量报告，确认完整/部分加载、流控剔除、结构及解析问题。
3. 设置小区、CRNTI、会话、资源和时间范围，输入问题并开始分析。
4. 未配置模型时执行确定性统计；配置后由 LangGraph 规划受控查询。右侧可以查看计划、SQL、参数、哈希和完整 Parquet。
5. 对特性分析，将符合模板的 Markdown 放在 `knowledge_assets/features`，点击同步，再选择特性文档。未定义完整生效规则时，不确认“本应生效”。
6. 从结果提炼案例草稿，补充场景/版本/差异/出处/根因状态，审核后发布。案例包保存在 `knowledge_assets/cases/<case_id>/rXXXX/`。

## 模型配置

复制 `.env.example` 为 `.env`。不预设服务或模型。`PROVIDER` 支持：

- `openai_compatible`：需要 API_KEY。
- `local_openai`：兼容接口的本地服务，允许无密钥。

BASE_URL 填接口前缀（如服务的 `/v1`），MODEL 填实际模型标识；LLM 与 EMBEDDING 独立配置。默认超时 60 秒、重试 2 次。修改后重启工作台以刷新配置。视觉使用 LLM_SUPPORTS_VISION=true 的主模型，或单独配置 VISION_* 并设置 VISION_SUPPORTS_VISION=true。存在历史图且当前 PNG 成功导出后才进行定性比对。

PNG 导出使用 Kaleido 和本机 Chrome，单图渲染预算 20 秒、独立进程总预算 30 秒；超时会终止本次导出进程树，环境不可用时保留 HTML 交互图。程序不会自行下载浏览器。

## 命令行

```powershell
.venv\Scripts\python -m srs_helper.cli generate workspace_storage/new_normal --profile normal
.venv\Scripts\python -m srs_helper.cli load workspace_storage/new_normal --contract config/sample_input_contract.json
.venv\Scripts\python -m srs_helper.cli quality <数据集目录>
.venv\Scripts\python -m srs_helper.cli summary <数据集目录> --cell 0
.venv\Scripts\python -m srs_helper.cli knowledge-sync
.venv\Scripts\python -m srs_helper.cli diagnose <数据集目录> "比较四流增益"
.venv\Scripts\python -m srs_helper.cli sessions
```

`diagnose --session-id <UUID>` 在同一数据版本继续会话。CLI 输出会话与操作 ID；完整响应、图表数据和计划结果位于 sessions/<UUID>/artifacts。随机生成默认需要本地示例表头，可通过 --template 指定另一份相同字段契约的表头。

## 恢复与故障

- 历史会话绑定固定数据库及数据指纹，恢复不重读 CSV。改变原目录不会改变旧结果。
- session_registry.sqlite 同时保存独立命名的 registry_* 表与 LangGraph 检查点表。备份时关闭应用，并一起复制数据集目录、sessions 及该 SQLite 文件。
- 证据缺失/哈希不一致会明确提示。不要手工覆盖证据；重新分析产生新 artifact。
- 模型鉴权失败不重试；限流/暂时错误有限重试。失败后保留原有结果。
- 案例文件保存成功但索引失败时可再次审核提交同一内容，或同步知识源重建检索。Markdown/图片是权威源，索引可删除后重建。

## 测试

```powershell
.venv\Scripts\python -m unittest discover -s tests -v
```

若沙箱阻止系统临时目录，可将 TEMP/TMP 指向仓库内已忽略的 `workspace_storage/test-tmp` 后再运行。全部测试使用合成数据和模拟 HTTP；不会向实际模型服务发送数据。


## SPF 与重复列升级（2026-10-27）

更新后重新加载 CSV 为 m1.5，创建或选择对应新数据会话。同步知识源，在特性文档中选择“SPF特性相关分析指南”，再询问 SPF 是否生效或未生效原因。程序直接执行文档结构化规则；条件未知和实际标志未知单列，不当作未生效。历史 m1.4 可保留，但没有新重复列合并结果，不宜用于验证这次 SPF 改动。字段间比较写 value_field，不将字段名填成 value。观察 DUPLICATE_VALUE_CONFLICT 时应核查同名非空值冲突，不默认取首列。跨数据集 A/B 比较本期不启用。


## 模型空输出和截断诊断（AH-010）

更新重启后按同一问题复验，从 performance.txt 按trace_id查找model_response；purpose区分plan/explain/embedding，error_code、finish_reason、content_type/length、usage和HTTP状态用于定位，不需要提供密钥或原始响应正文。

默认不改变服务端token预算。确认实际端点支持后，LLM_OUTPUT_TOKEN_PARAMETER选择max_tokens或max_completion_tokens，分别设置LLM_PLAN_OUTPUT_TOKENS、LLM_EXPLAIN_OUTPUT_TOKENS；若启用LLM_STRUCTURED_RETRIES=1，LLM_RECOVERY_OUTPUT_TOKENS必须大于首轮预算才会重试。数值需符合实际模型限额，未知时保持默认0。LLM_CALL_BUDGET_SECONDS限制单次chat内HTTP重试和恢复的累计调度时间，不代表全分析硬时限。不要将推理正文当成最终答案。

模型失败仍保留确定性条目报告；model_status与业务status分开。PARTIAL_FACTS_READY可能表示需求尚未识别完整，不等于所有统计失败。生产验证与限制见P0_P1_DELIVERY.md。


### 智谱直连 glm-5.3-flash（已核对能力）

官方来源：[模型说明](https://docs.bigmodel.cn/cn/guide/models/vlm/glm-5.3-flash)、[Chat API](https://docs.bigmodel.cn/api-reference/模型-api/对话补全)。该模型不支持关闭思考；可设置low/high/max推理强度，输出上限参数为max_tokens。本项目提供显式bigmodel_glm53flash能力配置，仅用于https://open.bigmodel.cn/api/paas/v4和glm-5.3-flash，其他端点不自动套用。

本次本地采用以下起始配置，预算并非生产性能保证；降低推理强度后仍需在真实问题上验证规划和解释质量：

```dotenv
LLM_PROVIDER=openai_compatible
LLM_BASE_URL=https://open.bigmodel.cn/api/paas/v4
LLM_MODEL=glm-5.3-flash
LLM_CAPABILITY_PROFILE=bigmodel_glm53flash
LLM_REASONING_EFFORT=low
LLM_OUTPUT_TOKEN_PARAMETER=max_tokens
LLM_PLAN_OUTPUT_TOKENS=8192
LLM_EXPLAIN_OUTPUT_TOKENS=8192
LLM_RECOVERY_OUTPUT_TOKENS=16384
LLM_STRUCTURED_RETRIES=1
LLM_TIMEOUT_SECONDS=120
LLM_CALL_BUDGET_SECONDS=180
LLM_MAX_RETRIES=0
```

LLM_API_KEY仅在本地.env填写，不能提交。重启工作台后读取新配置；已有进程环境变量优先于.env。embedding仍需独立可用模型，不用glm-5.3-flash代替向量模型。

最小连通性测试使用4096输出预算、low、无重试，只请求返回ok JSON：成功，finish_reason=stop、content长度12、prompt_tokens=28、completion_tokens=7。该结果仅确认认证与参数组合，不能替代复杂QueryPlan和真实数据解释验证。


## 连续追问与原始记录溯源

新版本在左侧提供“追问来源”和“继承的记录群体”，明确继承总体、生效、未生效、未知或某条件失败的记录；范围及版本必须与父分析一致。追问某次变化时填写完整event_id，仅继承前后记录。不从聊天文字猜群体，不自动扩展为相关用户全部记录。查询证据页可按本轮record_id回查并核验快照；用户级原因占比仍未启用。完整步骤、测试及规模限制见[P2～P6开发记录](P2_P6_DELIVERY.md)。


## 持续查询验证（2026-10-28）

重启工作台加载新代码及知识源。询问“SPF未生效原因”后，可看到建立任务、规划、追加验证和评估多个阶段；最终答案列出明确谓词获得支持/被反驳/未定，原始模型猜测不会直接作为已验证解释发布。只询问“SPF生效比例”不自动扩展原因。当前为记录谓词循环，保持用户级原因比例暂缓和同侪比较需显式请求的约束。

在查询证据中展开“持续验证任务、停止原因及缺口”，查看validation_tasks、assessments、missing_information、query_ledger、residual_cohorts。LOOP_FIELD_INVENTORY说明字段目录、映射和目标群体缺测/非法值依据；引用可下载完整Parquet。保留action_id、verification_loop和performance.txt用于现场反馈，不需要发送API密钥。

- GOALS_VERIFIED：本问题范围的核验完成，不等于根因或责任已确认。
- DATA_INSUFFICIENT / CRITERIA_REQUIRED：按缺口清单补字段、映射、有效数值或明确规则。
- TECHNICAL_FAILURE / BUDGET_EXHAUSTED / NO_PROGRESS：任务未完成，不能解释为数据不存在。模型不能生成任务也不会伪装成业务判据缺失。

追加查询默认最多12次、验证规划最多6次；全部规划/验证/解释模型调用默认合计12次，配置分别为SRS_VERIFY_MAX_QUERIES、SRS_VERIFY_MAX_PLANNER_CALLS、SRS_ANALYSIS_MAX_MODEL_CALLS，范围见.env.example。整体仍受SRS_ANALYSIS_BUDGET_SECONDS限制；重试不重置同一动作预算。已发布的部分结果不可变，调整配置后重新提问创建新动作。进程中断后的“重试上次未完成操作”复用已完成查询。

合成规模复验可运行 `.venv\Scripts\python.exe -m scripts.benchmark_verification --dataset <已有合成数据集目录>`；可加 `--long-table workspace_storage/scope_benchmark/bench.duckdb` 读取已有独立长表基准。该脚本使用离线模拟规划器，不调用真实API，不重新导入数据。现场验证仍应覆盖实际GLM响应及完整大表组合。


## 数据命名与删除

加载CSV时填写“数据名称”（可选，最多100字符）；留空会按来源名称和时间自动命名。“已有数据管理”按名称与版本ID展示数据，可创建新诊断会话或修改名称；旧数据也可直接命名，无需重新导入。

删除前核对选中版本及关联会话数量，勾选“确认删除此数据集及其输入快照”后点击“删除数据集”。这会删除应用存储的数据库和CSV输入快照，保留原始来源文件、历史报告和已生成证据。删除后历史会话标记数据不可用，不能继续查询和原始行回查；需要分析时重新加载源CSV。分析占用时删除被拒绝；其他程序占用文件或删除失败时，请释放占用后重试，不将错误提示视为已删除。


## 小区级参数连续补齐

在knowledge_assets/field_mappings/nr_srs_field_mapping.md新增的“数据层级”列维护字段粒度。当前三个SPF参数已标为“小区级”；其他字段留空，不自动猜测。L1/L2/L3“归属”不等于数据层级。

更新后重启工作台并重新加载CSV，已有不可变数据集不会自动补齐。新数据按同一logicCellId的时间序列，用前后最近的相同可信值填补中间缺失，不限时间间隔；不同值、冲突和单侧缺失仍未知，不取全局众数。同时间唯一有效值可共享给该时刻缺失记录，多个不同值不按票数决定。

SPF答案会说明有多少记录使用小区参数推定值，并保留仅原始观测下的预期计数。查询证据内__cell_raw_为原始值，__cell_fill_为前后record_id、时间、支持数和方法；来源record_id可在原始记录回查入口核验。修改层级或补齐策略需重新加载，不能将历史推定结果当作新版本证据。原始CSV、异常记录排除口径和实际生效flag不因这三个参数补齐而改写。
