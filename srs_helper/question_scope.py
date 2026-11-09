"""Explicit question candidates and immutable, user-confirmed analysis requests."""
import hashlib
import json
import re
from pathlib import Path
from datetime import datetime
from uuid import uuid4
from .queries import Scope
from .database import open_dataset
from .plans import scope_columns
from .intents import temporal_intent, single_subject_candidate, VERSION as INTENT_VERSION

MAPPING=Path(__file__).resolve().parent.parent/'config/question_fields.json'
def identity(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,default=str).encode()).hexdigest()
def mapping():
    payload=json.loads(MAPPING.read_text(encoding='utf-8-sig'))
    names={}; canonical=set()
    for item in payload['fields']:
        field=item['canonical_field']
        if field in canonical: raise ValueError('重复指标定义：'+field)
        canonical.add(field)
        for name in [field,*item['aliases']]:
            key=name.casefold()
            if key in names and names[key]!=field: raise ValueError('提问别名冲突：'+name)
            names[key]=field
    return payload,identity(payload)
def scope_dict(scope):
    return {k:v.isoformat() if isinstance(v,datetime) else v for k,v in scope.__dict__.items()}
def parse_question(question,scope):
    data,version=mapping(); proposed=scope_dict(scope); issues=[]
    for field,pattern in [('crnti',r'(?:CRNTI\s*(?:为|是|=|:|：)?\s*(0x[0-9a-f]+|\d+)|(0x[0-9a-f]+|\d+)\s*(?:这个|号)?用户)'),('logic_cell_id',r'(?:logicCellId|小区)\s*(?:为|是|=|:|：)?\s*(0x[0-9a-f]+|\d+)')]:
        matches=re.findall(pattern,question,re.I)
        values={int(v,16) if v.lower().startswith('0x') else int(v) for match in matches for v in (match if isinstance(match,tuple) else (match,)) if v}
        if len(values)>1: issues.append(field+'存在多个候选，请明确选择')
        elif values:
            value=next(iter(values))
            if value>65535: issues.append(field+'超出首版支持的0..65535范围')
            elif proposed.get(field) is not None and proposed[field]!=value: issues.append(field+'文本与界面冲突，请明确选择')
            else: proposed[field]=value
    if re.search(r'今天|昨天|最近|刚才|\d{1,2}:\d{2}|\d{4}-\d{2}-\d{2}',question):
        issues.append('文本时间请通过开始/结束时间明确指定，首版不自动补造时间')
    metrics=[]
    for item in data['fields']:
        names=[item['canonical_field'],*item['aliases']]
        if any(re.search(r'(?<![A-Za-z0-9_])'+re.escape(n)+r'(?![A-Za-z0-9_])',question,re.I) if n.isascii() else n in question for n in names):
            metrics.append(item)
    if re.search(r'\d',question) and not re.search(r'用户|crnti|小区|logicCellId',question,re.I): issues.append('数字含义未明确，请核对对象和范围')
    return {'scope':proposed,'metrics':metrics,'issues':issues,'mapping_hash':version,
            'trend':temporal_intent(question), 'single_subject_candidate':single_subject_candidate(question),
            'single_subject_statement':single_subject_candidate(question) and not re.search(r'是否|是不是|可能|假设|假如|不是|不属于|吗',re.split(r'[，,。；;]',question)[0])}
