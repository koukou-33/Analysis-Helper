"""Bounded record-predicate verification, with immutable evidence and explicit stop reasons.

Models propose predicates; only full query results decide their truth. Supporting a
predicate never promotes arbitrary model prose to a causal conclusion.
"""
from __future__ import annotations
from typing import Literal
import os
from pydantic import BaseModel, ConfigDict, Field
import pyarrow as pa
import pyarrow.parquet as pq
from .plans import Expression, QueryPlan, Step, execute_plan, validate_plan_fields
from .evidence import verify_artifact, evaluate
from .provenance import identity, canonical, bind_cohort, save_table
from .services import ServiceError
from .database import open_dataset
from .performance import validation_event

StopReason = Literal['GOALS_VERIFIED','DATA_INSUFFICIENT','CRITERIA_REQUIRED',
    'CLARIFICATION_REQUIRED','TECHNICAL_FAILURE','UNSUPPORTED_OPERATION',
    'BUDGET_EXHAUSTED','NO_PROGRESS','USER_STOPPED']

class Contract(BaseModel):
    model_config = ConfigDict(extra='forbid')

class TaskProposal(Contract):
    goal_id: str
    cohort: str
    expression: Expression
    quantifier: Literal['ALL','ANY'] = 'ALL'
    # An optional exact registered rule; otherwise this is an observational predicate.
    rule_id: str | None = None

class ValidationTask(Contract):
    task_id: str
    proposal: TaskProposal
    fingerprint: str
    query_fingerprint: str
    context_hash: str
    analysis_unit: Literal['record'] = 'record'
    status: Literal['PENDING','RUNNING','SUCCEEDED','FAILED','BLOCKED'] = 'PENDING'
    evidence_refs: list[str] = Field(default_factory=list)
    error_code: str | None = None

class HypothesisAssessment(Contract):
    task_id: str
    status: Literal['SUPPORTED','REFUTED','INCONCLUSIVE','BLOCKED_DATA','BLOCKED_CRITERIA']
    counts: dict[str,int] = Field(default_factory=dict)
    evidence_refs: list[str] = Field(default_factory=list)
    residual_cohorts: dict[str,str] = Field(default_factory=dict)
    imputed_record_count: int = 0
    interpretation: str = '仅验证所列记录条件，不确认因果或责任主体'

class MissingInformation(Contract):
    goal_id: str
    cohort: str
    field: str | None = None
    information_category: str | None = None
    missing_type: Literal['FIELD_ABSENT','MAPPING_UNRESOLVED','ALL_NULL','PARTIAL_MISSING',
        'INVALID_VALUES','TIME_COVERAGE_GAP','JOIN_KEY_MISSING','CRITERION_UNDEFINED']
    checked_artifacts: list[str] = Field(default_factory=list)
    affected_count: int | None = None
    required_to_complete: bool = True
    supplement: str

class LoopState(Contract):
    version: Literal['verification.v1'] = 'verification.v1'
    context_hash: str
    validation_tasks: list[ValidationTask] = Field(default_factory=list)
    assessments: list[HypothesisAssessment] = Field(default_factory=list)
    missing_information: list[MissingInformation] = Field(default_factory=list)
    query_ledger: dict[str,dict] = Field(default_factory=dict)
    residual_cohorts: dict[str,str] = Field(default_factory=dict)
    pending_drafts: list[dict] = Field(default_factory=list)
    round: int = 0
    planner_calls: int = 0
    duplicate_count: int = 0
    max_queries: int = Field(default=12,ge=1,le=12)
    max_planner_calls: int = Field(default=6,ge=1,le=6)
    stop_reason: StopReason | None = None
    stop_detail: str = ''
    initialized: bool = False
    planner_done: bool = False

class Proposals(Contract):
    tasks: list[TaskProposal] = Field(default_factory=list, max_length=4)


def fields_of(expr):
    return ({expr.field} if expr.field else set()) | ({expr.value_field} if expr.value_field else set()) | set().union(*(fields_of(e) for e in expr.args))


