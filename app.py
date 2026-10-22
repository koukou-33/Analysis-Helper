"""Run with: python -m streamlit run app.py"""
import json
import re
from numbers import Number
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pandas as pd
import plotly.express as px
import pyarrow.parquet as pq
import streamlit as st

from srs_helper.agent import DiagnosticEngine, restore_scope
from srs_helper.cases import create_draft, publish_case
from srs_helper.database import open_dataset, quality_report
from srs_helper.evidence import verify_artifact
from srs_helper.knowledge import inspect_image, safe_asset, parse_document
from srs_helper.pipeline import build_dataset
from srs_helper.queries import Scope
from srs_helper.sessions import dumps
from srs_helper.visual import export_chart

st.set_page_config(page_title="SRS 诊断工作台",layout="wide")
st.title("5G NR SRS 诊断工作台")
st.caption("确定性统计 · 可追溯证据 · 人工审核案例")


@st.cache_resource
def get_engine():
    return DiagnosticEngine()


engine = get_engine()
left,middle,right = st.columns([1,1.3,1.7])


def quality_style(frame):
    def highlight(row):
        def active(name):
            value = row.get(name,0)
            return isinstance(value,Number) and pd.notna(value) and value>0
        color = ""
        if active("has_parse_error") or active("parse_error_cnt"):
            color = "background-color: #ffe0e0"
        elif active("is_suspicious") or active("suspicious_cnt"):
            color = "background-color: #fff0c2"
        elif active("has_missing_measurement") or active("missing_cnt"):
            color = "background-color: #e4efff"
        return [color]*len(row)
    return frame.style.apply(highlight,axis=1)

with left:
    st.subheader("数据与范围")
    with st.expander("加载 CSV",expanded=False):
        with st.form("load"):
            input_path = st.text_input("目录或单个 CSV 路径",value="workspace_storage/synthetic_v1/normal")
            contract_path = st.selectbox("输入契约",["config/sample_input_contract.json","config/input_contract.json"])
            load = st.form_submit_button("加载新数据版本")
        if load:
            try:
                with st.spinner("固定快照并清洗…"):
                    path,meta = build_dataset(input_path,"workspace_storage/datasets",json.loads(Path(contract_path).read_text(encoding="utf-8")))
                if meta["status"]=="FAILED":
                    st.error("所有文件加载失败")
                    st.json(meta["files"])
                else:
                    sid = engine.store.create(path)
                    st.session_state["active_session"] = sid
                    st.success("数据已加载："+meta["status"])
            except Exception as exc:
                st.error("加载未完成："+type(exc).__name__+"。请检查路径、契约和文件内容。")
    sessions = engine.store.list()
    if sessions:
        session_ids = [s["id"] for s in sessions]
        names = {s["id"]:s["title"]+" · "+s["fingerprint"][:8]+" · "+s["accessed"][:19] for s in sessions}
        current = st.session_state.get("active_session",session_ids[0])
        sid = st.selectbox("历史诊断会话",session_ids,index=session_ids.index(current) if current in session_ids else 0,format_func=lambda v:names[v])
        st.session_state["active_session"] = sid
    else:
        sid = None
    with st.expander("从已有数据集创建会话"):
        datasets = sorted(Path("workspace_storage/datasets").glob("*/analysis.duckdb"))
        choices = [str(p.parent) for p in datasets if not p.parent.name.startswith(".building-")]
        if choices:
            selected_dataset = st.selectbox("固定数据版本",choices,format_func=lambda p:Path(p).name[:24])
            if st.button("创建诊断会话"):
                try:
                    st.session_state["active_session"] = engine.store.create(selected_dataset)
                    st.rerun()
                except Exception:
                    st.error("该数据版本未就绪或不兼容。")
    st.caption("模型未配置时仍可执行全部确定性统计。")
    if st.button("同步知识源"):
        st.json(engine.index.sync())

if sid is None:
    middle.info("请加载 CSV 或从已有数据集创建会话。")
    st.stop()

session = engine.store.get(sid)
history = engine.store.history(sid)
completed = [h for h in history if "answer" in h]
last = completed[-1] if completed else None

