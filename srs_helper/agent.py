"""LangGraph diagnosis with persisted checkpoints and evidence-first output."""
from .performance import measured, validation_event
from .domain_policy import FOUR_STREAM_POLICY, gains_missing
from .explanation import validate_explanation, ExplanationError, UNITS
from pydantic import ValidationError
import json
import sqlite3
import time
import hashlib
import os
from .cross_layer import describe_cross_layer
from .case_assessment import assess_cases
from .peers import compare_peers
from .requirements import question_requirements, audit_requirements, backfill_plan, inventory_requirements, analysis_budget
from .field_metadata import relevant_fields
from datetime import datetime
from typing import TypedDict
from uuid import uuid4

import portalocker
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from .database import open_dataset, quality_report
from .evidence import feature_analysis, validate_numeric_claim, verify_artifact
from .knowledge import KnowledgeIndex
from .plans import QueryPlan, default_plan, execute_plan, scope_columns, validate_plan_fields, PlanFieldError
from .queries import Scope, summarize
from .services import ModelService, ServiceConfig, ServiceError, extract_feature, call_model, model_budget
from .sessions import SessionStore, dumps
from .sql_executor import execute_sql
from .charts import time_series_artifact
from .provenance import analysis_context, validate_parent, bind_cohort, identity, event_population
from .prompts import EXPLAIN_SYSTEM, PLAN_SYSTEM, VERIFY_SYSTEM, PROMPT_VERSION, relevant_columns
from .verification_loop import VerificationLoop, TaskProposal, loop_report, predicate_label
from .state_analysis import state_analysis
from .reporting import feature_report
from .visual import compare_visual


class DiagnosticState(TypedDict, total=False):
    question: str
    action_id: str
    scope: dict
    plan: dict
    refs: list[dict]
    notices: list[str]
    retrieval_mode: str
    artifact_refs: list[dict]
    answer: str
    status: str
    steps: list[str]
    requirements: list[dict]
    route: str
    goals: list[dict]
    backfill_rounds: int
    budget_started: float
    loop_ready: bool
    loop_reenter: bool
    loop_state: dict


def restore_scope(data):
    data = dict(data)
    for key in ("start","end"):
        if isinstance(data.get(key),str):
            data[key] = datetime.fromisoformat(data[key])
    return Scope(**data)