def normalized(expr):
    data=expr.model_dump(exclude_none=True)
    if expr.op in {'and','or'}:
        data['args']=sorted([normalized(e) for e in expr.args],key=canonical)
    elif expr.args:
        data['args']=[normalized(e) for e in expr.args]
    return data


def predicate_label(expr):
    if expr.op in {'and','or','not'}:
        parts=[predicate_label(e) for e in expr.args]
        return ('非('+parts[0]+')') if expr.op=='not' else '('+(' 且 ' if expr.op=='and' else ' 或 ').join(parts)+')'
    if expr.op=='is_null':
        return expr.field+' 缺失'
    return expr.field+' '+{'eq':'=','ne':'≠','gt':'>','ge':'≥','lt':'<','le':'≤'}[expr.op]+' '+str(expr.value_field if expr.value_field is not None else expr.value)


def assess_counts(quantifier, true, false, unknown):
    if not true+false+unknown:
        return 'INCONCLUSIVE'  # no vacuous universal claims about empty populations
    if quantifier=='ALL':
        return 'REFUTED' if false else 'INCONCLUSIVE' if unknown else 'SUPPORTED'
    return 'SUPPORTED' if true else 'INCONCLUSIVE' if unknown else 'REFUTED'


class VerificationLoop:
    def __init__(self, *, directory, scope, meta, context, columns, folder, artifacts,
                 feature, goals, question, remaining, persist, restored=None, rules=()):
        self.directory,self.scope,self.meta,self.context=directory,scope,meta,context
        self.columns,self.folder,self.artifacts=columns,folder,artifacts
        self.feature,self.goals,self.question=feature or {},goals,question
        self.remaining,self.persist=remaining,persist
        self.rules={r['rule_id']:r for r in rules}
        self.reason_requested=any(w in question for w in ('原因','为什么','条件','排查','验证','假设'))
        self.context_hash=identity(context)
        self.state=LoopState.model_validate(restored) if restored else LoopState(context_hash=self.context_hash,
            max_queries=int(os.getenv('SRS_VERIFY_MAX_QUERIES','12')),
            max_planner_calls=int(os.getenv('SRS_VERIFY_MAX_PLANNER_CALLS','6')))
        if self.state.context_hash!=self.context_hash:
            raise ValueError('Loop context changed')
        self.cohorts={k:v['artifact_id'] for k,v in self.feature.get('cohorts',{}).items()}
        self.cohorts.update(self.state.residual_cohorts)
        self.goal_ids={g['id'] for g in goals}
        # Successful tasks retain their immutable results across interruption/retry.
        for ledger in self.state.query_ledger.values():
            for aid in ledger.get('artifacts',[]):
                verify_artifact(self.artifact(aid))
        for task in self.state.validation_tasks:
            if task.status=='RUNNING' and task.query_fingerprint not in self.state.query_ledger:
                task.status='PENDING'

    def artifact(self, aid):
        return next(a for a in self.artifacts if a['artifact_id']==aid)

    def save(self):
        self.persist(self.state.model_dump(mode='json'))

    def gap(self, **values):
        item=MissingInformation(**values)
        if item not in self.state.missing_information:
            self.state.missing_information.append(item)

    def initialize(self):
        if self.state.initialized:
            return
        self.state.initialized=True
        if self.feature and self.feature.get('validation',{}).get('status')!='VERIFIED':
            self.stop('TECHNICAL_FAILURE','特性证据未通过核账，不能继续自动验证')
            return
        if self.reason_requested and 'disabled_conditions' in self.goal_ids:
            spec=self.feature['feature']
            source=next(a for a in self.artifacts if a.get('origin')=='FEATURE' and a['operator']=='condition_evaluated_rows')
            verify_artifact(source)
            # Existing condition evidence is reused, not called a new database query.
            ids=[c['condition_id'] for c in spec['conditions']]
            names=pq.ParquetFile(source['path']).schema_arrow.names
            required=list(dict.fromkeys(['record_id',spec['flag_field']]+ids))
            required=[n for n in required if n in names]
            residual={name:[] for name in ['expected_disabled']+['unknown_'+c for c in ids]}
            for batch in pq.ParquetFile(source['path']).iter_batches(batch_size=2048,columns=required):
                for row in batch.to_pylist():
                    if row.get(spec['flag_field'])!=spec['disabled_value']:
                        continue
                    activation=evaluate(spec['activation_logic'],row) if spec.get('activation_logic') else None
                    if activation is True:
                        residual['expected_disabled'].append(row['record_id'])
                    for c in ids:
                        if row[c] is None and activation is None:
                            residual['unknown_'+c].append(row['record_id'])
            for name,record_ids in residual.items():
                if record_ids:
                    self.save_cohort(name,record_ids,source)
            for c in spec['conditions']:
                name='unknown_'+c['condition_id']
                if name in self.cohorts:
                    self.add(TaskProposal(goal_id='disabled_conditions',cohort=name,
                        expression=Expression.model_validate(c['expression'])))
            # Registered investigative rules only apply when their trigger population exists.
            for rule in self.rules.values():
                name=rule['cohort']
                if name in self.cohorts and self.artifact(self.cohorts[name])['row_count']:
                    self.add(TaskProposal(goal_id='disabled_conditions',cohort=name,
                        expression=Expression.model_validate(rule['expression']),quantifier=rule.get('quantifier','ANY'),rule_id=rule['rule_id']))
            if 'expected_disabled' in self.cohorts:
                self.gap(goal_id='disabled_conditions',cohort='expected_disabled',
                    information_category='完整预期条件满足但实际关闭的进一步判据',missing_type='CRITERION_UNDEFINED',
                    checked_artifacts=[self.cohorts['expected_disabled']],affected_count=len(residual['expected_disabled']),
                    supplement='补充筛选到生效阶段的可执行判据或外部日志；筛选标志差异本身不能确认根因。')
        self.save()

    def save_cohort(self, name, record_ids, source):
        if name in self.cohorts:
            return
        artifact=save_table(pa.table({'record_id':pa.array(record_ids,type=pa.string())}),
            self.folder,source,'LOOP_COHORT',[source])
        artifact['origin']='VERIFICATION_LOOP'
        self.artifacts.append(artifact)
        self.state.residual_cohorts[name]=artifact['artifact_id']
        self.cohorts[name]=artifact['artifact_id']

    def add(self, proposal, validate_only=False):
        p=TaskProposal.model_validate(proposal)
        if p.goal_id not in self.goal_ids or p.goal_id=='user_reason_ratio':
            raise ValueError('Unrequested or deferred goal')
        if p.cohort not in self.cohorts:
            raise ValueError('Unknown population; cannot expand scope')
        if len(fields_of(p.expression))>16 or len(canonical(p.expression.model_dump()))>8000:
            raise ValueError('Predicate too wide')
        if p.rule_id:
            rule=self.rules.get(p.rule_id)
            if not rule or normalized(Expression.model_validate(rule['expression']))!=normalized(p.expression) or rule['cohort']!=p.cohort:
                raise ValueError('Registered rule mismatch')
        # No model-generated source, joins, unit, scope, quality policy or raw SQL accepted.
        plan=self.plan(p)
        validate_plan_fields(plan,self.columns)
        if validate_only:
            return True
        fingerprint=identity({'context':self.context_hash,'cohort':self.cohorts[p.cohort],
            'unit':'record','purpose':p.goal_id,'expression':normalized(p.expression),'quantifier':p.quantifier})
        if any(t.fingerprint==fingerprint for t in self.state.validation_tasks):
            self.state.duplicate_count+=1
            return False
        self.state.validation_tasks.append(ValidationTask(task_id=fingerprint[:24],proposal=p,
            fingerprint=fingerprint,query_fingerprint=identity({'context':self.context_hash,
                'cohort':self.cohorts[p.cohort],'unit':'record','operator':'CONDITION_COUNT',
                'expression':normalized(p.expression)}),context_hash=self.context_hash))
        self.state.planner_done=False
        if self.state.stop_reason not in {'TECHNICAL_FAILURE','NO_PROGRESS','BUDGET_EXHAUSTED','USER_STOPPED'}:
            self.state.stop_reason=None
        return True

    def plan(self, p):
        conditions={'c0':p.expression}
        # Presence is queried in exactly the same cohort, not inferred from prompt excerpts.
        for i,field in enumerate(sorted(fields_of(p.expression)),1):
            conditions[f'c{i}']=Expression(op='is_null',field=field)
        return QueryPlan(dataset_id=self.meta['dataset_id'],dataset_fingerprint=self.meta['dataset_fingerprint'],
            question=self.question,steps=[Step(step_id='s1',operator='CONDITION_COUNT',
            purpose='VERIFY_RECORD_PREDICATE',conditions=conditions)])

    def pending(self):
        return next((t for t in self.state.validation_tasks if t.status in {'PENDING','RUNNING'}),None)

    def stop(self, reason, detail=''):
        self.state.stop_reason=reason; self.state.stop_detail=detail
        self.save()

    def can_run(self):
        if self.remaining()<=0 or self.state.round>=self.state.max_queries:
            self.stop('BUDGET_EXHAUSTED','累计时间或验证查询次数耗尽；尚未证明数据不足')
            return False
        return self.state.stop_reason not in {'TECHNICAL_FAILURE','NO_PROGRESS','BUDGET_EXHAUSTED','USER_STOPPED'}

    def execute(self):
        task=self.pending()
        if task is None or not self.can_run():
            return
        if task.query_fingerprint in self.state.query_ledger:
            task.status='RUNNING'
            task.evidence_refs=list(self.state.query_ledger[task.query_fingerprint]['artifacts'])
            self.save()
            return
        task.status='RUNNING'; self.state.round+=1; self.save()
        try:
            cohort=self.artifact(self.cohorts[task.proposal.cohort])
            verify_artifact(cohort)
            with bind_cohort(cohort):
                results=execute_plan(self.directory,self.scope,self.plan(task.proposal),self.folder,
                                     seconds=max(.001,min(30,self.remaining())))
            for a in results:
                verify_artifact(a)
                a.update(origin='VERIFICATION_LOOP',verification_task_id=task.task_id)
                a['parent_artifact_ids']=list(dict.fromkeys(a.get('parent_artifact_ids',[])+[cohort['artifact_id']]))
            # Persist the full header/mapping inventory and invalid numeric counts for this cohort.
            summary=next(a for a in results if a['operator']=='CONDITION_COUNT')
            counts=pq.read_table(summary['path']).to_pylist()[0]
            fields=sorted(fields_of(task.proposal.expression))
            raw_names={n for f in self.meta['files'] if f['status']=='LOADED' for n in f['columns']}
            aliases=self.meta['contract'].get('field_mapping',{}).get('aliases',{})
            missing_fields=[field for i,field in enumerate(fields,1) if counts[f'c{i}_true'] or field not in self.columns['resource']]
            invalid={}
            if missing_fields:
                with bind_cohort(cohort), open_dataset(self.directory,seconds=max(.001,min(30,self.remaining()))) as (con,_):
                    marks=','.join('?' for _ in missing_fields)
                    invalid=dict(con.execute('SELECT e.field_name,count(*) FROM l2_extra_numeric e '
                        'SEMI JOIN selected_cohort r USING(record_id) WHERE e.field_name IN ('+marks+
                        ") AND e.parse_state<>'VALID' GROUP BY e.field_name",missing_fields).fetchall())
            inventory=[]
            for i,field in enumerate(fields,1):
                inventory.append({'field':field,'mapped_raw_name':aliases.get(field,field),
                    'raw_header_present':aliases.get(field,field) in raw_names,
                    'resource_available':field in self.columns['resource'],
                    'unknown_count':counts[f'c{i}_true'] if field in self.columns['resource'] else counts['sample_cnt'],
                    'invalid_count':invalid.get(field,0), 'sample_cnt':counts['sample_cnt']})
            if inventory:
                catalog=save_table(pa.Table.from_pylist(inventory),self.folder,summary,'LOOP_FIELD_INVENTORY',[summary,cohort])
                catalog.update(origin='VERIFICATION_LOOP',verification_task_id=task.task_id,
                    field_mapping_hash=self.context['field_mapping_hash'])
                results.append(catalog)
            self.artifacts.extend(results)
            self.state.query_ledger[task.query_fingerprint]={'task_id':task.task_id,
                'status':'SUCCEEDED','artifacts':[a['artifact_id'] for a in results]}
            task.evidence_refs=[a['artifact_id'] for a in results]
            self.save()  # Query checkpoint precedes evaluation; retry never repeats this query.
        except Exception as exc:
            task.status='FAILED'; task.error_code=type(exc).__name__
            self.stop('BUDGET_EXHAUSTED' if self.remaining()<=0 else 'TECHNICAL_FAILURE',
                      '验证查询未完成：'+type(exc).__name__)

    def evaluate(self):
        task=self.pending()
        if task is None or task.query_fingerprint not in self.state.query_ledger:
            return
        p=task.proposal
        artifacts=[self.artifact(a) for a in task.evidence_refs]
        for a in artifacts:
            verify_artifact(a)
        summary=next(a for a in artifacts if a['operator']=='CONDITION_COUNT')
        rows=pq.read_table(summary['path']).to_pylist()
        if len(rows)!=1:
            raise ValueError('Verification needs one ungrouped complete summary')
        row=rows[0]; n=row['sample_cnt']
        counts={'true':row['c0_true'],'false':row['c0_false'],'unknown':row['c0_unknown']}
        if sum(counts.values())!=n or n!=self.artifact(self.cohorts[p.cohort])['row_count']:
            raise ValueError('Verification population mismatch')
        result=HypothesisAssessment(task_id=task.task_id,status=assess_counts(p.quantifier,**counts),
            counts={'total':n,**counts},evidence_refs=task.evidence_refs)
        source=next(a for a in artifacts if a['operator']=='condition_evaluated_rows')
        result.imputed_record_count=source.get('imputed_record_count',0)
        if result.imputed_record_count:
            result.interpretation+='；本判断包含小区参数推定补齐，不能视为全部直接观测'
        partitions={'true':[],'false':[],'unknown':[]}
        for batch in pq.ParquetFile(source['path']).iter_batches(batch_size=2048,columns=['record_id','c0']):
            for r in batch.to_pylist():
                partitions['unknown' if r['c0'] is None else 'true' if r['c0'] else 'false'].append(r['record_id'])
        for key,record_ids in partitions.items():
            if record_ids:
                name=task.task_id+'_'+key
                self.save_cohort(name,record_ids,source)
                result.residual_cohorts[key]=name
        catalog=next((a for a in artifacts if a['operator']=='LOOP_FIELD_INVENTORY'),None)
        inventory=pq.read_table(catalog['path']).to_pylist() if catalog else []
        for item in inventory:
            field=item['field']; count=item['unknown_count']; invalid=item['invalid_count']
            common=dict(goal_id=p.goal_id,cohort=p.cohort,field=field,checked_artifacts=task.evidence_refs,
                required_to_complete=result.status=='INCONCLUSIVE',
                supplement='补充该群体中的 '+field+' 有效数值，检查 field_mapping 和原始 CSV；未知不等于条件不满足。')
            if invalid:
                self.gap(**common,missing_type='INVALID_VALUES',affected_count=invalid)
            missing=count-invalid
            if missing:
                kind=('MAPPING_UNRESOLVED' if item['raw_header_present'] and not item['resource_available'] else
                      'FIELD_ABSENT' if not item['raw_header_present'] and not item['resource_available'] else
                      'ALL_NULL' if missing==n else 'PARTIAL_MISSING')
                self.gap(**common,missing_type=kind,affected_count=missing)
        task.status='SUCCEEDED'
        self.state.assessments.append(result)
        self.save()

    def planner_payload(self):
        return {'question':self.question,'context':self.context,'goals':self.goals,
            'cohorts':{k:{'artifact_id':a,'count':self.artifact(a)['row_count']} for k,a in self.cohorts.items()},
            'field_catalog':self.columns['resource'], 'field_catalog_mode':'COMPLETE_RESOURCE_CATALOG',
            'rules':list(self.rules.values()),'assessments':[a.model_dump() for a in self.state.assessments],
            'tasks':[t.model_dump() for t in self.state.validation_tasks],
            'missing_information':[m.model_dump() for m in self.state.missing_information],
            'pending_hypotheses':self.state.pending_drafts,'response_schema':Proposals.model_json_schema()}

    def propose(self, callback):
        if self.pending() or not self.can_run() or self.state.planner_done:
            return
        feedback=None
        # One schema/source repair, charged to the same persistent planning budget.
        for attempt in range(2):
            if self.state.planner_calls>=self.state.max_planner_calls or self.remaining()<=0:
                self.stop('BUDGET_EXHAUSTED','验证规划调用或时间预算耗尽；目标尚未完成')
                return
            self.state.planner_calls+=1; self.save()
            raw=None
            try:
                payload=self.planner_payload()
                if feedback:
                    payload['validation_feedback']=feedback
                raw=callback(payload)
                output=Proposals.model_validate(raw)
                for p in output.tasks:
                    self.add(p,validate_only=True)
                added=sum(self.add(p) for p in output.tasks)
                if output.tasks and not added:
                    self.stop('NO_PROGRESS','规划器重复等价任务，未取得新证据；不能宣称数据不足')
                elif not output.tasks:
                    if self.state.pending_drafts:
                        self.stop('NO_PROGRESS','最终解释未给出可执行验证任务；不能据此认定数据或业务判据缺失，未核验文字不发布')
                    self.state.planner_done=True
                self.state.pending_drafts=[]
                self.save()
                return
            except ServiceError as exc:
                self.stop('BUDGET_EXHAUSTED' if exc.code=='BUDGET_EXHAUSTED' else 'TECHNICAL_FAILURE','验证规划失败：'+exc.code)
                return
            except (ValueError,TypeError) as exc:
                details=exc.errors(include_input=False,include_url=False) if hasattr(exc,'errors') else getattr(exc,'details',str(exc))
                validation_event('verification_validation',attempt+1,details)
                feedback={'details':details,'failed_output_excerpt':canonical(raw)[:8000],
                          'instruction':'修复结构、目标和来源错误；只返回任务schema，不扩展问题'}
                if attempt==1:
                    self.stop('TECHNICAL_FAILURE','验证任务契约修复失败：'+type(exc).__name__)

    def accept_explanation(self, hypotheses):
        # Pure rates/descriptive queries do not acquire unlimited new causal goals.
        if not hypotheses or not self.reason_requested:
            return False
        goal='disabled_conditions' if 'disabled_conditions' in self.goal_ids else next(iter(sorted(self.goal_ids)))
        for item in hypotheses:
            if item.get('verification'):
                try:
                    self.add(item['verification'])
                except (ValueError,TypeError):
                    self.stop('TECHNICAL_FAILURE','最终解释附带的验证任务不符合范围或契约')
            else:
                self.state.pending_drafts.append({'goal_id':goal,'text':item['text'],'references':item['references']})
        if self.state.pending_drafts:
            self.state.planner_done=False
        self.save()
        return bool(self.pending() or self.state.pending_drafts)

    def residual_criteria(self):
        by_rule={t.proposal.rule_id:t for t in self.state.validation_tasks if t.status=='SUCCEEDED'}
        pair=[by_rule.get(r) for r in ('terminal_shape_mismatch','non_serving_observation')]
        if not all(pair) or 'unexplained_code_user_switch' in self.cohorts:
            return
        pools=[]; sources=[]
        for task in pair:
            a=next(a for a in self.state.assessments if a.task_id==task.task_id)
            name=a.residual_cohorts.get('false')
            if name is None:
                return
            source=self.artifact(self.cohorts[name]); sources.append(source)
            verify_artifact(source)
            pools.append(set(pq.read_table(source['path'],columns=['record_id'])['record_id'].to_pylist()))
        unexplained=pools[0]&pools[1]
        if unexplained:
            name='unexplained_code_user_switch'
            self.save_cohort(name,sorted(unexplained),sources[0])
            artifact=self.artifact(self.cohorts[name])
            artifact['parent_artifact_ids']=[a['artifact_id'] for a in sources]
            self.gap(goal_id='disabled_conditions',cohort=name,
                information_category='终端形态和服务身份均满足但码分开关关闭的进一步判据',
                missing_type='CRITERION_UNDEFINED',checked_artifacts=[artifact['artifact_id']],
                affected_count=len(unexplained),supplement='需补充互斥条件的明确字段及规则或上游决策记录；不能仅凭 codeUsrSpfSw=0 推断互斥。')
            self.save()

    def finish(self):
        self.residual_criteria()
        if self.state.stop_reason in {'TECHNICAL_FAILURE','BUDGET_EXHAUSTED','NO_PROGRESS','USER_STOPPED'}:
            return
        if self.pending():
            self.stop('BUDGET_EXHAUSTED' if self.remaining()<=0 else 'NO_PROGRESS','仍有必要验证未执行')
        elif any(m.required_to_complete and m.missing_type=='CRITERION_UNDEFINED' for m in self.state.missing_information):
            self.stop('CRITERIA_REQUIRED','已有观测事实；进一步解释需要明确判据')
        elif any(m.required_to_complete for m in self.state.missing_information):
            self.stop('DATA_INSUFFICIENT','具体缺测已在目标群体查询确认，见缺口清单')
        elif any(a.status=='INCONCLUSIVE' for a in self.state.assessments):
            self.stop('NO_PROGRESS','存在未定判断或空群体；不从零样本确认或排除条件')
        elif any(g['id']=='user_reason_ratio' for g in self.goals):
            self.stop('CRITERIA_REQUIRED','用户级原因占比分母尚未确认')
        elif any(g['status']!='VERIFIED' for g in self.goals):
            reasons={g.get('reason') for g in self.goals}
            if 'BUDGET_EXHAUSTED' in reasons:
                self.stop('BUDGET_EXHAUSTED','已有需求补查预算耗尽')
            elif 'QUERY_FAILED' in reasons or any(g['status']=='FAILED' for g in self.goals):
                self.stop('TECHNICAL_FAILURE','已有查询或证据校验失败')
            else:
                self.stop('NO_PROGRESS','部分目标未完成；不能仅凭空结果宣称数据不存在')
        else:
            self.stop('GOALS_VERIFIED','问题范围内的记录统计及可执行判据已核验；不代表根因确认')


