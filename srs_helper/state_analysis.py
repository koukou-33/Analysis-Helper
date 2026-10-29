"""Session-local observed state changes, never inferred continuous activation."""
import json
from itertools import groupby
from collections import Counter
import pyarrow as pa
import pyarrow.parquet as pq
import duckdb
from .provenance import ArtifactWriter, bind_cohort
from .database import open_dataset


def state_analysis(directory,scope,feature,source,folder):
    from .evidence import verify_artifact
    verify_artifact(source)
    spec=feature['feature']; flag=spec['flag_field']
    names=set(pq.ParquetFile(source['path']).schema_arrow.names)
    ids=[c['condition_id'] for c in spec['conditions']]
    fields=set()
    def visit(expr):
        fields.update(v for k,v in expr.items() if k in {'field','value_field'} and isinstance(v,str))
        for arg in expr.get('args',[]): visit(arg)
    for condition in spec['conditions']: visit(condition['expression'])
    fields.update(n for n in names if n.startswith(('__cell_raw_','__cell_fill_')))
    cols=[c for c in ['record_id','ue_uid','event_ts',flag]+ids+sorted(fields) if c in names]
    table=pq.read_table(source['path'],columns=list(dict.fromkeys(cols)))
    unassigned=sum(v is None for v in table["ue_uid"].to_pylist())
    missing_time=sum(v is None for v in table["event_ts"].to_pylist())
    selected_ids=pa.table({'record_id':table['record_id']})
    # Keep barriers from records excluded by quality or the explicitly selected population.
    with bind_cohort(None), open_dataset(directory) as (con,_):
        con.register('observed_ids',selected_ids)
        where,params=scope.where()
        barriers=con.execute('SELECT ue_uid,event_ts FROM l3_cleaned_srs LEFT JOIN assigned_sessions USING(record_id) '
            'WHERE ue_uid IS NOT NULL AND event_ts IS NOT NULL AND ('+where+') '
            'AND record_id NOT IN (SELECT record_id FROM observed_ids)',params).fetchall()
    obs_schema=pa.schema([('ue_uid',pa.string()),('event_ts',pa.timestamp('us')),('state',pa.string()),('segment_index',pa.int64()),
                          ('record_ids',pa.list_(pa.string())),('conditions_json',pa.string()),('values_json',pa.string())])
    event_schema=pa.schema([('event_id',pa.string()),('ue_uid',pa.string()),('before_time',pa.timestamp('us')),('after_time',pa.timestamp('us')),
        ('before_state',pa.string()),('after_state',pa.string()),('before_record_ids',pa.list_(pa.string())),('after_record_ids',pa.list_(pa.string())),
        ('before_conditions_json',pa.string()),('after_conditions_json',pa.string()),('before_values_json',pa.string()),('after_values_json',pa.string()),
        ('changed_conditions',pa.list_(pa.string())),('interpretation',pa.string()),('elapsed_seconds',pa.float64())])
    session_schema=pa.schema([('ue_uid',pa.string()),('observed_category',pa.string()),('enabled_records',pa.int64()),('disabled_records',pa.int64()),('uncertain_records',pa.int64()),('observed_changes',pa.int64())])
    obs_writer=ArtifactWriter(folder,source,'SESSION_OBSERVATIONS',obs_schema,source)
    event_writer=ArtifactWriter(folder,source,'OBSERVED_CHANGES',event_schema,source)
    session_writer=ArtifactWriter(folder,source,'SESSION_STATES',session_schema,source)
    categories=Counter()
    con=duckdb.connect(config={'memory_limit':'512MB','threads':'2'})
    con.register('observed',table)
    barrier_table=pa.Table.from_pylist([{'ue_uid':uid,'event_ts':ts} for uid,ts in barriers],schema=pa.schema([('ue_uid',pa.string()),('event_ts',pa.timestamp('us'))]))
    con.register('barriers',barrier_table)
    cursor=con.execute("SELECT *,false barrier FROM observed WHERE ue_uid IS NOT NULL AND event_ts IS NOT NULL UNION ALL BY NAME SELECT ue_uid,event_ts,true barrier FROM barriers WHERE ue_uid IN (SELECT ue_uid FROM observed) ORDER BY ue_uid,event_ts")
    def stream():
        for batch in cursor.fetch_record_batch(2048):
            yield from batch.to_pylist()
    try:
        for uid,user_rows in groupby(stream(),key=lambda r:r['ue_uid']):
            previous=None; segment=0; user_counts=Counter(); user_events=0; had_gaps=False
            for ts,group in groupby(user_rows,key=lambda r:r['event_ts']):
                group=list(group); real=[r for r in group if not r.get('barrier')]
                record_ids=[r['record_id'] for r in real]
                if len(group)>10000: raise ValueError('Too many simultaneous observations; narrow scope')
                values={('ENABLED' if r.get(flag)==spec['enabled_value'] else 'DISABLED' if r.get(flag)==spec['disabled_value'] else 'UNKNOWN') for r in real}
                state='GAP' if any(r.get('barrier') for r in group) else next(iter(values)) if len(values)==1 else 'AMBIGUOUS'
                conditions={name:(real[0].get(name) if real and all(r.get(name)==real[0].get(name) for r in real) else None) for name in ids}
                measurements={name:(real[0].get(name) if real and all(r.get(name)==real[0].get(name) for r in real) else None) for name in sorted(fields)}
                if previous is None or previous['state']!=state or state not in {'ENABLED','DISABLED'}:
                    segment+=1
                had_gaps = had_gaps or state not in {'ENABLED','DISABLED'}
                obs={'ue_uid':uid,'event_ts':ts,'state':state,'segment_index':segment,'record_ids':record_ids,
                     'conditions_json':json.dumps(conditions,sort_keys=True),'values_json':json.dumps(measurements,sort_keys=True,default=str)}
                obs_writer.append(obs); user_counts[state]+=len(real)
                if previous and state in {'ENABLED','DISABLED'} and previous['state'] in {'ENABLED','DISABLED'} and state!=previous['state']:
                    before=json.loads(previous['conditions_json'])
                    changed=[name for name in ids if before[name] is not None and conditions[name] is not None and before[name]!=conditions[name]]
                    event_writer.append({'event_id':f"{uid}:{previous['event_ts'].isoformat()}:{ts.isoformat()}",
                        'ue_uid':uid,'before_time':previous['event_ts'],'after_time':ts,'before_state':previous['state'],'after_state':state,
                        'before_record_ids':previous['record_ids'],'after_record_ids':record_ids,
                        'before_conditions_json':previous['conditions_json'],'after_conditions_json':obs['conditions_json'],
                        'before_values_json':previous['values_json'],'after_values_json':obs['values_json'],
                        'changed_conditions':changed,'interpretation':'条件值变化（可能含推定补齐，见前后来源），待核查' if changed else '已记录条件未见明确变化或存在缺失',
                        'elapsed_seconds':(ts-previous['event_ts']).total_seconds()})
                    user_events+=1
                previous=obs if state in {'ENABLED','DISABLED'} else None
            states={key for key in ('ENABLED','DISABLED') if user_counts[key]}
            category='MIXED' if len(states)==2 else next(iter(states)) if states else 'UNKNOWN'
            if had_gaps:
                category+='_WITH_GAPS'
            categories[category]+=1
            session_writer.append({'ue_uid':uid,'observed_category':category,'enabled_records':user_counts['ENABLED'],
                                 'disabled_records':user_counts['DISABLED'],'uncertain_records':user_counts['UNKNOWN']+user_counts['AMBIGUOUS']+user_counts['GAP'],
                                 'observed_changes':user_events})
    finally:
        con.close()
        for writer in (obs_writer,event_writer,session_writer):
            writer.flush(); writer.writer.close()
    artifacts=[writer.finish() for writer in (obs_writer,event_writer,session_writer)]
    return {'status':'VERIFIED' if session_writer.count else 'INSUFFICIENT','session_count':session_writer.count,'observed_changes':event_writer.count,
            'unassigned_records':unassigned,'missing_time_records':missing_time,
            'categories':dict(categories),
            'limitations':['仅会话内相邻可排序观测；不推断间隔内持续状态或精确转换时刻','未知、冲突、质量排除及群体之外的观测断开','变化前后条件相符不证明根因；不计算用户级原因占比'],
            'artifact_ids':[a['artifact_id'] for a in artifacts]},artifacts
