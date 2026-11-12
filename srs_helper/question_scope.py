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
from .intents import temporal_intent, single_subject_candidate, single_subject_statement, VERSION as INTENT_VERSION

MAPPING=Path(__file__).resolve().parent.parent/'config/question_fields.json'


def resolve_question(question,scope,directory,model=None,context=None):
    from .intent_resolution import resolve_question as resolve
    return resolve(question,scope,directory,model=model,context=context)
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
    return json.loads(json.dumps(scope.__dict__,default=lambda v:v.isoformat() if isinstance(v,datetime) else str(v)))


def normalize_scope(data):
    data=dict(data)
    for key in ('start','end'):
        if isinstance(data.get(key),str): data[key]=datetime.fromisoformat(data[key])
    return Scope(**data)


def _numbers(text):
    return [int(v,16) if v.lower().startswith('0x') else int(v) for v in re.findall(r'0x[0-9a-f]+|\d+',text,re.I)]


def _named_ids(question,label):
    number=r'(?:0x[0-9a-f]+|\d+)'
    chain=number+r'(?:\s*(?:和|与|及|、|,|，)\s*(?:'+label+r')?\s*'+number+r')*'
    return sorted({v for m in re.finditer(r'(?:'+label+r')\s*(?:为|是|=|:|：)?\s*('+chain+r')',question,re.I) for v in _numbers(m[1])})


def parse_question(question,scope):
    data,version=mapping(); proposed=scope_dict(scope); issues=[]
    selection_text=question
    exclusive=re.search(r'(?:只看|仅看|只分析|仅分析)([^。；;]+)',question)
    if exclusive: selection_text=exclusive[1]
    selection_text=re.sub(r'(?:不要|不看|排除|不包含)[^，,。；;]*(?:[，,。；;]|$)','',selection_text)
    cells=_named_ids(selection_text,'logicCellId|小区')
    crntis=_named_ids(selection_text,'crnti')
    crntis=sorted(set(crntis+_numbers(' '.join(re.findall(r'(0x[0-9a-f]+|\d+)\s*(?:这个|号)?用户',selection_text,re.I)))))
    pairs=[{'logic_cell_id':_numbers(c)[0],'crnti':_numbers(u)[0]} for c,u in re.findall(
        r'(?:小区|logicCellId)\s*(0x[0-9a-f]+|\d+)\s*(?:的)?\s*CRNTI\s*(?:为|是|=|:|：)?\s*(0x[0-9a-f]+|\d+)',selection_text,re.I)]
    if any(v>65535 for v in cells+crntis):
        issues.append('对象编号超出支持的0..65535范围')
        cells=[]; crntis=[]; pairs=[]
    if cells:
        current=([scope.logic_cell_id] if scope.logic_cell_id is not None else list(scope.logic_cell_ids or []))
        if current and set(current)!=set(cells): issues.append('小区文本与界面范围冲突，请明确选择')
        else:
            proposed['logic_cell_id']=cells[0] if len(cells)==1 else None
            if len(cells)>1: proposed['logic_cell_ids']=cells
            else: proposed.pop('logic_cell_ids',None)
    elif re.search(r'(?:所有|全部|两个|这两个|这份数据.*?)(?:的)?小区',question) and (scope.logic_cell_id is not None or scope.logic_cell_ids is not None):
        issues.append('问题要求多个或全部小区，但界面已限定小区；请核对范围')
    if len(pairs)>1 and {p['logic_cell_id'] for p in pairs}==set(cells):
        if scope.crnti is not None or scope.subjects is not None:
            issues.append('文本对象配对与已有对象范围需核对')
        else:
            proposed['crnti']=None; proposed['subjects']=pairs
    elif crntis:
        if scope.crnti is not None and (len(crntis)>1 or scope.crnti!=crntis[0]):
            issues.append('crnti文本与界面冲突，请明确选择')
        elif len(crntis)>1 and len(cells)>1:
            issues.append('多个小区与多个CRNTI的对应关系不明确，请说明配对')
        elif len(crntis)>1:
            proposed['crnti']=None; proposed['subjects']=[{'crnti':v} for v in crntis]
        else: proposed['crnti']=crntis[0]
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
            'single_subject_statement':single_subject_statement(question)}