class DiagnosticEngine:
    def __init__(self,storage="workspace_storage",assets="knowledge_assets",model=None,embedder=None):
        self.store = SessionStore(storage)
        self.index = KnowledgeIndex(assets,self.store.root/"knowledge")
        self.model = model or ModelService(ServiceConfig.from_env())
        vision_config = ServiceConfig.from_env("VISION")
        self.vision = ModelService(vision_config) if vision_config.configured else self.model
        embedding_config = ServiceConfig.from_env("EMBEDDING")
        self.embedder = embedder or (ModelService(embedding_config) if embedding_config.configured else None)

    @measured("diagnosis")
    def run(self,sid,question,scope=None,action_id=None,retry=False,feature_path=None,progress=None,plan_override=None,sql_override=None,peer_comparison=False,parent_action_id=None,cohort_name=None,parent_event_id=None):
        scope,action_id = scope or Scope(),action_id or str(uuid4())
        scope.where()
        session = self.store.get(sid)
        request = {"question":question,"scope":scope.__dict__,"feature_path":feature_path,"plan_override":plan_override,"sql_override":sql_override,"peer_comparison":peer_comparison,"parent_action_id":parent_action_id,"cohort_name":cohort_name,"parent_event_id":parent_event_id}
        # OS lock survives concurrent Streamlit reruns and is released if the process dies.
        with self.store.dataset_guard(session["dataset_dir"]), portalocker.Lock(str(self.store.directory(sid)/"run.lock"),timeout=0):
            existing = self.store.begin(sid,action_id,request,retry)
            if existing is not None:
                return existing
            try:
                parent = None; cohort = None
                if parent_action_id:
                    parent = next((r for r in self.store.history(sid) if r.get('action_id')==parent_action_id),None)
                    if parent is None:
                        raise ValueError('Parent analysis not found')
                    with open_dataset(session['dataset_dir']) as (_,meta):
                        cohort=validate_parent(parent,meta,scope,'all' if parent_event_id else cohort_name)
                    if parent_event_id:
                        cohort=event_population(parent,parent_event_id,self.store.directory(sid)/'artifacts')
                    elif '这些变化' in question:
                        raise ValueError('请选择父分析中的具体变化事件，不以整个记录群体替代。')
                    if plan_override is not None or sql_override is not None:
                        raise ValueError('Inherited cohort requires a diagnostic query, not a manual override')
                elif cohort_name or parent_event_id or any(w in question for w in ('这些用户','这些记录','这些变化','这些会话')):
                    raise ValueError('请明确选择追问来源和群体，不能从聊天文字猜测这些对象。')
                with bind_cohort(cohort):
                    return self._run(session,question,scope,action_id,feature_path,progress,plan_override,sql_override,peer_comparison,parent,cohort)
            except Exception:
                self.store.fail(sid,action_id)
                raise

    def _run(self,session,question,scope,action_id,feature_path,progress,plan_override,sql_override,peer_comparison,parent=None,inherited_cohort=None):
        sid = session["id"]
        with open_dataset(session["dataset_dir"]) as (con,meta):
            if (meta["dataset_id"],meta["dataset_fingerprint"]) != (session["dataset_id"],session["fingerprint"]):
                raise ValueError("Session dataset version no longer matches")
            if meta['rule_version'] not in {'m1.4','m1.5'}:
                raise ValueError('历史数据仅供恢复；请重新加载为 m1.4 后发起新分析。')
            columns = scope_columns(con)
        context=analysis_context(meta,scope,action_id,parent.get('action_id') if parent else None,
                                 inherited_cohort['artifact_id'] if inherited_cohort else None)
        context['parent_event_id']=inherited_cohort.get('event_id') if inherited_cohort else None
        folder = self.store.directory(sid)/"artifacts"
        folder.mkdir(parents=True,exist_ok=True)
        journal_path = folder/('progress_'+hashlib.sha256(action_id.encode()).hexdigest()+'.json')
        journal = json.loads(journal_path.read_text(encoding='utf-8')) if journal_path.exists() else {}
        budget = journal.get("budget") or analysis_budget()
        budget.setdefault('model_calls_limit',int(os.getenv('SRS_ANALYSIS_MAX_MODEL_CALLS','12')))
        if not 1<=budget['model_calls_limit']<=24:
            raise ValueError('Invalid cumulative model call budget')
        def save_progress(**updates):
            journal.update(updates)
            pending = journal_path.with_suffix('.tmp')
            pending.write_text(dumps(journal),encoding='utf-8')
            os.replace(pending,journal_path)
        operation_started=journal.get('budget_started',time.time())
        save_progress(budget_started=operation_started,budget=budget)
        def remaining():
            return max(0,budget['seconds']-(time.time()-operation_started))
        def budgeted_model(messages, purpose):
            count=journal.get('model_calls',0)
            if remaining()<=0 or count>=budget['model_calls_limit']:
                raise ServiceError('累计模型或时间预算耗尽',code='BUDGET_EXHAUSTED')
            save_progress(model_calls=count+1)
            return call_model(self.model,messages,purpose)
        ephemeral = {"validation_errors": [], "model_status":{"plan":"NOT_RUN","explain":"NOT_RUN"}}  # full results live in files, not LangGraph checkpoints
        def announce(name):
            if progress:
                progress(name)
        def retrieve(state):
            announce("检索知识")
            self.index.sync()
            docs,mode = self.index.search(question,embedder=self.embedder)
            refs = [{"path":d["path"],"version":d.get("source_version",d["version"]),"kind":d["kind"],
                     "title":d["metadata"].get("title",d["path"]),"excerpt":d["body"][:1500],
                     "warnings":d["warnings"],"required_metrics":d["metadata"].get("required_metrics",[]),"question_terms":d["metadata"].get("question_terms",[])} for d in docs]
            for ref, doc in zip(refs, docs):
                if doc['kind']=='field' and 'field_catalog' in doc['metadata']:
                    ref['excerpt'] = dumps(relevant_fields(doc['metadata']['field_catalog'],question))
            ephemeral["documents"] = docs
            return {"refs":refs,"retrieval_mode":mode,"steps":["retrieve"]}
        def route_node(state):
            all_features=[d for d in self.index.documents() if d['kind']=='feature']
            selected=feature_path or (parent.get('feature_binding',{}).get('path') if parent else None)
            candidates=[d for d in all_features if d['path']==selected] if selected else [d for d in all_features if d['metadata'].get('feature_id','__none__').lower() in question.lower()]
            if not candidates and any(w in question for w in ('特性','生效','条件')):
                candidates=[d for d in ephemeral['documents'] if d['kind']=='feature']
            doc=candidates[0] if len(candidates)==1 else None
            if selected and doc is None:
                raise ValueError('Selected feature version unavailable')
            if parent and doc and (parent.get('feature_binding',{}).get('rule_hash')!=identity(doc['metadata']) or parent.get('feature_binding',{}).get('path')!=doc['path'] or parent.get('feature_binding',{}).get('version')!=doc['version']):
                raise ValueError('Feature rule changed; start a new analysis')
            if sql_override is not None:
                doc=None
            ephemeral['selected_feature']=doc
            context['feature_rule_hash']=identity(doc['metadata']) if doc else None
            route='FEATURE' if doc and not doc['metadata'].get('unstructured') and plan_override is None and sql_override is None else 'DESCRIPTIVE' if question_requirements(question,columns,state['refs']) else 'EXPLORATION'
            return {'route':route,'steps':state['steps']+['route']}

        def requirements_node(state):
            announce("整理问题需求")
            with open_dataset(session['dataset_dir']) as (con,_):
                requirements = inventory_requirements(con,meta,question_requirements(question,columns,state['refs']))
            return {'requirements':requirements,
                    'backfill_rounds':journal.get('rounds',0),'budget_started':operation_started,'steps':state['steps']+['requirements']}
        def plan_node(state):
            announce("生成查询计划")
            notices = []
            save_progress(budget_started=state['budget_started'],budget=budget)
            if journal.get('plan'):
                ephemeral['model_status']['plan']='REUSED'
                return {'plan':journal['plan'],'notices':[], 'steps':state['steps']+['plan']}
            plan = default_plan(meta,question)
            if plan_override is not None:
                plan = QueryPlan.model_validate(plan_override)
                validate_plan_fields(plan,columns)
            elif state.get('route')=='FEATURE':
                ephemeral['model_status']['plan']='NOT_REQUIRED_STRUCTURED_FEATURE'
            elif self.model.config.configured and sql_override is None:
                history = [{"question":r.get("question"),"analysis_context":r.get("analysis_context"),"goals":r.get("goals",[]),"scope":r.get("scope")}
                           for r in self.store.history(sid)[-6:] if "answer" in r]
                prompt = {"question":question,"history":history,"current_scope":scope.__dict__,"dataset_id":meta["dataset_id"],
                          "dataset_fingerprint":meta["dataset_fingerprint"],"columns":relevant_columns(columns,state["requirements"]),"field_catalog_mode":"QUESTION_RELATED_SUBSET","reference_data":state["refs"],
                          "requirements":state["requirements"],"query_plan_schema":QueryPlan.model_json_schema()}
                messages = [SystemMessage(content=PLAN_SYSTEM),HumanMessage(content=dumps(prompt))]
                messages[0].content += FOUR_STREAM_POLICY
                output = None
                for attempt in range(3):
                    try:
                        output = budgeted_model(messages,'plan')
                        candidate = QueryPlan.model_validate(output)
                        if (candidate.dataset_id,candidate.dataset_fingerprint)!=(meta["dataset_id"],meta["dataset_fingerprint"]):
                            raise ValueError("wrong dataset")
                        validate_plan_fields(candidate,columns)
                        plan = candidate
                        ephemeral['model_status']['plan']='SUCCEEDED'
                        break
                    except ServiceError as exc:
                        ephemeral['model_status']['plan']='FAILED:'+exc.code
                        notices.append(str(exc)); break
                    except (ValueError,TypeError) as exc:
                        ephemeral['model_status']['plan']='VALIDATION_FAILED'
                        details = ([{"location":list(e["loc"]),"reason":e["msg"]} for e in exc.errors(include_input=False,include_url=False)]
                                   if isinstance(exc,ValidationError) else [exc.details] if isinstance(exc,PlanFieldError) else [{"location":[],"reason":"Invalid plan or dataset identity"}])
                        validation_event("plan_validation",attempt+1,details)
                        ephemeral["validation_errors"].append({"stage":"plan","attempt":attempt+1,"details":details})
                        messages.append(HumanMessage(content="上次失败输出（可能截短）："+dumps(output)[:8000]))
                        messages.append(HumanMessage(content="计划校验失败，请按以下定位修复，只返回 JSON："+dumps(details)))
                        if attempt==2:
                            notices.append("模型计划结构修复失败，使用默认描述性计划。")
                            plan = default_plan(meta,question)
            else:
                notices.append("尚未配置诊断模型，请填写 .env；本轮运行确定性统计。")
            plan = plan.model_copy(update={"quality_policy":"EXCLUDE_ABNORMAL_RECORD" if meta["rule_version"]in {"m1.4","m1.5"} else "INCLUDE_ALL_NUMERIC"})
            save_progress(plan=plan.model_dump(mode="json"))
            return {"plan":plan.model_dump(mode="json"),"notices":notices,"steps":state["steps"]+["plan"]}
        def execute(state):
            announce("执行范围查询")
            # Baseline succeeds independently of an invalid model-generated plan.
            ephemeral["summary"] = summarize(session["dataset_dir"],scope)
            artifacts,notices = [],list(state["notices"])
            if journal.get('execution_complete'):
                for artifact in journal['artifacts']:
                    verify_artifact(artifact)
                ephemeral.update(artifacts=journal['artifacts'],feature=journal.get('feature'),peers=journal.get('peers',{}),case_assessments=journal.get('case_assessments',[]))
                return {'artifact_refs':journal['artifacts'],'notices':journal.get('notices',[]),'steps':state['steps']+['execute']}
            try:
                if sql_override is not None:
                    artifacts = execute_sql(session["dataset_dir"],scope,sql_override["sql"],sql_override.get("parameters",[]),folder)
                    notices.append("手动 SQL 为探索性结果，不用于自动特性或根因判定。")
                elif state.get("route") != "FEATURE":
                    artifacts = execute_plan(session["dataset_dir"],scope,QueryPlan.model_validate(state["plan"]),folder,seconds=max(.001,min(30,remaining())))
                for artifact in artifacts:
                    artifact["origin"] = "QUERY"
            except Exception as exc:
                notices.append("计划执行失败（"+type(exc).__name__+"）；保留基础统计，可修改计划重试。")
            try:
                if any(r["field"] in {"usrRsrp","srsUeNiDb"} for r in state["requirements"]):
                    artifacts.append(time_series_artifact(session["dataset_dir"],scope,folder))
            except Exception as exc:
                notices.append("时序图数据生成失败（"+type(exc).__name__+"）；已保留其他结果。")
            try:
                ephemeral['peers'] = compare_peers(session['dataset_dir'],scope,[r['field'] for r in state['requirements']],folder,enabled=peer_comparison)
                artifacts.extend(ephemeral['peers']['artifacts'])
            except Exception as exc:
                ephemeral['peers'] = {'status':'QUERY_FAILED','reason':type(exc).__name__,'artifacts':[]}
                notices.append('同侪比较未完成，已保留其他事实。')
            try:
                artifacts.extend(describe_cross_layer(session['dataset_dir'],scope,question,[r['field'] for r in state['requirements']],folder))
            except Exception as exc:
                notices.append('跨层描述统计未完成（'+type(exc).__name__+'）。')
            try:
                ephemeral['case_assessments'],case_artifacts = assess_cases(session['dataset_dir'],scope,meta,ephemeral['documents'],folder)
                for artifact in case_artifacts:
                    artifact['origin']='CASE_CRITERIA'
                artifacts.extend(case_artifacts)
            except Exception as exc:
                ephemeral['case_assessments']=[{'status':'QUERY_FAILED','route':'EXPLORATION','reason':type(exc).__name__}]
            ephemeral.update(artifacts=artifacts,feature=None)
            save_progress(execution_complete=True,artifacts=artifacts,peers=ephemeral['peers'],case_assessments=ephemeral['case_assessments'],notices=notices)
            return {'artifact_refs':[{k:v for k,v in a.items() if k not in {'preview','parameters'}} for a in artifacts],
                    'notices':notices,'steps':state['steps']+['execute']}

        def feature_node(state):
            announce('特性规则核验')
            notices=list(state['notices'])
            if journal.get('feature_complete'):
                ephemeral['feature']=journal.get('feature')
                return {'steps':state['steps']+['feature']}
            doc=ephemeral.get('selected_feature')
            if doc:
                try:
                    if remaining()<=0:
                        raise TimeoutError('Analysis budget exhausted')
                    spec=doc['metadata']
                    if spec.get('unstructured'):
                        spec=extract_feature(self.model,doc,columns)
                    feature=feature_analysis(session['dataset_dir'],scope,meta,spec,folder,seconds=min(30,remaining()))
                    new=feature.pop('artifacts')
                    for artifact in new:
                        artifact['origin']='FEATURE'
                    ephemeral['artifacts'].extend(new)
                    ephemeral['feature']=feature
                except Exception as exc:
                    notices.append('特性核验失败（'+type(exc).__name__+'），不发布该部分结论。')
            save_progress(feature_complete=True,feature=ephemeral.get('feature'),artifacts=ephemeral['artifacts'],notices=notices)
            return {'notices':notices,'artifact_refs':[{k:v for k,v in a.items() if k not in {'preview','parameters'}} for a in ephemeral['artifacts']],
                    'steps':state['steps']+['feature']}

        def states_node(state):
            feature=ephemeral.get('feature') or {}
            if not any(word in question for word in ('用户','会话','变化','恢复','切换')) or feature.get('validation',{}).get('status')!='VERIFIED':
                ephemeral['session_states']={'status':'NOT_RUN'}
            elif journal.get('states_complete'):
                ephemeral['session_states']=journal['session_states']
            else:
                try:
                    source=next(a for a in ephemeral['artifacts'] if a.get('origin')=='FEATURE' and a['operator']=='condition_evaluated_rows')
                    result,new=state_analysis(session['dataset_dir'],scope,feature,source,folder)
                    ephemeral['session_states']=result
                    ephemeral['artifacts'].extend(new)
                    save_progress(states_complete=True,session_states=result,artifacts=ephemeral['artifacts'])
                except Exception as exc:
                    ephemeral['session_states']={'status':'FAILED','reason':type(exc).__name__}
            return {'steps':state['steps']+['states']}

        def coverage_node(state):
            announce("核账与有限补查")
            requirements = audit_requirements(journal.get('requirements',state['requirements']),ephemeral['artifacts'],columns)
            notices = list(state['notices'])
            rounds = state['backfill_rounds']
            # Hard total budget and finite batches; no free SQL or model-expanded requirements.
            while rounds < budget['rounds'] and time.time()-state['budget_started'] < budget['seconds']:
                plan = backfill_plan(meta,question,requirements,limit=budget["fields"])
                if plan is None or sql_override is not None:
                    break
                rounds += 1
                save_progress(rounds=rounds,requirements=requirements)
                before = [(r['id'],r['status']) for r in requirements]
                try:
                    remaining = max(0.1,budget['seconds']-(time.time()-state['budget_started']))
                    new = execute_plan(session['dataset_dir'],scope,plan,folder,seconds=min(30,remaining))
                    for artifact in new:
                        artifact['origin'] = 'COVERAGE_BACKFILL'
                    ephemeral['artifacts'].extend(new)
                    requirements = audit_requirements(requirements,ephemeral['artifacts'],columns)
                except Exception as exc:
                    for r in requirements:
                        if r['field'] in {m.field for step in plan.steps for m in step.metrics}:
                            r['status'] = 'QUERY_FAILED'
                    notices.append('需求补查失败（'+type(exc).__name__+'），保留已有事实。')
                save_progress(requirements=requirements,artifacts=ephemeral['artifacts'])
                if before == [(r['id'],r['status']) for r in requirements]:
                    break
            for r in requirements:
                if r['status']=='NOT_QUERIED':
                    r['status']='MANUAL_QUERY_NOT_AUDITED' if sql_override is not None else 'BUDGET_EXHAUSTED'
            save_progress(requirements=requirements)
            pending = [r for r in requirements if r['status']!='COVERED']
            if pending:
                notices.append('相关需求部分完成，未覆盖项及原因见需求核账。')
            return {'requirements':requirements,'backfill_rounds':rounds,'notices':notices,
                    'artifact_refs':[{k:v for k,v in a.items() if k not in {'preview','parameters'}} for a in ephemeral['artifacts']],
                    'steps':state['steps']+['coverage']}
        def verify(state):
            announce("校验证据")
            for artifact in ephemeral["artifacts"]:
                verify_artifact(artifact)
            return {"steps":state["steps"]+["verify"]}
        def goals_node(state):
            feature=ephemeral.get('feature') or {}
            goals=[]
            if ephemeral.get('selected_feature'):
                valid=feature.get('validation',{}).get('status')=='VERIFIED'
                status='VERIFIED' if valid and feature.get('sample_cnt',0)>0 else 'INSUFFICIENT' if valid else 'FAILED'
                goals.append({'id':'actual_rate','status':status,'evidence':feature.get('actual_metrics',{})})
                if any(w in question for w in ('原因','条件','为什么','未生效')):
                    goals.append({'id':'disabled_conditions','status':'INSUFFICIENT' if feature.get('disabled_condition_coverage',{}).get('MISSING_CONDITIONS',0) else status,
                                  'parent_cohort_id':feature.get('cohorts',{}).get('disabled',{}).get('cohort_id'),'evidence':feature.get('reason_metrics',{})})
                if any(w in question for w in ('用户','会话')) and any(w in question for w in ('占比','比例')):
                    goals.append({'id':'user_reason_ratio','status':'NOT_RUN','reason':'用户级原因分母待确认；本轮仅发布条目和观测会话状态事实'})
            if any(word in question for word in ('用户','会话','变化','恢复','切换')) and ephemeral.get('selected_feature'):
                goals.append({'id':'session_states','status':ephemeral['session_states']['status'],'evidence':ephemeral['session_states'].get('artifact_ids',[])})
            for r in state['requirements']:
                goals.append({'id':r['id'],'status':'VERIFIED' if r['status']=='COVERED' else 'INSUFFICIENT','reason':r['status'],'evidence':r['artifacts']})
            if not goals:
                goals.append({'id':'question_scope','status':'INSUFFICIENT','reason':'未识别可验证的业务目标'})
            return {'goals':goals,'steps':state['steps']+['goals']}

        def loop_prepare(state):
            announce('建立验证任务与残余群体')
            loop=VerificationLoop(directory=session['dataset_dir'],scope=scope,meta=meta,context=context,
                columns=columns,folder=folder,artifacts=ephemeral['artifacts'],feature=ephemeral.get('feature'),
                goals=state['goals'],question=question,remaining=remaining,
                persist=lambda data:save_progress(loop_state=data,artifacts=ephemeral['artifacts']),
                restored=journal.get('loop_state'),
                rules=(ephemeral.get('selected_feature') or {}).get('metadata',{}).get('verification_rules',[]))
            ephemeral['loop']=loop
            loop.initialize()
            return {'steps':state['steps']+['loop_prepare'],'loop_state':loop.state.model_dump(mode='json')}

        def loop_plan(state):
            announce('评估证据缺口并规划下一次验证')
            loop=ephemeral['loop']
            if loop.pending() and loop.can_run():
                ready=True
            else:
                needs_planner=loop.reason_requested and (loop.cohorts or loop.state.pending_drafts)
                if needs_planner and self.model.config.configured:
                    def propose(payload):
                        try:
                            output=budgeted_model([SystemMessage(content=VERIFY_SYSTEM+FOUR_STREAM_POLICY),
                                HumanMessage(content=dumps(payload))],'verify')
                            ephemeral['model_status']['verify']='SUCCEEDED'
                            return output
                        except ServiceError as exc:
                            ephemeral['model_status']['verify']='FAILED:'+exc.code
                            raise
                    loop.propose(propose)
                elif loop.state.pending_drafts:
                    loop.stop('TECHNICAL_FAILURE','未配置模型，无法将新增解释转换为验证任务')
                ready=bool(loop.pending()) and loop.can_run()
            if not ready:
                loop.finish()
            return {'loop_ready':ready,'loop_state':loop.state.model_dump(mode='json'),'steps':state['steps']+['loop_plan']}

        def loop_execute(state):
            announce('执行追加验证查询')
            ephemeral['loop'].execute()
            return {'loop_state':ephemeral['loop'].state.model_dump(mode='json'),'steps':state['steps']+['loop_execute']}

        def loop_evaluate(state):
            announce('核验支持、反驳和未知群体')
            loop=ephemeral['loop']
            try:
                loop.evaluate()
            except (ValueError,KeyError,TypeError) as exc:
                task=loop.pending()
                if task:
                    task.status='FAILED'; task.error_code=type(exc).__name__
                loop.stop('TECHNICAL_FAILURE','验证证据核账失败：'+type(exc).__name__)
            return {'loop_state':loop.state.model_dump(mode='json'),'steps':state['steps']+['loop_evaluate']}

        def report_node(state):
            announce("生成确定性报告")
            s = ephemeral["summary"]
            answer = (f"当前范围包含 {s['flow_valid_count']} 条流控有效记录，其中 {s['session_assigned_count']} 条可归属观测会话。"
                      f"RSRP / NI 有效样本分别为 {s['rsrp_valid_count']} / {s['ni_valid_count']}。"
                      +(f"其中 {s['business_valid_count']} 条参与业务统计；可疑及非法流数记录整行排除。" if s["quality_policy"]=="EXCLUDE_ABNORMAL_RECORD" else "历史数据采用旧口径，包含可疑值；请重新加载以应用新规则。")+"各流均值独立统计，使用有限值算术均值。")
            if not s["flow_valid_count"]:
                answer += " 当前范围无数据，不能据此确认或排除根因。"
            missing_gains = gains_missing(s)
            if missing_gains and any(r["field"]=="instBfSinrGain" for r in state["requirements"]):
                answer += " 四流映射已确认，但当前范围没有有效增益样本，仅报告缺测；不能比较流间增益或推断采集故障。"
            if state["refs"]:
                answer += " 已提供知识参考；相似性不等于根因确认，适用条件及外部证据仍需核对。"
            business_answer = feature_report(ephemeral.get('feature'))
            if business_answer:
                answer = business_answer + '\n\n' + answer
            states=ephemeral.get('session_states',{})
            if states.get('status')=='VERIFIED':
                answer+='\n\n用户观测会话 '+str(states['session_count'])+' 个，相邻观测状态变化 '+str(states['observed_changes'])+' 次。仅反映观测变化，不确认间隔内持续状态、精确变化时间或根因；用户级原因占比尚未启用。'
            return {'answer':answer,'steps':state['steps']+['report']}

        def explain(state):
            announce('整理可选模型解释')
            answer=state['answer']
            s=ephemeral['summary']
            missing_gains=gains_missing(s)
            notices = list(state["notices"])
            hypotheses = []
            # A final generator runs once; subsequent verification ends in deterministic composition.
            if journal.get('explanation_complete'):
                ephemeral.update(claims=journal.get('claims',[]),hypotheses=journal.get('hypotheses',[]),
                    responsibility_evidence=journal.get('responsibility_evidence',[]),visual=journal.get('visual',{}))
                ephemeral['model_status']['explain']=journal.get('explain_status','REUSED')
                return {'answer':answer,'notices':notices,'steps':state['steps']+['explain']}
            # Free-form model text is never promoted to measured fact. Numeric claims must bind full artifacts.
            feature_consistent = (ephemeral.get('feature') or {}).get('status') != 'INCONSISTENT'
            if not feature_consistent:
                ephemeral['model_status']['explain']='BLOCKED_INCONSISTENT_EVIDENCE'
                notices.append('特性证据一致性校验失败，暂停模型解释，保留独立基础统计。')
            if feature_consistent and self.model.config.configured and ephemeral["artifacts"] and s["business_valid_count"] and (not state["requirements"] or any(r["status"]=="COVERED" for r in state["requirements"])):
                evidence = [{"artifact_id":a["artifact_id"],"row_count":a["row_count"],"preview":a["preview"][:10]} for a in ephemeral["artifacts"]]
                messages = [SystemMessage(content=EXPLAIN_SYSTEM),
                            HumanMessage(content=dumps({"question":question,"evidence":evidence,"feature_summary":ephemeral.get("feature"),"session_states":ephemeral.get("session_states"),"business_goals":state["goals"],"verification_contract":TaskProposal.model_json_schema(),"verification_state":ephemeral["loop"].planner_payload(),"knowledge":state["refs"],"case_assessments":ephemeral["case_assessments"],"requirements":state["requirements"],"allowed_claim_columns_and_units":UNITS}))]
                messages[0].content += FOUR_STREAM_POLICY
                for attempt in range(3):
                    try:
                        output = budgeted_model(messages,'explain')
                        claims,hypotheses = validate_explanation(output,ephemeral["artifacts"],state["refs"])
                        ephemeral['model_status']['explain']='SUCCEEDED'
                        ephemeral["claims"] = claims
                        break
                    except ServiceError as exc:
                        ephemeral['model_status']['explain']='FAILED:'+exc.code
                        notices.append(str(exc)); break
                    except ExplanationError as exc:
                        ephemeral['model_status']['explain']='VALIDATION_FAILED'
                        details = {"code":exc.code,"location":exc.location}
                        validation_event("explanation_validation",attempt+1,details)
                        ephemeral["validation_errors"].append({"stage":"explanation","attempt":attempt+1,**details})
                        messages.append(HumanMessage(content="上次失败输出（可能截短）："+dumps(output)[:8000]))
                        messages.append(HumanMessage(content="解释校验失败："+dumps(details)+"。修复对应字段；无证据的 claims/hypotheses 请删去，允许空列表。允许的数值列及单位："+dumps(UNITS)))
                        if attempt==2:
                            notices.append("模型解释未通过证据校验（"+exc.code+"，"+exc.location+"），只发布确定性事实表。")
            ephemeral["responsibility_evidence"] = []
            for doc in ephemeral['documents']:
                catalog = doc['metadata'].get('field_catalog',{}).get('fields',{})
                for requirement in state['requirements']:
                    entry = catalog.get(requirement['field'],catalog.get(requirement['field']+'[0]',{}))
                    if entry:
                        ephemeral['responsibility_evidence'].append({'field':requirement['field'],
                            'responsibility_layer':entry.get('responsibility_layer') or '待确认',
                            'evidence_status':requirement['status'],'source':doc['path'],'version':doc['version'],
                            'interpretation':'仅用于排查方向，不确认根因或免责'})
            ephemeral["hypotheses"] = hypotheses
            ephemeral["visual"] = ({"status":"NOT_RUN","reason":"当前问题没有可用的增益图比对需求或样本。"} if missing_gains or not any(r["field"]=="instBfSinrGain" for r in state["requirements"])
                                  else compare_visual(self.vision,s,ephemeral["documents"],self.index,folder))
            save_progress(explanation_complete=True,claims=ephemeral.get('claims',[]),hypotheses=hypotheses,
                explain_status=ephemeral['model_status']['explain'],responsibility_evidence=ephemeral['responsibility_evidence'],visual=ephemeral['visual'])
            feature = ephemeral.get('feature') or {}
            partial = any(g['status']!='VERIFIED' for g in state['goals']) or feature.get('status')=='INCONSISTENT'
            return {"answer":answer,"status":"PARTIAL_FACTS_READY" if partial else "FACTS_READY", "notices":notices,"steps":state["steps"]+["explain"]}
        def completion_gate(state):
            loop=ephemeral['loop']
            # Persist consumption together with queued tasks, so a retry does not lose a new hypothesis.
            if not journal.get('explanation_consumed'):
                loop.accept_explanation(ephemeral.get('hypotheses',[]))
                save_progress(explanation_consumed=True,loop_state=loop.state.model_dump(mode='json'),artifacts=ephemeral['artifacts'])
            reenter=bool(loop.pending() or loop.state.pending_drafts) and loop.can_run()
            return {'loop_reenter':reenter,'steps':state['steps']+['completion_gate']}

        def finalize(state):
            loop=ephemeral['loop']; loop.finish()
            assessments=[]
            tasks={t.task_id:t for t in loop.state.validation_tasks}
            for a in loop.state.assessments:
                p=tasks[a.task_id].proposal
                assessments.append({'text':predicate_label(p.expression),'status':a.status,
                    'references':a.evidence_refs,'task_id':a.task_id,'cohort':p.cohort,
                    'quantifier':p.quantifier,'counts':a.counts,'imputed_record_count':a.imputed_record_count,'interpretation':a.interpretation})
            ephemeral['hypotheses']=assessments
            data=loop.state.model_dump(mode='json')
            goals=[dict(g) for g in state['goals']]
            for goal in goals:
                related=[t for t in loop.state.validation_tasks if t.proposal.goal_id==goal['id']]
                goal['verification_tasks']=[t.task_id for t in related]
                goal['verification_evidence']=list(dict.fromkeys(a for t in related for a in t.evidence_refs))
                affected=any(m.goal_id==goal['id'] and m.required_to_complete for m in loop.state.missing_information)
                affected=affected or any(t.status!='SUCCEEDED' for t in related)
                if loop.reason_requested and goal['id'] in {'disabled_conditions','question_scope'} and loop.state.stop_reason!='GOALS_VERIFIED':
                    affected=True
                if affected and goal['status']=='VERIFIED':
                    goal.update(status='FAILED' if loop.state.stop_reason=='TECHNICAL_FAILURE' else 'INSUFFICIENT',
                                reason=loop.state.stop_reason)
            return {'goals':goals,'answer':state['answer']+'\n\n'+loop_report(data),'loop_state':data,
                'status':'FACTS_READY' if loop.state.stop_reason=='GOALS_VERIFIED' else 'PARTIAL_FACTS_READY',
                'steps':state['steps']+['finalize']}

        graph = StateGraph(DiagnosticState)
        nodes = [("retrieve",retrieve),("route",route_node),("requirements",requirements_node),("plan",plan_node),
            ("execute",execute),("feature",feature_node),("states",states_node),("coverage",coverage_node),
            ("verify",verify),("goals",goals_node),("loop_prepare",loop_prepare),("loop_plan",loop_plan),
            ("loop_execute",loop_execute),("loop_evaluate",loop_evaluate),("report",report_node),
            ("explain",explain),("completion_gate",completion_gate),("finalize",finalize)]
        for name,node in nodes:
            graph.add_node(name,measured('agent_'+name)(node))
        graph.add_node('rule_plan',measured('agent_rule_plan')(plan_node))
        graph.add_edge(START,'retrieve')
        chain=['retrieve','route','requirements']
        for a,b in zip(chain,chain[1:]):
            graph.add_edge(a,b)
        graph.add_conditional_edges('requirements',lambda state:'rule_plan' if state['route']=='FEATURE' else 'plan',
                                    {'rule_plan':'rule_plan','plan':'plan'})
        graph.add_edge('rule_plan','execute'); graph.add_edge('plan','execute')
        chain=['execute','feature','states','coverage','verify','goals','loop_prepare','loop_plan']
        for a,b in zip(chain,chain[1:]):
            graph.add_edge(a,b)
        graph.add_conditional_edges('loop_plan',lambda state:'loop_execute' if state['loop_ready'] else 'report')
        graph.add_edge('loop_execute','loop_evaluate'); graph.add_edge('loop_evaluate','loop_plan')
        graph.add_edge('report','explain'); graph.add_edge('explain','completion_gate')
        graph.add_conditional_edges('completion_gate',lambda state:'loop_plan' if state['loop_reenter'] else 'finalize')
        graph.add_edge('finalize',END)
        connection = sqlite3.connect(self.store.db_path,check_same_thread=False)
        try:
            compiled = graph.compile(checkpointer=SqliteSaver(connection))
            with model_budget(remaining()):
                final = compiled.invoke({"question":question,"action_id":action_id,"scope":json.loads(dumps(scope.__dict__)),
                                     "artifact_refs":[],"refs":[],"notices":[],"steps":[]},
                                    {"configurable":{"thread_id":sid},"recursion_limit":96})
        finally:
            connection.close()
        result = {"session_id":sid,"action_id":action_id,"question":question,"scope":scope.__dict__,
                  "dataset_id":meta["dataset_id"],"dataset_fingerprint":meta["dataset_fingerprint"],
                  "answer":final["answer"],"notices":final["notices"],"status":final["status"],"model_status":ephemeral["model_status"],
                  "verification_loop":final["loop_state"],"session_states":ephemeral.get("session_states",{}),"prompt_version":PROMPT_VERSION,"analysis_context":context,"inherited_artifacts":(parent.get("artifacts",[])+([inherited_cohort] if inherited_cohort and inherited_cohort.get("event_id") else [])) if parent else [],"route":final['route'],"goals":final['goals'],
                  "feature_binding":({'path':ephemeral['selected_feature']['path'],'version':ephemeral['selected_feature']['version'],
                                      'rule_hash':identity(ephemeral['selected_feature']['metadata'])} if ephemeral.get('selected_feature') else {}),
                  "responsibility_evidence":ephemeral["responsibility_evidence"],"requirements":final["requirements"],"backfill_rounds":final["backfill_rounds"],"validation_errors":ephemeral["validation_errors"],"plan":final["plan"],"steps":final["steps"],"references":final["refs"],"retrieval_mode":final["retrieval_mode"],
                  "case_assessments":ephemeral["case_assessments"],"peers":ephemeral["peers"],"summary":ephemeral["summary"],"artifacts":ephemeral["artifacts"],"feature":ephemeral["feature"],
                  "hypotheses":ephemeral["hypotheses"],"claims":ephemeral.get("claims",[]),"visual":ephemeral["visual"]}
        self.store.finish(sid,action_id,result)
        return json.loads(dumps(result))
