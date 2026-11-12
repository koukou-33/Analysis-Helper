"""Deterministic descriptions and charts for saved analysis evidence."""
import pandas as pd
import plotly.express as px

OPERATORS = {
    'FILTER':'筛选符合条件的记录', 'GROUP_AGG':'按对象分组统计',
    'CONDITION_COUNT':'统计条件满足、不满足和未知的条目',
    'CONDITION_ROWS':'逐条核对条件', 'COMPARE':'比较字段分布',
    'TIME_SLICE':'筛选指定时间范围', 'TEMPORAL_RELATE':'关联时间邻近的观测',
    'METRIC_TIME_SERIES':'查看指标随时间的观测',
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


def metric_figures(frame, artifact):
    """Joint view of cell/resource series without pretending their counters are synced."""
    if frame.empty:
        return []
    points=frame.copy()
    points.loc[points['barrier'],'value']=float('nan')
    points['小区']=points.logicCellId.astype(str)
    unit=artifact.get('unit','原始值')
    if not {'series_id','observation_index'}<=set(points.columns):
        points=points.sort_values(['logicCellId','ue_uid','segment','event_ts'],kind='stable')
        points['对象']=points['小区']+' / '+points.ue_uid.astype(str)
        figure=px.line(points,x='event_ts',y='value',color='小区',line_group=points['对象']+' / '+points.segment.astype(str),
                       markers=True,labels={'event_ts':'原始观测时间','value':unit},title=artifact.get('field','指标')+' 时间观测')
        figure.update_traces(connectgaps=False)
        return [(figure,'历史证据使用原始时间轴；秒内帧级观测需重新分析生成新证据。')]

    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
    points=points.sort_values(['logicCellId','ue_uid','series_id','observation_index'],kind='stable')
    series=list(points.groupby('series_id',sort=False))
    colors={cell:px.colors.qualitative.Plotly[i%len(px.colors.qualitative.Plotly)]
            for i,cell in enumerate(sorted(points['小区'].unique()))}
    charts=[]
    # Bound each figure's height while retaining every selected resource series.
    for offset in range(0,len(series),6):
        batch=series[offset:offset+6]
        titles=[]
        for _,part in batch:
            first=part.iloc[0]
            titles.append(f"小区 {first['小区']} · {first['ue_uid']} · 资源 {first.get('resourceId','?')} · RB {first.get('freqRbStart','?')} / {first.get('freqRbNum','?')}")
        figure=make_subplots(rows=len(batch),cols=1,shared_xaxes=False,subplot_titles=titles,
                            vertical_spacing=min(.10,.30/len(batch)))
        legends=set()
        for row,(_,part) in enumerate(batch,1):
            for _,segment in part.groupby('segment',sort=False):
                cell=segment['小区'].iloc[0]
                hover=[]
                for _,p in segment.iterrows():
                    hover.append(f"原始时间：{p['event_ts']}<br>帧/子帧/时隙：{p.get('frmNum')} / {p.get('subFrmNum')} / {p.get('slot')}"
                                 f"<br>TTI：{p.get('uwTtiCnt1')}<br>来源：{p.get('source_file')} 行 {p.get('first_file_row_id')}～{p.get('last_file_row_id')}"
                                 f"<br>顺序：{p.get('ordering_status')}<br>原始行数：{p.get('sample_cnt')}")
                figure.add_trace(go.Scatter(x=segment.observation_index,y=segment.value,mode='lines+markers',
                    name='小区 '+cell,legendgroup=cell,showlegend=cell not in legends,
                    line={'color':colors[cell]},connectgaps=False,text=hover,
                    hovertemplate='%{text}<br>值：%{y}<extra>%{fullData.name}</extra>'),row=row,col=1)
                legends.add(cell)
            ticks=part.iloc[::max(1,len(part)//10)].drop_duplicates('observation_index')
            figure.update_xaxes(title_text='帧/子帧/时隙观测序列（非时间间隔）',tickmode='array',
                tickvals=ticks.observation_index.tolist(),
                ticktext=[f"{p.get('frmNum')} / {p.get('subFrmNum')} / {p.get('slot')}" for _,p in ticks.iterrows()],row=row,col=1)
            figure.update_yaxes(title_text=unit,row=row,col=1)
        figure.update_layout(height=max(330,250*len(batch)),title=artifact.get('field','指标')+' 多小区观测',margin={'t':90})
        charts.append((figure,'各面板按来源文件与资源内的观测序列展示；帧号回绕、重置、缺测或顺序不明处断线。横轴间距不代表毫秒，面板之间不表示严格同步。'))
    return charts
