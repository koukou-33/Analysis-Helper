"""Bounded adaptive tool selection; model output never establishes completion."""
from typing import Literal
import re
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from .plans import QueryPlan
from .provenance import identity
from .progress import StorageFailure


class ToolAction(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: Literal['query', 'search', 'finish']
    purpose: str = Field(min_length=1, max_length=2000)
    query: QueryPlan | None = None
    search: str | None = Field(default=None, max_length=1000)

    @model_validator(mode='after')
    def action_payload(self):
        if self.action == 'query' and self.query is None:
            raise ValueError('query requires a query plan')
        if self.action == 'search' and not (self.search or '').strip():
            raise ValueError('search requires nonempty search text')
        if self.action != 'query' and self.query is not None:
            raise ValueError('query is only allowed for the query action')
        if self.action != 'search' and self.search is not None:
            raise ValueError('search is only allowed for the search action')
        return self


TOOL_SYSTEM = '''你是当前固定数据范围的分析工具调度器。根据问题、已有证据和缺口选择下一步。
仅返回符合schema的JSON：query执行受控QueryPlan，search检索相关规则或案例，finish表示没有新的合法必要行动。
finish不是根因确认或目标完成。不得改变数据集、范围、质量口径；不得虚构字段、阈值和时间窗口。
优先复用已有结果，避免重复查询。没有证据不能宣称数据不足。统计、比较和时序可组合QueryPlan算子。
validation_feedback 是前次结构校验错误，只修正相应结构；不要重复执行已成功任务。用户声明、数据事实与未确认规则必须区分。
'''


def validation_details(exc):
    """Never persist Pydantic input/ctx or a raw model response in diagnostics."""
    details = []
    for error in exc.errors(include_url=False, include_context=False, include_input=False)[:12]:
        location = [v if isinstance(v, int) else str(v)[:80]
                    if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', str(v)) else '<field>'
                    for v in error['loc']]
        # All validator messages are local schema instructions. Hide arbitrary
        # enum/input renderings and validation context; the schema supplies choices.
        kind = error['type']
        message = error['msg'] if kind == 'value_error' else {
            'missing': 'Required field is missing',
            'extra_forbidden': 'Unknown field; use only schema fields',
            'literal_error': 'Value must be one of the schema choices',
            'model_type': 'Expected a JSON object matching the schema',
        }.get(kind, 'Value does not satisfy the declared schema constraint')
        details.append({'location': location, 'type': kind, 'message': message[:400]})
    return details


def run_tools(*, propose, payload, query, search, save, restored, remaining, limit=6,
              max_schema_repairs=2):
    ledger = list(restored or [])
    repairs = sum(t.get('error') == 'INVALID_TOOL_PROPOSAL' for t in ledger)
    while len(ledger) < limit and remaining() > 0:
        if ledger and ledger[-1]['status'] in {'FINISHED', 'FAILED', 'NO_PROGRESS'}:
            return ledger
        if ledger and ledger[-1]['status'] == 'RUNNING':
            ledger[-1]['status'] = 'FAILED'
            ledger[-1]['error'] = 'Interrupted tool execution; result not committed'
            save(ledger)
            return ledger
        try:
            feedback = ledger[-1].get('validation_errors', []) if ledger else []
            proposal = propose({**payload(), 'task_ledger': ledger,
                                'validation_feedback': feedback,
                                'schema': ToolAction.model_json_schema()})
            try:
                action = ToolAction.model_validate(proposal)
            except ValidationError as exc:
                repairs += 1
                retryable = repairs <= max_schema_repairs and len(ledger) + 1 < limit and remaining() > 0
                ledger.append({'status': 'INVALID_PROPOSAL' if retryable else 'FAILED',
                               'error': 'INVALID_TOOL_PROPOSAL', 'failure_kind': 'VALIDATION',
                               'attempt': repairs, 'retryable': retryable,
                               'validation_errors': validation_details(exc)})
                save(ledger)
                if retryable:
                    continue
                return ledger
            if action.action == 'finish':
                ledger.append({'status': 'FINISHED', 'purpose': action.purpose})
                save(ledger)
                return ledger
            content = action.model_dump(mode='json')
            fingerprint_data = dict(content)
            fingerprint_data.pop('purpose')
            if action.query:
                fingerprint_data['query'] = {'steps': content['query']['steps'],
                                             'dataset_id': action.query.dataset_id,
                                             'dataset_fingerprint': action.query.dataset_fingerprint}
            key = identity(fingerprint_data)
            if any(t.get('fingerprint') == key for t in ledger):
                ledger.append({'status': 'NO_PROGRESS', 'purpose': action.purpose})
                save(ledger)
                return ledger
            task = {'status': 'RUNNING', 'fingerprint': key, 'action': content}
            ledger.append(task)
            save(ledger)
            task['result'] = query(action.query) if action.action == 'query' else search(action.search)
            task['status'] = 'SUCCEEDED'
            save(ledger)
        except Exception as exc:
            if isinstance(exc, StorageFailure):
                raise
            if not ledger or ledger[-1]['status'] != 'RUNNING':
                ledger.append({'status': 'FAILED'})
            ledger[-1].update(status='FAILED', error=getattr(exc, 'code', type(exc).__name__),
                              failure_kind='EXECUTION' if ledger[-1].get('action') else 'PROPOSAL_SERVICE',
                              retryable=False)
            if isinstance(exc, ValidationError):
                ledger[-1]['validation_errors'] = validation_details(exc)
            save(ledger)
            return ledger
    if not ledger or ledger[-1]['status'] in {'SUCCEEDED', 'INVALID_PROPOSAL', 'REOPENED'}:
        ledger.append({'status': 'FAILED', 'error': 'BUDGET_EXHAUSTED',
                       'failure_kind': 'BUDGET', 'retryable': False})
        save(ledger)
    return ledger
