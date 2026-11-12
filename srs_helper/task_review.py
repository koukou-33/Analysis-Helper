"""Task-level semantic review with deterministic coverage and reference gates."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class TaskAssessment(BaseModel):
    model_config=ConfigDict(extra='forbid')
    task_id: str
    status: Literal['SUPPORTED','LIMITED']
    evidence_ids: list[str]=Field(default_factory=list,max_length=30)
    reason: str=Field(min_length=1,max_length=1200)


class TaskReview(BaseModel):
    model_config=ConfigDict(extra='forbid')
    assessments: list[TaskAssessment]=Field(max_length=16)


SYSTEM='''你是分析任务覆盖审核器。只审核已执行证据，不生成原因结论、不发起查询。
逐项核对objective，返回符合schema的JSON。SUPPORTED必须提供直接支持任务的artifact_id；
有统计不等于已回答原因，有文档命中不等于规则已核实。缺少必要证据为LIMITED并明确缺口。
数据审核失败、对象遗漏、时序精度不够时不得宣称完整覆盖。只返回任务核账依据，不输出内部思考过程。
'''


def review_tasks(intent, artifacts, data_audit, propose=None):
    tasks=(intent or {}).get('tasks',[])
    if not tasks:
        return {'status':'NOT_REQUIRED','tasks':[]}
    known={a['artifact_id'] for a in artifacts}
    records=[]
    semantic=[]
    for task in tasks:
        if task['task_id'] in {'scope_coverage','observation_resolution'}:
            passed=data_audit.get('status')=='PASS'
            if task['task_id']=='observation_resolution':
                passed=passed and any(a.get('observation_version')=='observations.v2' for a in artifacts)
            records.append({**task,'status':'SUPPORTED' if passed else 'LIMITED',
                'evidence_ids':data_audit.get('evidence_ids',[]),
                'basis':'DETERMINISTIC_COVERAGE','reason':'已核对实际对象与观测覆盖。' if passed else '数据覆盖或观测精度存在未解决缺口。'})
        else:
            semantic.append(task)
    failure=None
    assessments={}
    if semantic and propose is not None:
        try:
            review=TaskReview.model_validate(propose({'tasks':semantic,'schema':TaskReview.model_json_schema()}))
            expected={t['task_id'] for t in semantic}
            for item in review.assessments:
                if item.task_id not in expected or item.task_id in assessments:
                    raise ValueError('Unknown or duplicate task')
                if not set(item.evidence_ids)<=known:
                    raise ValueError('Unknown evidence')
                if item.status=='SUPPORTED' and not item.evidence_ids:
                    raise ValueError('Unsupported completion without evidence')
                assessments[item.task_id]=item.model_dump()
        except Exception as exc:
            failure=getattr(exc,'code',type(exc).__name__)
            assessments={}
    for task in semantic:
        assessment=assessments.get(task['task_id'],{'status':'LIMITED','evidence_ids':[],
            'reason':'语义任务审核未完成；保留已执行统计，不声明已完整回答该目标。'})
        if data_audit.get('status') not in {'PASS','NOT_REQUIRED'}:
            assessment={**assessment,'status':'LIMITED','reason':'数据覆盖审核未通过，不能确认该任务完整覆盖。'}
        records.append({**task,**assessment,'basis':'MODEL_WITH_BOUND_REFERENCES'})
    return {'status':'PASS' if all(t['status']=='SUPPORTED' for t in records) else 'LIMITED',
        'tasks':records,'error':failure,
        'meaning':'语义审核是有证据引用的任务覆盖评估，不构成独立业务真值或根因证明。'}