with left:
    restored = last.get("scope",{}) if last else {}
    scope_valid = True
    try:
        cell = st.text_input("logicCellId（空为全部）",value=str(restored.get("logic_cell_id") or "") if restored.get("logic_cell_id")!=0 else "0",key=sid+"cell")
        crnti = st.text_input("CRNTI（空为全部）",value=str(restored.get("crnti") or ""),key=sid+"crnti")
        ue = st.text_input("ue_uid（空为全部）",value=restored.get("ue_uid") or "",key=sid+"ue")
        resource = st.text_input("resourceId（空为全部）",value="" if restored.get("resource_id") is None else str(restored["resource_id"]),key=sid+"resource")
        phy = st.text_input("phyCellInst（辅助筛选）",value="" if restored.get("phy_cell_inst") is None else str(restored["phy_cell_inst"]),key=sid+"phy")
        start = st.text_input("开始时间（含日期，包含）",value=restored.get("start") or "",key=sid+"start")
        end = st.text_input("结束时间（含日期，不包含）",value=restored.get("end") or "",key=sid+"end")
        scope = Scope(int(cell) if cell else None,ue or None,int(crnti) if crnti else None,int(resource) if resource else None,
                      datetime.fromisoformat(start) if start else None,datetime.fromisoformat(end) if end else None,int(phy) if phy else None)
        scope.where()
    except ValueError:
        st.error("筛选值无效；时间示例：2026-10-22T00:00:00")
        scope_valid,scope = False,Scope()
    feature_docs = [d for d in engine.index.documents() if d["kind"]=="feature"]
    feature_path = st.selectbox("特性文档（可选）",[None]+[d["path"] for d in feature_docs],format_func=lambda v:v or "仅描述性统计")
    try:
        with st.expander("数据质量与加载清单"):
            report = quality_report(session["dataset_dir"])
            st.json(report["baseline"])
            st.dataframe(pd.DataFrame(report["flow"]),hide_index=True)
            st.dataframe(pd.DataFrame(report["field_errors"]),hide_index=True)
            st.dataframe(pd.DataFrame(report["blocks"][:100]),hide_index=True)
            st.caption("块级预览最多 100 个块；完整信息可下载。")
            st.download_button("下载质量报告",dumps(report),file_name="quality_report.json")
            st.json(report["files"])
        dataset_ok = True
    except Exception:
        dataset_ok = False
        st.error("会话数据集不可用；仍可查看已保存历史。")


def run_question(question, plan_override=None,sql_override=None):
    action_id = str(uuid4())
    st.session_state["pending_action"] = {"sid":sid,"action_id":action_id,"question":question,
                                          "scope":scope.__dict__,"feature_path":feature_path,"plan_override":plan_override,"sql_override":sql_override}
    try:
        with st.status("正在分析",expanded=True) as status:
            engine.run(sid,question,scope,action_id,feature_path=feature_path,progress=lambda step:st.write(step),plan_override=plan_override,sql_override=sql_override)
            status.update(label="分析完成",state="complete")
        st.session_state.pop("pending_action",None)
        st.rerun()
    except Exception:
        st.error("本次操作未完成，已有证据保留。可显式重试。")


with middle:
    st.subheader("诊断与追问")
    with st.form("question_"+sid):
        question = st.text_area("问题描述",value="比较各流增益，查看异常比例与缺测情况。")
        submitted = st.form_submit_button("开始分析",disabled=not(scope_valid and dataset_ok))
    if submitted and question.strip():
        run_question(question)
    pending = st.session_state.get("pending_action")
    if pending and pending["sid"]==sid and st.button("重试上次未完成操作",disabled=not dataset_ok):
        try:
            engine.run(sid,pending["question"],restore_scope(pending["scope"]),pending["action_id"],retry=True,
                       feature_path=pending["feature_path"],plan_override=pending["plan_override"],sql_override=pending.get("sql_override"))
            st.session_state.pop("pending_action",None)
            st.rerun()
        except Exception:
            st.error("重试仍未完成，请检查数据或服务配置。")
    for item in completed[-10:]:
        with st.chat_message("user"):
            st.write(item["question"])
        with st.chat_message("assistant"):
            st.write(item["answer"])
            st.caption("范围："+dumps(item["scope"]))
            for notice in item["notices"]:
                st.warning(notice)
            for hypothesis in item.get("hypotheses",[]):
                st.info("待验证假设："+hypothesis["text"])
                st.caption("引用："+", ".join(hypothesis["references"]))
            for claim in item.get("claims",[]):
                st.write("已校验数值",claim)
            if item.get("visual"):
                visual = item["visual"]
                if visual["status"]=="COMPARED":
                    st.info("视觉比对（定性参考，不确认根因）")
                    st.json(visual["result"])
                else:
                    st.caption(visual.get("reason","本轮未执行视觉比对。"))
            for ref in item.get("references",[]):
                with st.expander("知识参考："+ref["title"]):
                    st.write(ref["excerpt"])
                    st.caption(ref["path"]+" @ "+ref["version"][:12])
                    st.caption("相似点：检索词匹配；差异与适用条件尚需验证。参考不等于根因确认。")
                    archive = engine.index.storage/"versions"/ref["version"]
                    if (archive/"document.json").exists():
                        doc = json.loads((archive/"document.json").read_text(encoding="utf-8"))
                        for im in doc["images"]:
                            try:
                                st.image(str(safe_asset(archive,im["path"])),caption="历史参考图："+im["caption"])
                            except Exception:
                                st.warning("历史参考图片不可用。")
            if item.get("missing_artifacts"):
                st.warning("部分证据文件缺失；可重新分析生成新证据，旧引用不覆盖。")

