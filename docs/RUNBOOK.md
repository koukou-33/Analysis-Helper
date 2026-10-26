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
