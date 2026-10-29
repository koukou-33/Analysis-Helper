"""Versioned contexts and immutable, narrowly projected population evidence."""
import hashlib
import json
from contextvars import ContextVar
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4
import pyarrow as pa
import pyarrow.parquet as pq
import duckdb

active_cohort = ContextVar('active_cohort', default=None)
active_cohort_ref = ContextVar('active_cohort_ref', default=None)


def canonical(value):
    return json.dumps(value,sort_keys=True,ensure_ascii=False,default=str,separators=(',',':'))


def identity(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def analysis_context(meta, scope, action_id, parent=None, cohort=None):
    return {'version':'analysis.v1','analysis_id':action_id,'parent_analysis_id':parent,
            'dataset_id':meta['dataset_id'],'dataset_fingerprint':meta['dataset_fingerprint'],
            'scope':json.loads(canonical(scope.__dict__)), 'quality_policy':'EXCLUDE_ABNORMAL_RECORD',
            'etl_rule_version':meta['rule_version'],'field_mapping_hash':identity(meta['contract']),
            'analysis_unit':'record','session_identity':'logicCellId+crnti+session_start',
            'cohort_id':cohort,'cross_session_merge':False}


def save_table(table, folder, base, operator, parents=()):
    from .evidence import verify_artifact
    for parent in parents:
        verify_artifact(parent)
    aid=str(uuid4()); path=Path(folder)/(aid+'.parquet')
    path.parent.mkdir(parents=True,exist_ok=True)
    pq.write_table(table,path)
    with path.open('rb') as stream:
        digest=hashlib.file_digest(stream,'sha256').hexdigest()
    return {**{k:base[k] for k in ('dataset_id','dataset_fingerprint','scope','quality_policy')},
            'input_cohort_id':base.get('input_cohort_id'),
            'artifact_id':aid,'path':str(path.resolve()),'sha256':digest,'step_id':operator,
            'operator':operator,'row_count':table.num_rows,'preview':table.slice(0,100).to_pylist(),
            'preview_truncated':table.num_rows>100,'parent_artifact_ids':[a['artifact_id'] for a in parents]}


def feature_cohorts(feature, source, folder):
    from .evidence import verify_artifact
    verify_artifact(source)
    spec=feature['feature']; flag=spec['flag_field']
    fields=set(pq.ParquetFile(source['path']).schema_arrow.names)
    keys=[k for k in ('record_id','ue_uid','event_ts','source_file','source_file_hash','file_row_id') if k in fields]
    # One projection; never load raw JSON or unrelated supplemental columns.
    wanted=keys+([flag] if flag in fields and flag not in keys else [])+[c['condition_id'] for c in spec['conditions']]
    with duckdb.connect(config={'memory_limit':'512MB','threads':'2'}) as con:
        con.register('rows',pq.read_table(source['path'],columns=wanted))
        quoted='"'+flag.replace('"','""')+'"'
        selections={'all':('TRUE',[]), 'enabled':(quoted+'=?',[spec['enabled_value']]),
                    'disabled':(quoted+'=?',[spec['disabled_value']]),
                    'unknown':('('+quoted+' IS NULL OR '+quoted+' NOT IN (?,?))',[spec['enabled_value'],spec['disabled_value']])}
        if flag not in fields:
            selections={'all':('TRUE',[]),'enabled':('FALSE',[]),'disabled':('FALSE',[]),'unknown':('TRUE',[])}
        for condition in spec['conditions']:
            name=condition['condition_id']
            selections['condition_'+name+'_failed']=('('+quoted+'=? AND "'+name+'" IS FALSE)',[spec['disabled_value']]) if flag in fields else ('FALSE',[])
        projection=','.join('"'+k+'"' for k in keys)
        cohorts={}; artifacts=[]
        for name,(where,params) in selections.items():
            table=con.execute('SELECT '+projection+' FROM rows WHERE '+where,params).fetch_arrow_table()
            if len(set(table['record_id'].to_pylist()))!=table.num_rows:
                raise ValueError('Cohort record identity must be unique')
            artifact=save_table(table,folder,source,'COHORT_'+name.upper(),[source])
            artifacts.append(artifact)
            cohorts[name]={'cohort_id':artifact['artifact_id'],'artifact_id':artifact['artifact_id'],
                          'unit':'record','dedup_key':['record_id'],'count':table.num_rows,
                          'parent_cohort_id':None,'predicate':{'flag':flag,'state':name},
                          'context_hash':identity({k:source[k] for k in ('dataset_id','dataset_fingerprint','scope','quality_policy','input_cohort_id') if k in source}),
                          'rule_hash':identity(spec)}
        for name in ('enabled','disabled','unknown'):
            cohorts[name]['parent_cohort_id']=cohorts['all']['cohort_id']
            metric=feature['actual_metrics'][name]
            if metric['numerator']!=cohorts[name]['count'] or metric['denominator']!=cohorts['all']['count']:
                raise ValueError('Metric population mismatch')
            metric.update(numerator_cohort_id=cohorts[name]['cohort_id'],denominator_cohort_id=cohorts['all']['cohort_id'],
                          dedup_key=['record_id'],context_hash=cohorts[name]['context_hash'],rule_hash=identity(spec))
        feature['reason_metrics']={}
        for condition in spec['conditions']:
            name=condition['condition_id']; key='condition_'+name+'_failed'
            cohorts[key]['parent_cohort_id']=cohorts['disabled']['cohort_id']
            count=cohorts[key]['count']
            if count!=feature['disabled_conditions'][name].get('False',0):
                raise ValueError('Reason cohort count mismatch')
            feature['reason_metrics'][name]={}
            for denominator in ('disabled','all'):
                n=cohorts[denominator]['count']
                feature['reason_metrics'][name][denominator]={'numerator':count,'denominator':n,'value':count/n if n else None,
                    'unit':'ratio','analysis_unit':'record','dedup_key':['record_id'],
                    'numerator_cohort_id':cohorts[key]['cohort_id'],'denominator_cohort_id':cohorts[denominator]['cohort_id'],
                    'context_hash':cohorts[key]['context_hash'],'rule_hash':identity(spec)}
    return cohorts,artifacts


def validate_parent(parent, meta, scope, cohort_name):
    from .evidence import verify_artifact
    context=parent.get('analysis_context')
    if not context or (context['dataset_id'],context['dataset_fingerprint']) != (meta['dataset_id'],meta['dataset_fingerprint']):
        raise ValueError('Parent dataset mismatch or legacy context')
    if (context['scope']!=json.loads(canonical(scope.__dict__)) or context['etl_rule_version']!=meta['rule_version']
            or context['field_mapping_hash']!=identity(meta['contract'])):
        raise ValueError('Parent scope or version mismatch; start a new analysis')
    if context.get('quality_policy')!='EXCLUDE_ABNORMAL_RECORD' or context.get('analysis_unit')!='record' or context.get('cross_session_merge') is not False:
        raise ValueError('Parent quality or analysis unit mismatch')
    cohorts=(parent.get('feature') or {}).get('cohorts',{})
    if cohort_name not in cohorts:
        raise ValueError('Select an explicit available parent cohort')
    if parent.get('feature',{}).get('validation',{}).get('status')!='VERIFIED':
        raise ValueError('Parent feature evidence is not verified')
    for artifact in parent['artifacts']:
        verify_artifact(artifact)
    selected=cohorts[cohort_name]
    artifact=next(a for a in parent['artifacts'] if a['artifact_id']==selected['artifact_id'])
    if any(canonical(artifact.get(k))!=canonical(context.get(k)) for k in ('dataset_id','dataset_fingerprint','scope','quality_policy')):
        raise ValueError('Parent artifact context mismatch')
    artifact_context={k:artifact[k] for k in ('dataset_id','dataset_fingerprint','scope','quality_policy','input_cohort_id') if k in artifact}
    if selected.get('context_hash')!=identity(artifact_context) or selected.get('rule_hash')!=identity(parent['feature']['feature']):
        raise ValueError('Parent cohort provenance mismatch')
    if artifact['row_count']!=selected['count'] or selected['unit']!='record' or selected['dedup_key']!=['record_id']:
        raise ValueError('Parent population contract mismatch')
    return artifact


@contextmanager
def bind_cohort(artifact):
    table=pq.read_table(artifact['path'],columns=['record_id']) if artifact else None
    token=active_cohort.set(table)
    ref_token=active_cohort_ref.set(artifact['artifact_id'] if artifact else None)
    try:
        yield
    finally:
        active_cohort.reset(token)
        active_cohort_ref.reset(ref_token)


def evidence_contains_record(artifact, record_id):
    """Allow a row or an explicitly recorded cell-inference source, never arbitrary peers."""
    from .evidence import verify_artifact
    verify_artifact(artifact)
    names=pq.ParquetFile(artifact['path']).schema_arrow.names
    policy=artifact.get('cell_parameter_policy') or {}
    fields=['__cell_fill_'+f for f in policy.get('fields',[]) if '__cell_fill_'+f in names]
    cols=(['record_id'] if 'record_id' in names else [])+fields
    if not cols:
        return False
    for batch in pq.ParquetFile(artifact['path']).iter_batches(batch_size=2048,columns=cols):
        for row in batch.to_pylist():
            if row.get('record_id')==record_id:
                return True
            for field in fields:
                source=row.get(field)
                if isinstance(source,dict) and record_id in (source.get('previous_record_id'),source.get('next_record_id')):
                    return True
    return False


def record_trace(directory, record_id):
    from .database import open_dataset
    with open_dataset(directory) as (con,meta):
        cur=con.execute('SELECT record_id,source_file,source_file_hash,file_row_id,raw_values,error_reasons FROM l2_standardized WHERE record_id=?',[record_id])
        row=cur.fetchone()
        if row is None:
            raise ValueError('Record not found in dataset')
        result=dict(zip([d[0] for d in cur.description],row))
        snapshot=Path(directory)/'snapshot'/result['source_file']
        if snapshot.parent.resolve()!=(Path(directory)/'snapshot').resolve():
            raise ValueError('Snapshot path outside dataset')
        with snapshot.open('rb') as stream:
            if hashlib.file_digest(stream,'sha256').hexdigest()!=result['source_file_hash']:
                raise ValueError('Snapshot changed')
        result['dataset_id']=meta['dataset_id']
        return result


class ArtifactWriter:
    """Bounded output buffer; full state histories never enter checkpoints or memory lists."""
    def __init__(self,folder,base,operator,schema,parent):
        self.aid=str(uuid4()); self.path=Path(folder)/(self.aid+'.parquet')
        self.writer=pq.ParquetWriter(self.path,schema); self.schema=schema
        self.base=base; self.operator=operator; self.parent=parent
        self.buffer=[]; self.preview=[]; self.count=0

    def append(self,row):
        self.buffer.append(row); self.count+=1
        if self.count>1000000: raise ValueError('State result exceeds one million rows')
        if len(self.preview)<100: self.preview.append(row)
        if len(self.buffer)>=2048: self.flush()

    def flush(self):
        if self.buffer:
            self.writer.write_table(pa.Table.from_pylist(self.buffer,schema=self.schema)); self.buffer=[]

    def finish(self):
        self.flush(); self.writer.close()
        with self.path.open('rb') as stream: digest=hashlib.file_digest(stream,'sha256').hexdigest()
        return {**{k:self.base[k] for k in ('dataset_id','dataset_fingerprint','scope','quality_policy')},
            'input_cohort_id':self.base.get('input_cohort_id'), 'artifact_id':self.aid,'path':str(self.path.resolve()),'sha256':digest,
            'step_id':self.operator,'operator':self.operator,'row_count':self.count,'preview':self.preview,
            'preview_truncated':self.count>100,'parent_artifact_ids':[self.parent['artifact_id']]}


def event_population(parent, event_id, folder):
    from .evidence import verify_artifact
    import pyarrow.compute as pc
    events=[a for a in parent['artifacts'] if a['operator']=='OBSERVED_CHANGES']
    if len(events)!=1: raise ValueError('Parent has no unique observed-change result')
    event_artifact=events[0]; verify_artifact(event_artifact)
    matches=[]
    for batch in pq.ParquetFile(event_artifact['path']).iter_batches():
        matches.extend(r for r in batch.to_pylist() if r['event_id']==event_id)
    if len(matches)!=1: raise ValueError('Select a complete event_id from the parent evidence')
    ids=set(matches[0]['before_record_ids']+matches[0]['after_record_ids'])
    all_id=parent['feature']['cohorts']['all']['artifact_id']
    source=next(a for a in parent['artifacts'] if a['artifact_id']==all_id)
    verify_artifact(source)
    table=pq.read_table(source['path'])
    subset=table.filter(pc.is_in(table['record_id'],value_set=pa.array(sorted(ids))))
    if subset.num_rows!=len(ids): raise ValueError('Event records not in parent population')
    result=save_table(subset,folder,source,'EVENT_POPULATION',[event_artifact,source])
    result['event_id']=event_id
    return result