def confirm_request(directory,question,scope,metrics,trend,single_subject=False,execution_mode='MANUAL_CONFIRMATION'):
    if execution_mode not in {'MANUAL_CONFIRMATION','AUTO_FROM_QUESTION'}:
        raise ValueError('未知执行方式')
    scope.where()
    for key in ('crnti','logic_cell_id','resource_id','phy_cell_inst'):
        value=getattr(scope,key)
        if value is not None and (not isinstance(value,int) or isinstance(value,bool) or value<0 or value>65535):
            raise ValueError(key+'必须为0..65535整数')
    catalog,version=mapping()
    registered={m['canonical_field']:m for m in catalog['fields']}
    if any(m not in registered for m in metrics): raise ValueError('指标未在提问语义映射中注册')
    with open_dataset(directory) as (con,meta):
        available=scope_columns(con)['resource']
        where,params=scope.where()
        count,cells,sessions=con.execute('SELECT count(*),count(DISTINCT logicCellId),count(DISTINCT ue_uid) FROM l4_ue_resource_ts WHERE '+where,params).fetchone()
    request={'revision_id':str(uuid4()),'version':'analysis-request.v1','parser_version':'explicit-scope.v1','question':question,
        'dataset_id':meta['dataset_id'],'dataset_fingerprint':meta['dataset_fingerprint'],
        'scope':scope_dict(scope),'metrics':[registered[m] for m in metrics], 'mapping_hash':version,
        'trend':bool(trend),'quality_policy':'EXCLUDE_ABNORMAL_RECORD','group_by':['logicCellId','ue_uid'],
        'chart':'OBSERVATION_LINE','bucket':'RAW_OBSERVATIONS','confirmed_at':datetime.now().isoformat(),
        'preflight':{'records':count,'cells':cells,'sessions':sessions,'unavailable_fields':[m for m in metrics if m not in available]}}
    request['intent_version']=INTENT_VERSION
    request['execution_mode']=execution_mode
    request['subject_assertion']=({'type':'USER_ASSERTED_SINGLE_SUBJECT','scope':scope_dict(scope),
        'dataset_fingerprint':meta['dataset_fingerprint'],'source':'EXPLICIT_USER_STATEMENT' if execution_mode=='AUTO_FROM_QUESTION' else 'EXPLICIT_USER_CONFIRMATION',
        'statement':question,'cross_segment_lines':False} if single_subject else None)
    request['request_hash']=identity(request)
    return request


def query_logic(request, inherited=False, peer_comparison=False):
    """Describe the fixed scope and policy, not hidden reasoning or guessed SQL."""
    scope=request['scope']
    labels={'logic_cell_id':'小区','crnti':'CRNTI','ue_uid':'观测会话','resource_id':'资源','phy_cell_inst':'物理小区'}
    parts=[label+' '+str(scope[k]) for k,label in labels.items() if scope.get(k) is not None]
    if scope.get('start'): parts.append('从 '+str(scope['start'])+'（含）')
    if scope.get('end'): parts.append('至 '+str(scope['end'])+'（不含）')
    target='、'.join(parts) or '当前数据集全部范围'
    metrics='、'.join(m.get('display_name',m['canonical_field'])+'（'+m['canonical_field']+'）' for m in request.get('metrics',[])) or '问题相关指标'
    steps=[f'查询{target}内的{metrics}','按业务有效记录统计']
    if inherited: steps.append('仅使用所选父证据群体')
    if request.get('subject_assertion'): steps.append('依据本次提问将多个 CRNTI 视为同一用户')
    if request.get('trend'): steps.append('按小区和接入会话查看时间变化，段间不连线')
    if peer_comparison: steps.append('按已请求的同侪约束比较')
    return '查询逻辑：'+'，'.join(steps)+'。'

def validate_request(request,meta,question,scope,check_mapping=True):
    content={k:v for k,v in request.items() if k!='request_hash'}
    if identity(content)!=request.get('request_hash'): raise ValueError('确认请求哈希不匹配')
    if request.get('execution_mode') not in {None,'MANUAL_CONFIRMATION','AUTO_FROM_QUESTION'}: raise ValueError('未知执行方式')
    if request.get('version')!='analysis-request.v1' or request['question']!=question or request['scope']!=scope_dict(scope): raise ValueError('问题或范围已变化，请重新确认')
    if (request['dataset_id'],request['dataset_fingerprint'])!=(meta['dataset_id'],meta['dataset_fingerprint']): raise ValueError('数据版本已变化，请重新确认')
    assertion=request.get('subject_assertion')
    if assertion and (assertion.get('type')!='USER_ASSERTED_SINGLE_SUBJECT' or assertion.get('scope')!=request['scope'] or assertion.get('dataset_fingerprint')!=request['dataset_fingerprint'] or assertion.get('cross_segment_lines') is not False):
        raise ValueError('身份说明超出本次确认范围')
    if check_mapping and request['mapping_hash']!=mapping()[1]: raise ValueError('字段语义已变化，请重新确认')
