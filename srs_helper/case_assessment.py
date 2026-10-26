"""Evaluate source-backed case criteria; similarity never establishes root cause."""
from .evidence import feature_analysis
from .knowledge import FeatureCondition


def assess_cases(directory,scope,meta,documents,output_dir):
    assessments,artifacts=[],[]
    for doc in [d for d in documents if d['kind']=='case'][:3]:
        item={'path':doc['path'],'version':doc['version'],'status':'REFERENCE_ONLY','route':'EXPLORATION',
              'limitation':'相似性或必要条件满足不确认根因。'}
        criteria=doc['metadata'].get('criteria',[])
        if criteria:
            try:
                conditions=[FeatureCondition.model_validate(c) for c in criteria]
                for condition in conditions:
                    source=condition.source
                    if source.revision not in {doc['version'],str(doc['metadata'].get('revision',1))} or source.document_id!=doc['path'] or not source.excerpt or source.excerpt not in doc['body']:
                        raise ValueError('Criteria need a literal source excerpt in this case')
                feature={'feature_id':doc['metadata']['case_id'],'title':doc['metadata']['title'],
                         'flag_field':'__case_observation_only','conditions':[c.model_dump() for c in conditions]}
                result=feature_analysis(directory,scope,meta,feature,output_dir)
                artifacts.extend(result.pop('artifacts'))
                necessary=[c for c in conditions if c.role=='NECESSARY']
                blockers=[c for c in conditions if c.role=='BLOCKING']
                counts=result['conditions']
                if any(counts[c.condition_id].get('True',0) for c in blockers):
                    item['status']='BLOCKING_CRITERIA_OBSERVED'
                elif not necessary or not result['sample_cnt'] or any(counts[c.condition_id].get('None',0) for c in necessary+blockers):
                    item['status']='INSUFFICIENT'
                elif any(counts[c.condition_id].get('False',0) for c in necessary):
                    item['status']='NOT_SUPPORTED'
                else:
                    item.update(status='NECESSARY_CRITERIA_SUPPORTED',route='CASE_SUPPORTED_INVESTIGATION')
                item['evaluation']=result
            except (ValueError,KeyError,TypeError):
                item['status']='INVALID_OR_UNSUPPORTED_CRITERIA'
        assessments.append(item)
    return assessments,artifacts
