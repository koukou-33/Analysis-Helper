"""Bounded adaptive tool selection; model output never establishes completion."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from .plans import QueryPlan
from .provenance import identity
from .progress import StorageFailure


class ToolAction(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: Literal['query', 'search', 'finish']
    purpose: str = Field(min_length=1, max_length=2000)
    query: QueryPlan | None = None
    search: str | None = Field(default=None, max_length=1000)


TOOL_SYSTEM = '''你是当前固定数据范围的分析工具调度器。根据问题、已有证据和缺口选择下一步。
仅返回符合schema的JSON：query执行受控QueryPlan，search检索相关规则或案例，finish表示没有新的合法必要行动。
finish不是根因确认或目标完成。不得改变数据集、范围、质量口径；不得虚构字段、阈值和时间窗口。
优先复用已有结果，避免重复查询。没有证据不能宣称数据不足。统计、比较和时序可组合QueryPlan算子。
'''


def run_tools(*, propose, payload, query, search, save, restored, remaining, limit=6):
    ledger = list(restored or [])
    while len(ledger) < limit and remaining() > 0:
        if ledger and ledger[-1]['status'] in {'FINISHED', 'FAILED', 'NO_PROGRESS'}:
            return ledger
        if ledger and ledger[-1]['status'] == 'RUNNING':
            ledger[-1]['status'] = 'FAILED'
            ledger[-1]['error'] = 'Interrupted tool execution; result not committed'
            save(ledger)
            return ledger
        try:
            action = ToolAction.model_validate(propose({**payload(), 'task_ledger': ledger,
                                                       'schema': ToolAction.model_json_schema()}))
            if action.action == 'finish':
                ledger.append({'status': 'FINISHED', 'purpose': action.purpose})
                save(ledger)
                return ledger
            if action.action == 'query' and action.query is None:
                raise ValueError('query plan required')
            if action.action == 'search' and not action.search:
                raise ValueError('search text required')
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
            ledger[-1].update(status='FAILED', error=getattr(exc, 'code', type(exc).__name__))
            save(ledger)
            return ledger
    if not ledger or ledger[-1]['status'] == 'SUCCEEDED':
        ledger.append({'status': 'FAILED', 'error': 'BUDGET_EXHAUSTED'})
        save(ledger)
    return ledger
