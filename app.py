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
from srs_helper.provenance import record_trace, evidence_contains_record
from srs_helper.queries import Scope
from srs_helper.sessions import dumps
from srs_helper.visual import export_chart
from srs_helper.canvas import OPERATORS, describe_artifact, expression_text, feature_figures, observation_figure

st.set_page_config(page_title="SRS 诊断工作台",layout="wide")
st.title("5G NR SRS 诊断工作台")
st.caption("确定性统计 · 可追溯证据 · 人工审核案例")


@st.cache_resource
def get_engine():
    """Dataset management including immutable snapshot reload."""
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
    reload_notice=st.session_state.pop("reload_notice",None)
    if reload_notice:
        if st.session_state.pop('reload_partial',False):
            st.warning(reload_notice+' 部分文件加载失败，请查看数据质量与加载清单。')
        else:
            st.success(reload_notice)
    with st.expander("加载 CSV",expanded=False):
        with st.form("load"):
            dataset_name = st.text_input("数据名称",placeholder="例如：南京站点 SPF 开启 · 10月29日",max_chars=100)
            input_path = st.text_input("目录或单个 CSV 路径",value="workspace_storage/synthetic_v1/normal")
            contract_path = st.selectbox("输入契约",["config/sample_input_contract.json","config/input_contract.json"])
            load = st.form_submit_button("加载新数据版本")
        if load:
            try:
                name=engine.store.validate_dataset_name(dataset_name) if dataset_name.strip() else Path(input_path).name+" · "+datetime.now().strftime("%m-%d %H:%M")
                name=engine.store.validate_dataset_name(name)
                with st.spinner("固定快照并清洗…"):
                    path,meta = build_dataset(input_path,engine.store.root/"datasets",json.loads(Path(contract_path).read_text(encoding="utf-8")))
                if meta["status"]=="FAILED":
                    st.error("所有文件加载失败")
                    st.json(meta["files"])
                else:
                    engine.store.name_dataset(path,name)
                    st.session_state["selected_dataset_path"]=str(Path(path).resolve())
                    sid = engine.store.create(path)
                    st.session_state["active_session"] = sid
                    st.success("数据已加载："+meta["status"])
            except Exception as exc:
                st.error("加载未完成："+type(exc).__name__+"。请检查路径、契约和文件内容。")
    sessions = engine.store.list()
    if sessions:
        session_ids = [s["id"] for s in sessions]
        names = {s["id"]:engine.store.dataset_name(s["dataset_dir"])+" · "+s["title"]+" · "+s["fingerprint"][:8]+" · "+s["accessed"][:19]+(" · 数据不可用" if not (Path(s["dataset_dir"])/"analysis.duckdb").is_file() else "") for s in sessions}
        current = st.session_state.get("active_session",session_ids[0])
        sid = st.selectbox("历史诊断会话",session_ids,index=session_ids.index(current) if current in session_ids else 0,format_func=lambda v:names[v])
        st.session_state["active_session"] = sid
    else:
        sid = None
    with st.expander("已有数据管理"):
        datasets=engine.store.datasets()
        by_path={d['path']:d for d in datasets}
        if datasets:
            paths=list(by_path)
            preferred=st.session_state.get('selected_dataset_path')
            selected_dataset=st.selectbox('固定数据版本',paths,
                index=paths.index(preferred) if preferred in paths else 0,
                format_func=lambda p:by_path[p]['name']+' · '+by_path[p]['dataset_id'][:8])
            st.session_state['selected_dataset_path']=selected_dataset
            selected=by_path[selected_dataset]
            if st.button("创建诊断会话"):
                try:
                    st.session_state["active_session"]=engine.store.create(selected_dataset)
                    st.rerun()
                except Exception:
                    st.error("该数据版本不可用或正在被操作，请稍后重试。")
            st.caption('重新加载将使用该版本保存的CSV快照及当前清洗、小区参数补齐规则生成新版本；旧数据与历史报告保留。不会读取原目录后来新增或修改的文件。')
            reload_name=st.text_input('重新加载后的数据名称',value=selected['name'][:95]+' · 重载',
                max_chars=100,key='reload_name_'+selected['dataset_id'])
            reload_contract=st.selectbox('重新加载输入契约',
                ['沿用原输入格式与别名','config/sample_input_contract.json','config/input_contract.json'],
                key='reload_contract_'+selected['dataset_id'])
            if st.button('重新加载数据'):
                try:
                    contract=None if reload_contract=='沿用原输入格式与别名' else json.loads(Path(reload_contract).read_text(encoding='utf-8'))
                    with st.spinner('正在校验CSV快照并重新清洗，请勿重复操作…'):
                        new_path,new_meta=engine.store.reload_dataset(selected_dataset,
                            expected_dataset_id=selected['dataset_id'],name=reload_name,contract=contract)
                    if new_meta['status']=='FAILED':
                        st.error('重新加载失败，旧版本已保留。')
                        st.json(new_meta['files'])
                    else:
                        st.session_state['active_session']=engine.store.create(new_path)
                        st.session_state['selected_dataset_path']=str(new_path.resolve())
                        st.session_state['reload_notice']='重新加载已生成新版本：'+reload_name+'（'+new_meta['status']+'）。旧版本已保留。'
                        st.session_state['reload_partial']=new_meta['status']=='PARTIAL_READY'
                        st.rerun()
                except ValueError as exc:
                    st.error(str(exc))
                except Exception as exc:
                    st.error('重新加载未完成（'+type(exc).__name__+'），旧版本已保留；请检查快照、输入契约或数据占用。')
            new_name=st.text_input('修改数据名称',value=selected['name'],max_chars=100,key='dataset_name_'+selected['dataset_id'])
            if st.button('保存数据名称'):
                try:
                    engine.store.name_dataset(selected_dataset,new_name)
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))
                except Exception:
                    st.error('修改失败，数据可能正在被使用。')
            st.caption(f"关联 {selected['sessions']} 个诊断会话。删除将移除数据库和输入快照，保留历史报告及已生成证据；删除后无法继续查询或回查原始行。原始CSV源文件不会删除。")
            confirmed=st.checkbox('确认删除此数据集及其输入快照',key='delete_confirm_'+selected['dataset_id'])
            if st.button('删除数据集',disabled=not confirmed):
                try:
                    engine.store.delete_dataset(selected_dataset,expected_dataset_id=selected['dataset_id'])
                    st.session_state.pop('pending_action',None)
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))
                except Exception:
                    st.error('删除未完成，数据可能正在分析或被其他程序占用。请释放占用后重试。')
        else:
            st.caption('暂无可管理的数据集。')
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
    feature_path = st.selectbox("特性文档（可选）",[None]+[d["path"] for d in feature_docs],format_func=lambda v:v or "自动识别问题相关特性")
    parents={h['action_id']:h for h in completed if (h.get('feature') or {}).get('cohorts') and h.get('analysis_context')}
    parent_action_id=st.selectbox('追问来源',[None]+list(parents),format_func=lambda v:'新分析' if v is None else parents[v]['question']+' · '+v[:8])
    cohort_name=None
    parent_event_id=None
    if parent_action_id:
        labels={'all':'父分析全部业务有效条目','enabled':'父分析实际生效条目','disabled':'父分析实际未生效条目','unknown':'父分析状态未知条目'}
        cohort_name=st.selectbox('继承的记录群体',list(parents[parent_action_id]['feature']['cohorts']),format_func=lambda v:labels.get(v,v))
        parent_event_id=st.text_input('具体变化 event_id（可选，填写后仅继承该事件前后记录）',value='') or None
        st.caption('追问严格继承该记录群体，不自动扩展为相关用户全部记录。范围和规则变化请新建分析。')
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
        dataset_ok = report.get('rule_version') in {'m1.4','m1.5'}
        if not dataset_ok:
            st.info('旧数据仅供查看历史结果；请重新加载 CSV 以应用实际流数和整行排除规则。')
    except Exception:
        dataset_ok = False
        st.error("会话数据集不可用；仍可查看已保存历史。")