with right:
    st.subheader("证据画布与案例审核")
    if not completed:
        st.info("完成一次分析后显示结果与审核入口。")
    else:
        result = st.selectbox("历史结果",completed,format_func=lambda r:r["question"][:45]+" · "+r["action_id"][:8],index=len(completed)-1)
        st.caption("数据版本："+result["dataset_fingerprint"][:16]+"；当前画布范围："+dumps(result["scope"]))
        panel = st.radio("视图",["统计与图表","查询证据","特性条件","案例审核"],horizontal=True,key="panel_"+sid)
        if panel=="统计与图表":
            summary = result["summary"]
            st.metric("流控有效记录",summary["flow_valid_count"])
            groups = pd.DataFrame(summary["groups"])
            if not groups.empty:
                show_all = st.checkbox("展示全部会话",value=False)
                if not show_all:
                    active = groups.groupby("ue_uid")["sample_cnt"].sum().nlargest(10).index
                    groups = groups[groups.ue_uid.isin(active)]
                    st.caption("默认按当前范围记录数展示 Top-10 活跃会话，并非异常排名；统计已按完整范围计算。")
                st.dataframe(quality_style(groups),hide_index=True)
                st.caption("黄色：含可疑值；红色：含解析错误；蓝色：缺测。颜色不改变统计样本。")
                st.download_button("下载完整 UE/资源统计",pd.DataFrame(summary["groups"]).to_csv(index=False).encode("utf-8-sig"),"ue_summary.csv")
                with st.expander("小区时频统计（按当前范围重算）"):
                    st.dataframe(pd.DataFrame(summary.get("cell_tf",[])),hide_index=True)
                streams = pd.DataFrame(summary["streams"])
                streams = streams[streams.ue_uid.isin(groups.ue_uid)]
                streams["对象"] = streams.ue_uid+" / R"+streams.resourceId.astype(str)
                chart_type = st.selectbox("图表",["流均值折线","流均值热力图","RSRP 时间曲线"])
                if chart_type=="流均值折线":
                    fig = px.line(streams.sort_values("streamId"),x="streamId",y="gain_avg",color="对象",markers=True,
                                  labels={"streamId":"流编号（0–3）","gain_avg":"增益算术均值（dB）"})
                elif chart_type=="流均值热力图":
                    matrix = streams.pivot(index="对象",columns="streamId",values="gain_avg")
                    fig = px.imshow(matrix,labels={"x":"流编号","y":"对象","color":"增益（dB）"},aspect="auto")
                else:
                    time_artifacts = [a for a in result["artifacts"] if a["operator"]=="TIME_SERIES"]
                    if time_artifacts:
                        try:
                            verify_artifact(time_artifacts[0])
                            batches = pq.ParquetFile(time_artifacts[0]["path"]).iter_batches(batch_size=2000)
                            batch = next(batches,None)
                            timed = batch.to_pandas() if batch else pd.DataFrame()
                            if not timed.empty:
                                timed["对象"] = timed.ue_uid+" / R"+timed.resourceId.astype(str)
                                fig = px.line(timed,x="time_bucket",y="rsrp_avg",color="对象",labels={"time_bucket":"日期时间（1 秒桶）","rsrp_avg":"RSRP（dBm）"})
                            else:
                                fig = px.line()
                            st.caption("时序预览最多 2000 个分组秒桶，保留完整日期；完整时序结果在查询证据中下载。")
                        except Exception:
                            fig = px.line()
                            st.warning("时序证据不可用。")
                    else:
                        fig = px.line()
                        st.info("此历史结果没有时序证据，请新建一次分析。")
                st.plotly_chart(fig,use_container_width=True)
                st.caption("当前数据统计；开发生成的 CSV 属于模拟测量。各流独立均值，缺测不补零。")
                st.download_button("下载交互图",fig.to_html(include_plotlyjs=True),"stream_gain.html")
                if st.button("导出当前流增益图 PNG"):
                    with st.spinner("生成静态图…"):
                        chart = export_chart(summary,engine.store.directory(sid)/"artifacts")
                    st.session_state["export_chart_"+result["action_id"]] = chart
                chart = st.session_state.get("export_chart_"+result["action_id"])
                if chart:
                    if chart["status"]=="READY":
                        st.download_button("下载 PNG",Path(chart["png"]).read_bytes(),"stream_gain.png",mime="image/png")
                    else:
                        st.warning(chart.get("reason","当前没有图表数据。"))
                st.dataframe(streams,hide_index=True)
        elif panel=="查询证据":
            st.json(result["plan"])
            for artifact in result["artifacts"]:
                with st.expander(artifact["operator"]+" · "+artifact["step_id"]):
                    st.caption(f"完整行数 {artifact['row_count']}；预览最多 100 行；SHA256 {artifact['sha256']}")
                    st.code(artifact.get("sql","内部条件逐记录关系"),language="sql")
                    st.write("绑定参数",artifact.get("parameters",[]))
                    try:
                        verify_artifact(artifact)
                        st.dataframe(quality_style(pd.DataFrame(artifact["preview"])),hide_index=True)
                        st.download_button("下载完整 Parquet",Path(artifact["path"]).read_bytes(),Path(artifact["path"]).name,key=artifact["artifact_id"])
                    except Exception:
                        st.error("证据缺失或哈希不匹配。")
                    pin_key = "pin_"+artifact["artifact_id"]
                    pinned = st.checkbox("置顶标记（不提升证据等级）",value=engine.store.ui_value(sid,pin_key,False),key=pin_key)
                    engine.store.set_ui_value(sid,pin_key,pinned)
            with st.form("edit_plan_"+sid):
                plan_text = st.text_area("修改结构化查询计划（新建证据，不覆盖旧结果）",value=json.dumps(result["plan"],ensure_ascii=False,indent=2),height=220)
                rerun_plan = st.form_submit_button("执行修改后的计划",disabled=not(scope_valid and dataset_ok))
            if rerun_plan:
                try:
                    plan = json.loads(plan_text)
                    run_question("手动计划："+result["question"],plan)
                except ValueError:
                    st.error("计划 JSON 无效。")
            with st.form("manual_sql_"+sid):
                sql_text = st.text_area("受控只读 SQL",value="SELECT logicCellId, AVG(usrRsrp) AS rsrp_avg, COUNT(*) AS sample_cnt FROM resource GROUP BY logicCellId")
                sql_params = st.text_input("绑定参数（JSON 列表，SQL 中使用 ?）",value="[]")
                execute_manual = st.form_submit_button("执行探索 SQL",disabled=not(scope_valid and dataset_ok))
            if execute_manual:
                try:
                    run_question("手动 SQL 探索",sql_override={"sql":sql_text,"parameters":json.loads(sql_params)})
                except ValueError:
                    st.error("参数 JSON 无效。")
            st.caption("仅允许当前范围的 resource/stream 和受控 CTE；探索结果不自动确认特性或根因。")
        elif panel=="特性条件":
            if result.get("feature"):
                st.json(result["feature"])
            else:
                st.info("选择有效特性文档后分析；未选择时不推断特性生效条件。")
        else:
            if st.button("从当前结果提炼案例草稿"):
                selected_chart = st.session_state.get("export_chart_"+result["action_id"])
                st.session_state["draft_folder"] = str(create_draft(engine.store,sid,{**result,"selected_chart":selected_chart}))
            draft_folder = st.session_state.get("draft_folder")
            if draft_folder:
                draft_dir = Path(draft_folder)
                draft_file = next(draft_dir.glob("*.md"))
                with st.form("review_"+draft_dir.name):
                    edited = st.text_area("YAML 元数据与 Markdown 正文",draft_file.read_text(encoding="utf-8"),height=400)
                    image_file = st.file_uploader("附加图片（PNG/JPEG/WebP）",type=["png","jpg","jpeg","webp"])
                    caption = st.text_input("图片说明")
                    reviewer = st.text_input("审核人")
                    approval = st.checkbox("我已核对来源、适用条件、图表及根因状态，同意发布该案例版本")
                    save_draft = st.form_submit_button("保存草稿")
                    publish = st.form_submit_button("审核并发布")
                if save_draft or publish:
                    try:
                        if image_file:
                            images = draft_dir/"images"
                            images.mkdir(exist_ok=True)
                            image_path = images/(uuid4().hex+Path(image_file.name).suffix.lower())
                            image_path.write_bytes(image_file.getvalue())
                            inspect_image(image_path)
                            edited += f"\n\n![{caption.replace(']','')}](images/{image_path.name})\n"
                        draft_file.write_text(edited,encoding="utf-8")
                        if publish:
                            saved = publish_case(engine.index,draft_file,reviewer,approved=approval)
                            st.success("文件已保存；索引状态："+saved["status"])
                        else:
                            st.success("草稿已保存。")
                    except Exception as exc:
                        st.error("保存未完成："+str(exc))
                with st.expander("正文预览"):
                    try:
                        preview = parse_document(draft_file,"case")
                        st.markdown(re.sub(r"!\[[^\]]*\]\([^)]+\)","",preview["body"]))
                        for image in preview["images"]:
                            st.image(str(safe_asset(draft_dir,image["path"])),caption=image["caption"])
                        for warning in preview["warnings"]:
                            st.warning(warning)
                    except Exception:
                        st.info("请先保存有效草稿以预览正文。")
