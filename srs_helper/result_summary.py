"""Full-relation facts shared by reports and model evidence; previews are examples."""
import hashlib
import json
import math
from pathlib import Path

from .sessions import dumps
from .performance import measured

BASE_UNITS = {'instBfSinrGain':'dB', 'usrRsrp':'dBm', 'srsUeNiDb':'dBm',
              'gain_avg':'dB', 'rsrp_avg':'dBm', 'ni_avg':'dBm'}


def field_units():
    from .question_scope import mapping
    units = dict(BASE_UNITS)
    for entry in mapping()[0]['fields']:
        units[entry['canonical_field']] = (entry.get('unit') or 'raw') if entry.get('meaning_status')!='RAW_VALUE_ONLY' and entry.get('scale') in (None,1) else 'raw'
    return units


def persist_summary(artifact, summary):
    summary = {'version':'evidence-summary.v1', 'artifact_id':artifact['artifact_id'],
               'artifact_sha256':artifact['sha256'], 'dataset_fingerprint':artifact['dataset_fingerprint'],
               'scope':artifact['scope'], 'input_cohort_id':artifact.get('input_cohort_id'), **summary}
    content = dumps(summary).encode('utf-8')
    path = Path(artifact['path']).with_suffix('.summary.json')
    with path.open('xb') as stream:
        stream.write(content)
    artifact['summary_ref'] = {'path':str(path), 'sha256':hashlib.sha256(content).hexdigest()}
    artifact['evidence_summary'] = json.loads(content)


def read_summary(artifact):
    ref = artifact.get('summary_ref')
    if not ref: return None
    content = Path(ref['path']).read_bytes()
    if hashlib.sha256(content).hexdigest() != ref['sha256']:
        raise ValueError('Summary hash mismatch')
    summary = json.loads(content)
    if summary['artifact_id'] != artifact['artifact_id'] or summary['artifact_sha256'] != artifact['sha256']:
        raise ValueError('Summary source mismatch')
    return summary


@measured('evidence_summary')
def summarize_relation(con, name, artifact, step=None, source_units=None, extra=None):
    """One narrow aggregate over the executed relation, no source-table pivot."""
    from .plans import identifier, expression_sql
    schema = {r[0]:r[1] for r in con.execute('DESCRIBE "'+name+'"').fetchall()}
    units = {c:(source_units or field_units()).get(c,'raw') for c in schema}
    numeric = []
    groups = []
    if step:
        groups = step.group_by
        if step.operator == 'GROUP_AGG':
            numeric = [m.output for m in step.metrics]
            for m in step.metrics:
                units[m.output] = 'count' if m.aggregate=='count' else (source_units or {}).get(m.field,field_units().get(m.field,'raw'))
            units['sample_cnt'] = 'count'
        elif step.operator == 'COMPARE':
            numeric = [step.compare_field, 'comparison_diff']
            units['comparison_diff'] = units.get(step.compare_field,'raw')
        elif step.operator == 'CONDITION_COUNT':
            numeric = ['sample_cnt']+[f'{c}_{suffix}' for c in step.conditions for suffix in ('true','false','unknown')]
            for c in numeric: units[c] = 'count'
    if not numeric:
        numeric = [c for c in schema if c in field_units() or c == 'value']
    numeric = [c for c in dict.fromkeys(numeric) if c in schema and any(t in schema[c] for t in ('INT','DOUBLE','FLOAT','DECIMAL','REAL'))]
    facts = []
    def fact(key, value, unit, label):
        if isinstance(value,float) and not math.isfinite(value): value=None
        facts.append({'fact_id':key, 'value':value, 'unit':unit, 'label':label})
    fact('result_rows', artifact['row_count'], 'count', '完整结果行数（按该算子结果粒度）')
    select = []; specs = []
    for c in numeric:
        q = identifier(c,schema)
        for op,label in [('count','有效值数'),('min','最小值'),('max','最大值')]:
            select.append(f'{op}({q})')
            specs.append((c+'.'+op,'count' if op=='count' else units[c],c+' '+label))
        if step and step.operator=='CONDITION_COUNT':
            select.append(f'coalesce(sum({q}),0)')
            specs.append((c+'.total','count',c+' 各组总计'))
        # Aggregate result columns are not averaged a second time.
        if not step or step.operator not in {'GROUP_AGG','CONDITION_COUNT'}:
            select.append(f'avg({q})'); specs.append((c+'.avg',units[c],c+' 均值'))
    for c in ('related','barrier'):
        if c in schema:
            for state in ('TRUE','FALSE','NULL'):
                select.append(f'count(*) FILTER (WHERE "{c}" IS {state})')
                specs.append((c+'.'+state,'count',c+' '+state+' 条数'))
    if 'event_ts' in schema:
        select += ['min(event_ts)','max(event_ts)']
        specs += [('time.start','timestamp','最早观测'),('time.end','timestamp','最晚观测')]
    if select:
        row = con.execute('SELECT '+','.join(select)+' FROM "'+name+'"').fetchone()
        for (key,unit,label),value in zip(specs,row): fact(key,value,unit,label)
    if step and step.operator in {'FILTER','TIME_SLICE'}:
        source_cols = {r[0] for r in con.execute('DESCRIBE "'+step.source+'"').fetchall()}
        params=[]
        condition = expression_sql(step.condition,source_cols,params) if step.operator=='FILTER' else 'event_ts>=? AND event_ts<?'
        if step.operator=='TIME_SLICE': params=[step.start,step.end]
        row=con.execute('SELECT count(*),count(*) FILTER(WHERE ('+condition+') IS FALSE),count(*) FILTER(WHERE ('+condition+') IS NULL) FROM "'+step.source+'"',params+params).fetchone()
        for key,value,label in zip(('source_rows','excluded_false','excluded_unknown'),row,('来源关系行数（本步骤分母）','条件不满足行数','条件未知行数')):
            fact(key,value,'count',label)
    limitations = list(artifact.get('query_plan',{}).get('limitations',[]))
    if step and step.operator=='COMPARE': limitations.append('comparison_diff 为组内最大值减最小值；不是双数据集比较。')
    if step and step.operator=='TEMPORAL_RELATE': limitations.append('related 表示前向窗口内是否存在观测，不是配对数量或因果证据。')
    if step and step.operator=='CONDITION_COUNT': limitations.append('条件可能重叠，不可相加为原因贡献；unknown 不等于 false。')
    grouped = {}
    if step and step.operator in {'GROUP_AGG','CONDITION_COUNT'}:
        order=' ORDER BY '+','.join(identifier(c,schema) for c in groups) if groups else ''
        rows=con.execute('SELECT * FROM "'+name+'"'+order+' LIMIT 201').fetchall()
        grouped={'group_results':[dict(zip(schema,r)) for r in rows[:200]],
                 'group_results_truncated':len(rows)>200}
    persist_summary(artifact, {'operator':artifact['operator'], 'schema':schema, 'column_units':units,
        'group_by':groups, 'step':step.model_dump(mode='json') if step else None,
        'source_relation':step.source if step else None, 'parent_artifact_ids':artifact.get('parent_artifact_ids',[]),
        'facts':facts, 'limitations':limitations, **grouped, **(extra or {})})
    return units