def confirm_request(directory,question,scope,metrics,trend,single_subject=False,execution_mode='MANUAL_CONFIRMATION',intent_spec=None):
    if execution_mode not in {'MANUAL_CONFIRMATION','AUTO_FROM_QUESTION'}:
        raise ValueError('未知执行方式')
    if execution_mode=='AUTO_FROM_QUESTION' and intent_spec and intent_spec.get('unresolved_ambiguities'):
        raise ValueError('存在未解决的意图或对象范围歧义')
    scope.where()
    for key in ('crnti','logic_cell_id','resource_id','phy_cell_inst'):
        value=getattr(scope,key)
        if value is not None and (not isinstance(value,int) or isinstance(value,bool) or value<0 or value>65535):
            raise ValueError(key+'必须为0..65535整数')
    catalog,version=mapping()
    registered={m['canonical_field']:m for m in catalog['fields']}
    with open_dataset(directory) as (con,meta):
        available=scope_columns(con)['resource']
        for metric in metrics:
            if metric not in registered:
                if metric not in available: raise ValueError('指标不存在于当前数据或提问语义映射：'+metric)
                registered[metric]={'canonical_field':metric,'display_name':metric,'aliases':[],
                    'unit':None,'scale':None,'meaning_status':'RAW_VALUE_ONLY','source':'当前数据字段目录'}
        where,params=scope.where()
        count,cells,sessions=con.execute('SELECT count(*),count(DISTINCT logicCellId),count(DISTINCT ue_uid) FROM l4_ue_resource_ts WHERE '+where,params).fetchone()
        cell_rows=con.execute('SELECT logicCellId,count(*) AS records,count(DISTINCT crnti) AS users,count(DISTINCT ue_uid) AS sessions FROM l4_ue_resource_ts WHERE '+where+' GROUP BY logicCellId ORDER BY logicCellId',params).fetchall()
        object_rows=con.execute('SELECT logicCellId,crnti,ue_uid,count(*) AS records FROM l4_ue_resource_ts WHERE '+where+' GROUP BY logicCellId,crnti,ue_uid ORDER BY logicCellId,crnti,ue_uid LIMIT 257',params).fetchall()
        if single_subject and len(object_rows)>256:
            raise ValueError('同一用户声明的成员超过256个，请缩小范围并明确成员')
        if single_subject and any(row[2]>1 for row in cell_rows) and not scope.subjects and not re.search(r'(?:所有|全部|不同|多个|各个)\s*crnti.*?(?:同一(?:个)?|一个)\s*用户',question,re.I) and execution_mode!='MANUAL_CONFIRMATION':
            raise ValueError('同一小区存在多个CRNTI，请明确同一用户的成员，或明确声明这些CRNTI均属于该用户')
    request={'revision_id':str(uuid4()),'version':'analysis-request.v1','parser_version':'semantic-intent.v1' if intent_spec else 'explicit-scope.v2','question':question,
        'dataset_id':meta['dataset_id'],'dataset_fingerprint':meta['dataset_fingerprint'],
        'scope':scope_dict(scope),'metrics':[registered[m] for m in metrics], 'mapping_hash':version,
        'trend':bool(trend),'quality_policy':'EXCLUDE_ABNORMAL_RECORD','group_by':['logicCellId','ue_uid'],
        'chart':'OBSERVATION_LINE','bucket':'RAW_OBSERVATIONS','confirmed_at':datetime.now().isoformat(),
        'preflight':{'records':count,'cells':cells,'sessions':sessions,'unavailable_fields':[m for m in metrics if m not in available],
            'cell_coverage':[dict(zip(['logic_cell_id','records','users','sessions'],r)) for r in cell_rows]}}
    request['intent_version']=INTENT_VERSION
    request['execution_mode']=execution_mode
    request['subject_assertion']=({'type':'USER_ASSERTED_SINGLE_SUBJECT','scope':scope_dict(scope),
        'dataset_fingerprint':meta['dataset_fingerprint'],'source':'EXPLICIT_USER_STATEMENT' if execution_mode=='AUTO_FROM_QUESTION' else 'EXPLICIT_USER_CONFIRMATION',
        'statement':question,'cross_segment_lines':False} if single_subject else None)
    if request['subject_assertion']:
        request['subject_assertion']['members']=[dict(zip(['logic_cell_id','crnti','ue_uid','records'],r)) for r in object_rows]
    if intent_spec is not None:
        # Bind semantics to the actual request after UI edits; never reuse stale scope.
        request['intent_spec']={**intent_spec,'resolved_scope':scope_dict(scope),'metrics':list(metrics),
            'trend':bool(trend),'subject_basis':'USER_STATEMENT' if single_subject else None,
            'unresolved_ambiguities':[],
            'dataset_id':meta['dataset_id'],'dataset_fingerprint':meta['dataset_fingerprint']}
    request['request_hash']=identity(request)
    return request