def loop_report(data):
    state=LoopState.model_validate(data)
    labels={'SUPPORTED':'条件判断获支持','REFUTED':'条件判断被反驳','INCONCLUSIVE':'条件判断未定',
            'BLOCKED_DATA':'数据缺口','BLOCKED_CRITERIA':'判据缺口'}
    tasks={t.task_id:t for t in state.validation_tasks}
    lines=[]
    for a in state.assessments:
        p=tasks[a.task_id].proposal; c=a.counts
        lines.append(f"{labels[a.status]}：群体 {p.cohort} 中{'全部记录' if p.quantifier=='ALL' else '存在记录'}满足 {predicate_label(p.expression)}；满足/不满足/未知为 {c['true']}/{c['false']}/{c['unknown']} 条。")
        if a.imputed_record_count:
            lines.append(f'该验证有 {a.imputed_record_count} 条记录使用小区参数推定补齐，来源见完整证据。')
    for m in state.missing_information:
        lines.append('待补充：'+(m.field or m.information_category or '')+'（'+m.cohort+'，'+m.missing_type+
            ('，'+str(m.affected_count)+' 条' if m.affected_count is not None else '')+'）。'+m.supplement)
    stop_labels={'GOALS_VERIFIED':'本问题核验完成','DATA_INSUFFICIENT':'存在已确认的数据缺口',
        'CRITERIA_REQUIRED':'需要补充判据','TECHNICAL_FAILURE':'技术故障，分析未完成',
        'BUDGET_EXHAUSTED':'预算耗尽，分析未完成','NO_PROGRESS':'暂无新进展，分析未完成',
        'UNSUPPORTED_OPERATION':'所需操作暂不支持','CLARIFICATION_REQUIRED':'需要澄清','USER_STOPPED':'用户已停止'}
    lines.append('验证循环：'+stop_labels.get(state.stop_reason,'进行中')+'。'+state.stop_detail)
    if state.assessments:
        lines.append('以上支持或反驳仅针对所列记录条件；不把模型原始解释、相关性或筛选阶段标志升格为根因。')
    return '\n\n'.join(lines)