def limitations_for(plan, artifacts):
    values = ['计划自述：'+s for s in (plan or {}).get('limitations',[]) if isinstance(s,str)]
    for a in artifacts:
        summary=read_summary(a)
        if summary:
            values += summary.get('limitations',[])
            if any(u=='raw' for u in summary.get('column_units',{}).values()):
                values.append('未确认单位的指标使用原始数值，不做物理单位换算。')
        values += a.get('limitations',[])
    return list(dict.fromkeys(values))


def summary_report(artifacts):
    from .canvas import describe_artifact
    lines=[]
    for a in artifacts:
        s=read_summary(a)
        if not s or a['operator']=='METRIC_TIME_SERIES': continue
        lines.append(describe_artifact(a,a.get('query_plan')))
        lines.append('；'.join(f"{f['label']}：{f['value']}" for f in s['facts']))
        if a['operator'] in {'GROUP_AGG','CONDITION_COUNT'}:
            lines.append('分组结果（覆盖预览，完整结果可下载）：'+'；'.join('、'.join(str(k)+'='+str(v) for k,v in row.items()) for row in a['preview'][:8]))
        lines.append('证据：'+a['artifact_id'])
    return '\n'.join(lines)


def model_evidence(artifacts, max_chars=32000):
    """A bounded evidence envelope with explicit omissions and full-summary references."""
    evidence=[]; omitted=[]; used=0
    for a in sorted(artifacts,key=lambda a:bool(a.get('summary_ref')),reverse=True):
        s=read_summary(a)
        entry={'artifact_id':a['artifact_id'],'operator':a['operator'],'row_count':a['row_count'],
               'scope':a.get('scope'), 'summary':s, 'preview':a.get('preview',[])[:6],
               'preview_policy':a.get('preview_policy'), 'examples_only':True}
        size=len(dumps(entry))
        if used+size>max_chars:
            entry['preview']=[]
            if s:
                entry['summary']={k:v for k,v in s.items() if k not in {'schema','column_units'}}
            size=len(dumps(entry))
        if used+size>max_chars:
            omitted.append(a['artifact_id']); continue
        evidence.append(entry); used+=size
    return {'artifacts':evidence,'omitted_artifact_ids':omitted,'char_budget':max_chars,
            'policy':'全量摘要可用于核验；样例不可估计总体。省略证据不能视为不存在，必要时继续定向查询。'}


def bounded_explanation_context(payload, max_chars=64000):
    """Bound the whole human message, including specialized feature evidence."""
    def compact(value):
        if isinstance(value,dict):
            return {k:([{'artifact_id':a.get('artifact_id'),'operator':a.get('operator'),'row_count':a.get('row_count')} for a in v]
                       if k=='artifacts' and isinstance(v,list) and all(isinstance(a,dict) and 'path' in a for a in v) else compact(v))
                    for k,v in value.items()}
        if isinstance(value,list): return [compact(v) for v in value]
        return value
    result=compact(payload)
    omitted=[]
    for key in ('knowledge','case_assessments','session_states','feature_summary','evidence','requirements','verification_state'):
        if len(dumps(result))<=max_chars: break
        if key in result:
            result[key]={'omitted':True,'reason':'上下文字符预算；不是没有数据，必要时继续定向查询'}
            omitted.append(key)
    result['context_omissions']=omitted
    if len(dumps(result))>max_chars:
        from .services import ServiceError
        raise ServiceError('解释上下文超过预算，保留确定性报告',code='CONTEXT_LIMIT')
    return dumps(result)
