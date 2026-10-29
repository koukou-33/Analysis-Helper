"""Deterministic descriptions and charts for saved analysis evidence."""
import pandas as pd
import plotly.express as px

OPERATORS = {
    'FILTER':'筛选符合条件的记录', 'GROUP_AGG':'按对象分组统计',
    'CONDITION_COUNT':'统计条件满足、不满足和未知的条目',
    'CONDITION_ROWS':'逐条核对条件', 'COMPARE':'比较字段分布',
    'TIME_SLICE':'筛选指定时间范围', 'TEMPORAL_RELATE':'关联时间邻近的观测',
    'TIME_SERIES':'按时间统计测量值', 'SESSION_OBSERVATIONS':'查看会话状态观测',
    'OBSERVED_CHANGES':'核对状态变化前后的条件', 'SESSION_STATES':'汇总会话观测状态',
    'condition_evaluated_rows':'逐条核对条件',
}


def expression_text(expr):
    op=expr.get('op')
    if op in {'and','or'}:
        return (' 且 ' if op=='and' else ' 或 ').join('（'+expression_text(a)+'）' for a in expr.get('args',[]))
    if op=='not':
        return '不满足（'+expression_text(expr['args'][0])+'）'
    field=expr.get('field','字段')
    if op=='is_null': return field+' 缺失'
    signs={'eq':'等于','ne':'不等于','lt':'小于','le':'小于或等于','gt':'大于','ge':'大于或等于'}
    value=expr.get('value_field') if expr.get('value_field') is not None else expr.get('value')
    return f'{field} {signs.get(op,op)} {value}'


def describe_artifact(artifact, plan):
    """Describe known structured operations; never invent a translation of arbitrary SQL."""
    operator=artifact.get('operator','')
    text=OPERATORS.get(operator,'查看已保存的查询结果')+'。'
    plan=artifact.get('query_plan') or plan
    step_id=artifact.get('step_id','').removesuffix('_rows')
    steps=(plan or {}).get('steps',[]) if isinstance(plan,dict) else []
    step=next((s for s in steps if s.get('step_id')==step_id),None)
    # Feature and loop plans may reuse step ids; only bind when operator also agrees.
    if step and (step.get('operator')==operator or operator=='condition_evaluated_rows' and step.get('operator')=='CONDITION_COUNT') and artifact.get('plan_id')==(plan or {}).get('plan_id') and artifact.get('plan_id'):
        if step.get('condition'): text+='筛选条件：'+expression_text(step['condition'])+'。'
        if step.get('conditions'): text+='核对条件：'+'；'.join(expression_text(e) for e in step['conditions'].values())+'。'
        if step.get('group_by'): text+='分组字段：'+'、'.join(step['group_by'])+'。'
        aggregates={'avg':'均值','count':'有效值数量','min':'最小值','max':'最大值','sum':'总和','stddev_samp':'样本标准差'}
        if step.get('metrics'): text+='计算：'+'；'.join(m['field']+'的'+aggregates.get(m['aggregate'],m['aggregate']) for m in step['metrics'])+'。'
    return text+f"完整结果包含 {artifact.get('row_count',0)} 行；表格预览不代表全量分布。"


def feature_figures(feature):
    if not feature or feature.get('validation',{}).get('status')!='VERIFIED': return []
    count=feature.get('sample_cnt',0)
    if not count: return []
    flags=feature.get('flag_states',{})
    if sum(flags.values())!=count: return []
    rows=[{'状态':label,'条目数':flags.get(key,0),'占比':flags.get(key,0)/count}
          for key,label in [('True','生效'),('False','未生效'),('None','未知')]]
    fig=px.bar(pd.DataFrame(rows),x='状态',y='占比',color='状态',text='条目数',
               title='实际生效条目占比',hover_data=['条目数'])
    fig.update_yaxes(tickformat='.1%',range=[0,1])
    charts=[(fig,f'分母：当前范围全部 {count} 条业务有效记录，包含状态未知条目；未知不等于未生效。')]
    labels={c['condition_id']:expression_text(c['expression']) for c in feature.get('feature',{}).get('conditions',[])}
    reasons=[]
    for key,metrics in feature.get('reason_metrics',{}).items():
        for denominator,label in [('disabled','占未生效条目'),('all','占全部业务有效条目')]:
            m=metrics.get(denominator,{})
            if m.get('value') is not None:
                reasons.append({'条件':labels.get(key,key),'口径':label,'占比':m['value'],
                                '分子':m['numerator'],'分母':m['denominator']})
    if reasons:
        fig=px.bar(pd.DataFrame(reasons),x='占比',y='条件',color='口径',barmode='group',
                   hover_data=['分子','分母'],title='未生效条目中的条件不满足比例')
        fig.update_xaxes(tickformat='.1%',range=[0,1])
        charts.append((fig,'条件可能重叠，不能相加；这是条目级条件核验，不是用户级原因占比或根因确认。'))
    return charts


def observation_figure(rows):
    frame=pd.DataFrame(rows)
    if frame.empty or not {'event_ts','ue_uid','state'}<=set(frame.columns): return None
    labels={'ENABLED':'生效','DISABLED':'未生效','UNKNOWN':'未知','AMBIGUOUS':'同时间冲突','GAP':'观测断点'}
    frame['状态']=frame.state.map(labels).fillna('未知')
    return px.scatter(frame.sort_values('event_ts'),x='event_ts',y='ue_uid',color='状态',symbol='状态',
                      labels={'event_ts':'观测时间','ue_uid':'用户观测会话'},title='会话状态随时间的观测变化')