def run_question(question, plan_override=None,sql_override=None,peer_comparison=False):
    action_id = str(uuid4())
    st.session_state["pending_action"] = {"sid":sid,"action_id":action_id,"question":question,
                                          "scope":scope.__dict__,"feature_path":feature_path,"plan_override":plan_override,"sql_override":sql_override,"peer_comparison":peer_comparison,"parent_action_id":parent_action_id,"cohort_name":cohort_name,"parent_event_id":parent_event_id}
    try:
        with st.status("正在分析",expanded=True) as status:
            completed_result=engine.run(sid,question,scope,action_id,feature_path=feature_path,progress=lambda step:st.write(step),plan_override=plan_override,sql_override=sql_override,peer_comparison=peer_comparison,parent_action_id=parent_action_id,cohort_name=cohort_name,parent_event_id=parent_event_id)
            complete=completed_result.get("status")=="FACTS_READY"
            status.update(label="本问题核验完成" if complete else "已保留部分结果，请查看停止原因和缺口",state="complete" if complete else "error")
        st.session_state.pop("pending_action",None)
        st.rerun()
    except ValueError as exc:
        st.error(str(exc))
    except Exception:
        st.error("本次操作未完成，已有证据保留。可显式重试。")


with middle:
    st.subheader("诊断与追问")
    with st.form("question_"+sid):
        question = st.text_area("问题描述",value="比较各流增益，查看异常比例与缺测情况。")
        peer_comparison = st.checkbox("明确请求同侪比较（需选择目标 UE 或 CRNTI）",value=False)
        submitted = st.form_submit_button("开始分析",disabled=not(scope_valid and dataset_ok))
    if submitted and question.strip():
        run_question(question,peer_comparison=peer_comparison)
    pending = st.session_state.get("pending_action")
    if pending and pending["sid"]==sid and st.button("重试上次未完成操作",disabled=not dataset_ok):
        try:
            engine.run(sid,pending["question"],restore_scope(pending["scope"]),pending["action_id"],retry=True,
                       feature_path=pending["feature_path"],plan_override=pending["plan_override"],sql_override=pending.get("sql_override"),peer_comparison=pending.get("peer_comparison",False),parent_action_id=pending.get("parent_action_id"),cohort_name=pending.get("cohort_name"),parent_event_id=pending.get("parent_event_id"))
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
                status=hypothesis.get('status')
                if status:
                    st.info({'SUPPORTED':'条件判断获支持','REFUTED':'条件判断被反驳','INCONCLUSIVE':'条件判断未定'}.get(status,status)+'：'+hypothesis['text'])
                    st.caption(hypothesis.get('interpretation','')+' · '+str(hypothesis.get('counts',{})))
                else:
                    st.info('历史结果中的待验证假设（尚未完成自动核验）：'+hypothesis['text'])
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
        panel = st.selectbox("选择画布内容",["统计与图表","查询证据","特性条件","案例审核"],key="panel_"+sid)
        if panel=="统计与图表":
            for chart_index,(figure,caption) in enumerate(feature_figures(result.get('feature'))):
                st.plotly_chart(figure,use_container_width=True,key=f"feature_chart_{result['action_id']}_{chart_index}")
                st.caption(caption)
            observations=[a for a in result.get('artifacts',[]) if a.get('operator')=='SESSION_OBSERVATIONS']
            if observations:
                with st.expander('状态变化图',expanded=any(word in result['question'] for word in ('变化','切换','趋势','时序'))):
                    try:
                        artifact=observations[0]
                        verify_artifact(artifact)
                        batch=next(pq.ParquetFile(artifact['path']).iter_batches(batch_size=2000),None)
                        figure=observation_figure(batch.to_pylist() if batch else [])
                        if figure is not None:
                            st.plotly_chart(figure,use_container_width=True,key='observations_'+result['action_id'])
                            st.caption(f"最多展示前 2000 个观测点，完整证据 {artifact['row_count']} 行。点之间不连线，不推断间隔内持续状态或精确切换时刻。完整结果可在查询证据下载。")
                        else:
                            st.info('当前范围没有可绘制的状态观测。')
                    except (ValueError,OSError):
                        st.warning('状态观测证据不可用或校验失败，未绘图。')
            elif any(word in result['question'] for word in ('变化','切换','趋势','时序')):
                st.info('本次结果没有会话状态观测证据，无法绘制状态变化图。')
            summary = result["summary"]
            st.metric("流控有效记录",summary["flow_valid_count"])
            st.metric("业务准入记录",summary.get("business_valid_count",summary["flow_valid_count"]))
            st.caption("统计策略："+summary['quality_policy']+"；规则版本："+summary.get('rule_version','历史版本'))
            if summary.get('quality'):
                st.json(summary['quality'])
                if summary['quality'].get('partial_stream_count'):
                    st.warning('存在 8 流记录：仅覆盖前四流。')
            with st.expander("需求核账与补查"):
                st.caption(f"补查轮数：{result.get('backfill_rounds',0)}")
                st.dataframe(pd.DataFrame(result.get('requirements',[])),hide_index=True)
                st.dataframe(pd.DataFrame(result.get('responsibility_evidence',[])),hide_index=True)
                st.json(result.get('case_assessments',[]))
            if result.get('peers',{}).get('status') not in {None,'NOT_REQUESTED'}:
                st.write({k:v for k,v in result['peers'].items() if k!='artifacts'})
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
            with st.expander('分析范围与目标详情'):
                st.json(result.get('analysis_context',{}))
                st.json(result.get('goals',[]))
            with st.expander('持续验证任务、停止原因及缺口'):
                st.json(result.get('verification_loop',{}))
            advanced=st.checkbox('显示技术详情与高级查询',value=False,key='advanced_'+sid)
            if advanced:
                st.json(result["plan"])
            with st.form('trace_'+sid):
                trace_id=st.text_input('回查原始记录 record_id')
                inspect_record=st.form_submit_button('核验并回查原始记录')
            if inspect_record and trace_id:
                try:
                    allowed=any(evidence_contains_record(evidence,trace_id) for evidence in result['artifacts'])
                    if not allowed: raise ValueError('记录不属于本轮证据')
                    st.json(record_trace(session['dataset_dir'],trace_id))
                except (ValueError,OSError):
                    st.error('记录不属于本轮证据，或原始快照校验失败。')
            for artifact in result["artifacts"]:
                with st.expander(OPERATORS.get(artifact['operator'],'查询结果')+" · "+artifact["step_id"]):
                    st.caption(f"完整行数 {artifact['row_count']}；预览最多 100 行；SHA256 {artifact['sha256']}")
                    st.write(describe_artifact(artifact,result.get('plan')))
                    if advanced:
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
            if advanced:
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
                feature=result['feature']
                st.caption('条件不满足仅表示排查线索；多个条件可重叠，不直接确认根因。')
                if feature.get('validation',{}).get('status')=='VERIFIED':
                    conditions=[]
                    for condition in feature.get('feature',{}).get('conditions',[]):
                        counts=feature.get('conditions',{}).get(condition['condition_id'],{})
                        conditions.append({'条件':expression_text(condition['expression']),
                            '满足':counts.get('True',0),'不满足':counts.get('False',0),'未知':counts.get('None',0)})
                    st.dataframe(pd.DataFrame(conditions),hide_index=True)
                    if feature.get('cell_parameter_inference',{}).get('imputed_record_count'):
                        st.caption('部分条件使用小区参数推定补齐，原始值与补齐来源可在证据中回查。')
                else:
                    st.warning('条件统计尚未通过一致性校验。')
                with st.expander('完整特性与会话核验详情'):
                    st.json(feature)
                    st.json(result.get("session_states",{}))
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