def query_logic(request, inherited=False, peer_comparison=False):
    """Describe the fixed scope and policy, not hidden reasoning or guessed SQL."""
    scope=request['scope']
    labels={'logic_cell_id':'小区','crnti':'CRNTI','ue_uid':'观测会话','resource_id':'资源','phy_cell_inst':'物理小区'}
    parts=[label+' '+str(scope[k]) for k,label in labels.items() if scope.get(k) is not None]
    if scope.get('logic_cell_ids'): parts.append('小区 '+ '、'.join(map(str,scope['logic_cell_ids'])))
    if scope.get('subjects'): parts.append('对象集合 '+str(len(scope['subjects']))+' 组配对条件')
    if scope.get('start'): parts.append('从 '+str(scope['start'])+'（含）')
    if scope.get('end'): parts.append('至 '+str(scope['end'])+'（不含）')
    target='、'.join(parts) or '当前数据集全部范围'
    metrics='、'.join(m.get('display_name',m['canonical_field'])+'（'+m['canonical_field']+'）' for m in request.get('metrics',[])) or '问题相关指标'
    steps=[f'查询{target}内的{metrics}','按业务有效记录统计']
    if inherited: steps.append('仅使用所选父证据群体')
    if request.get('subject_assertion'): steps.append('依据本次用户说明将所选小区/接入记录作为同一用户联合查看，保留各小区来源')
    if request.get('trend'): steps.append('按小区和接入会话查看时间变化，段间不连线')
    if peer_comparison: steps.append('按已请求的同侪约束比较')
    return '查询逻辑：'+'，'.join(steps)+'。'

def validate_request(request,meta,question,scope,check_mapping=True):
    content={k:v for k,v in request.items() if k!='request_hash'}
    if identity(content)!=request.get('request_hash'): raise ValueError('确认请求哈希不匹配')
    if request.get('execution_mode') not in {None,'MANUAL_CONFIRMATION','AUTO_FROM_QUESTION'}: raise ValueError('未知执行方式')
    if request.get('version')!='analysis-request.v1' or request['question']!=question or scope_dict(normalize_scope(request['scope']))!=scope_dict(scope): raise ValueError('问题或范围已变化，请重新确认')
    if (request['dataset_id'],request['dataset_fingerprint'])!=(meta['dataset_id'],meta['dataset_fingerprint']): raise ValueError('数据版本已变化，请重新确认')
    assertion=request.get('subject_assertion')
    if assertion and (assertion.get('type')!='USER_ASSERTED_SINGLE_SUBJECT' or assertion.get('scope')!=request['scope'] or assertion.get('dataset_fingerprint')!=request['dataset_fingerprint'] or assertion.get('cross_segment_lines') is not False):
        raise ValueError('身份说明超出本次确认范围')
    if check_mapping and request['mapping_hash']!=mapping()[1]: raise ValueError('字段语义已变化，请重新确认')
