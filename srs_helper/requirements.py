"""Question-scoped, provenance-bearing descriptive requirements and evidence accounting."""
import re
import os
import pyarrow.parquet as pq


def analysis_budget():
    values = {'rounds':int(os.getenv('SRS_BACKFILL_MAX_ROUNDS','2')),
              'fields':int(os.getenv('SRS_BACKFILL_FIELD_BATCH','10')),
              'seconds':float(os.getenv('SRS_ANALYSIS_BUDGET_SECONDS','120'))}
    if not (0 <= values['rounds'] <= 5 and 1 <= values['fields'] <= 10 and 1 <= values['seconds'] <= 600):
        raise ValueError('Invalid analysis budget')
    return values


def question_requirements(question, columns, refs=()):
    fields = []
    for field, words in [('usrRsrp',('rsrp','接收功率')), ('srsUeNiDb',('ni','干扰')),
                         ('instBfSinrGain',('gain','增益','四流')), ('ueLayerNum',('流数',)),
                         ('userNum',('usernum','码分用户'))]:
        if any((re.search(r'(?<![a-z])'+word+r'(?![a-z])',question.lower()) if word.isascii() else word in question) for word in words):
            fields.append(field)
    for field in sorted(set(columns['resource']) | set(columns['stream'])):
        if re.search(r'(?<![\w])'+re.escape(field)+r'(?![\w])',question):
            fields.append(field)
    requirements = [{"id":f"metric_{i}", "field":field, "purpose":"DESCRIPTIVE_STATISTICS",
             "source":"USER_QUESTION", "source_excerpt":question, "status":"NOT_QUERIED",
             "relation":"stream" if field=='instBfSinrGain' else "resource",
             "artifacts":[], "valid_count":None}
            for i,field in enumerate(dict.fromkeys(fields))]
    for ref in refs:
        # Only explicitly relevant structured documents can add requirements.
        # Similarity retrieval alone does not authorize every metric in a document.
        terms = ref.get('question_terms',[])
        if not terms or not any(isinstance(t,str) and t and t in question for t in terms):
            continue
        for field in ref.get('required_metrics',[]):
            if not isinstance(field,str) or any(r['field']==field for r in requirements):
                continue
            requirements.append({'id':f'metric_{len(requirements)}','field':field,'purpose':'DESCRIPTIVE_STATISTICS',
                'source':ref['path'],'source_version':ref['version'],'source_excerpt':field,
                'relation':'stream' if field=='instBfSinrGain' else 'resource','status':'NOT_QUERIED','artifacts':[],'valid_count':None})
    return requirements


def inventory_requirements(con, meta, requirements):
    aliases = meta['contract'].get('field_mapping',{}).get('aliases',{})
    types = {r[0]:r[1] for r in con.execute('DESCRIBE l4_ue_resource_ts').fetchall()}
    standardized = {r[0] for r in con.execute('DESCRIBE l2_standardized').fetchall()}
    for item in requirements:
        field = item['field']
        names = [f'instBfSinrGain[{i}]' for i in range(4)] if field=='instBfSinrGain' else [aliases.get(field,field)]
        placeholders = ','.join('?' for _ in names)
        count,present,missing = con.execute(f'SELECT count(*),sum(present_count),sum(missing_count) FROM field_catalog WHERE raw_name IN ({placeholders})',names).fetchone()
        item['field_presence'] = 'ABSENT' if not count else 'ALL_NULL' if not present else 'PARTIAL_MISSING' if missing else 'PRESENT'
        if not count:
            item['status'] = 'NOT_AVAILABLE'
        elif not present:
            item['status'] = 'ALL_NULL'
        if field in types and not any(t in types[field] for t in ('INT','DOUBLE','FLOAT','DECIMAL')):
            item['status'] = 'UNSUPPORTED_TYPE'
        item['value_state'] = 'NOT_CHECKED'
        if count and present and field in standardized and item['status']!='UNSUPPORTED_TYPE':
            quoted = '"'+field.replace('"','""')+'"'
            valid = con.execute(f'SELECT count({quoted}) FROM l2_standardized').fetchone()[0]
            item['value_state'] = 'HAS_NUMERIC_VALUES' if valid else 'PARSE_FAILED'
            if not valid:
                item['status'] = 'PARSE_FAILED'
        if count and present and field not in types and field!='instBfSinrGain':
            valid,errors = con.execute("SELECT count(*) FILTER(WHERE parse_state='VALID'),count(*) FILTER(WHERE parse_state<>'VALID') FROM l2_extra_numeric WHERE field_name=?",[field]).fetchone()
            item['value_state'] = 'PARSE_FAILED' if errors and not valid else 'HAS_NUMERIC_VALUES' if valid else 'UNSUPPORTED_ARRAY'
            if item['value_state'] in {'PARSE_FAILED','UNSUPPORTED_ARRAY'}:
                item['status'] = item['value_state']
    return requirements


def audit_requirements(requirements, artifacts, columns):
    result = []
    for original in requirements:
        item = dict(original)
        item['artifacts'] = []
        if item['field'] not in columns[item['relation']]:
            item.update(status='NOT_AVAILABLE',valid_count=None)
        elif item['status'] not in {'UNSUPPORTED_TYPE','UNSUPPORTED_ARRAY','PARSE_FAILED'}:
            counts = []
            for artifact in artifacts:
                for metric in artifact.get('metric_evidence',[]):
                    if metric['field']==item['field'] and metric['aggregate']=='count' and metric['relation']==item['relation']:
                        if not any(m['field']==item['field'] and m['relation']==item['relation'] and m['aggregate']=='avg'
                                   for m in artifact.get('metric_evidence',[])):
                            continue
                        table = pq.read_table(artifact['path'],columns=[metric['output']])
                        counts.append(sum(v or 0 for v in table.column(0).to_pylist()))
                        item['artifacts'].append(artifact['artifact_id'])
            if counts:
                item.update(valid_count=max(counts),status='COVERED' if max(counts)>0 else 'NO_VALID_VALUES')
        result.append(item)
    return result


def backfill_plan(meta, question, requirements, limit=10):
    from .plans import QueryPlan, Step, Metric
    pending = [r for r in requirements if r['status']=='NOT_QUERIED'][:limit]
    steps = []
    for relation in ('resource','stream'):
        selected = [r for r in pending if r['relation']==relation]
        if selected:
            steps.append(Step(step_id=f's{len(steps)+1}',operator='GROUP_AGG',source=relation,
                              group_by=['streamId'] if relation=='stream' else [],
                              metrics=[m for i,r in enumerate(selected) for m in (
                                  Metric(field=r['field'],aggregate='count',output={'instBfSinrGain':'gain_valid_cnt','usrRsrp':'rsrp_valid_cnt','srsUeNiDb':'ni_valid_cnt'}.get(r['field'],f'valid_{i}')),
                                  Metric(field=r['field'],aggregate='avg',output={'instBfSinrGain':'gain_avg','usrRsrp':'rsrp_avg','srsUeNiDb':'ni_avg'}.get(r['field'],f'mean_{i}')))]))
    if not steps:
        return None
    return QueryPlan(dataset_id=meta['dataset_id'],dataset_fingerprint=meta['dataset_fingerprint'],
                     question=question,steps=steps,quality_policy='EXCLUDE_ABNORMAL_RECORD' if meta.get('rule_version')=='m1.4' else 'INCLUDE_ALL_NUMERIC')
