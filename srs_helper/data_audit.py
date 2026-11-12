"""Deterministic, full-artifact coverage gate before evidence is frozen."""
from datetime import datetime
from pathlib import Path
from threading import Timer
from time import monotonic

import duckdb

from .database import open_dataset
from .evidence import verify_artifact
from .provenance import active_cohort_ref


def _canonical_scope(scope):
    def normalize(value):
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, dict):
            result = {k: normalize(v) for k, v in value.items() if v not in (None, (), [])}
            for key in ('start', 'end'):
                if isinstance(result.get(key), str):
                    result[key] = datetime.fromisoformat(result[key]).isoformat()
            return result
        if isinstance(value, (list, tuple)):
            return [normalize(v) for v in value]
        return value
    return normalize(scope)


def _requested_cells(scope):
    cells = set(getattr(scope, 'logic_cell_ids', ()) or ())
    if scope.logic_cell_id is not None:
        cells.add(scope.logic_cell_id)
    for subject in getattr(scope, 'subjects', ()) or ():
        if isinstance(subject, dict) and subject.get('logic_cell_id') is not None:
            cells.add(subject['logic_cell_id'])
    return cells


def _trend_counts(artifact, seconds=30):
    """Scan the complete verified Parquet, never group_statistics/preview caps."""
    verify_artifact(artifact)
    with duckdb.connect(config={'memory_limit': '256MB', 'threads': '2'}) as con:
        timer = Timer(max(.001, seconds), con.interrupt)
        timer.daemon = True
        timer.start()
        try:
            con.read_parquet(str(Path(artifact['path']))).create_view('audit_points')
            columns = {r[0] for r in con.execute('DESCRIBE audit_points').fetchall()}
            if not {'logicCellId', 'value', 'record_ids'} <= columns:
                raise ValueError('Trend artifact lacks coverage columns')
            represented = ('sum(eligible_count)' if 'eligible_count' in columns
                           else 'sum(array_length(record_ids))')
            rows = con.execute(f'''SELECT logicCellId,count(*) AS points,count(value) AS valid_points,
                {represented} AS represented_rows FROM audit_points GROUP BY logicCellId''').fetchall()
            return {r[0]: dict(zip(['points', 'valid_points', 'represented_rows'], r[1:])) for r in rows}
        finally:
            timer.cancel()


def audit_data_coverage(directory, scope, request, artifacts, seconds=30):
    """Check requested cells, precision and lineage counts; suggest bounded repairs.

    Status NEEDS_DATA means a missing/stale trend can be regenerated, LIMITED means
    the fixed data cannot support all requested observations, PASS means this gate
    found no coverage defect (not a declaration of overall diagnostic completion).
    """
    result = {'version': 'data-audit.v1', 'status': 'NOT_REQUIRED', 'per_cell': [],
              'tasks': [], 'limitations': [], 'evidence_ids': []}
    if not request:
        return result
    deadline = monotonic() + seconds
    metric_fields = [m['canonical_field'] if isinstance(m, dict) else m
                     for m in request.get('metrics', [])]
    trend = bool(request.get('trend') and metric_fields)
    with open_dataset(directory, seconds=seconds) as (con, meta):
        if (request.get('dataset_id'), request.get('dataset_fingerprint')) != (meta['dataset_id'], meta['dataset_fingerprint']):
            raise ValueError('Audit request belongs to another dataset')
        con.execute('''CREATE TEMP VIEW audit_source AS
            SELECT l.*,a.ue_uid,coalesce(c.business_eligible,false) AS business_eligible,
                   c.flow_status
            FROM l2_standardized l LEFT JOIN l3_cleaned_srs c USING(record_id)
            LEFT JOIN assigned_sessions a USING(record_id)''')
        where, params = scope.where()
        con.execute('CREATE TEMP TABLE audit_selected AS SELECT * FROM audit_source WHERE '+where, params)
        rows = con.execute('''SELECT logicCellId,count(*) AS source_rows,
            count(*) FILTER(WHERE flow_status='VALID_FLOW') AS flow_valid_rows,
            count(*) FILTER(WHERE business_eligible) AS eligible_rows,
            count(*) FILTER(WHERE event_ts IS NULL) AS time_unavailable_rows,
            count(*) FILTER(WHERE business_eligible AND event_ts IS NOT NULL AND ue_uid IS NOT NULL) AS located_rows,
            count(*) FILTER(WHERE flow_status IS NULL) AS quarantined_rows
            FROM audit_selected GROUP BY logicCellId ORDER BY logicCellId''').fetchall()
        names = ['logicCellId', 'source_rows', 'flow_valid_rows', 'eligible_rows',
                 'time_unavailable_rows', 'located_rows', 'quarantined_rows']
        by_cell = {row[0]: {**dict(zip(names, row)), 'metrics': {}} for row in rows}
        fine_cells = {r[0] for r in con.execute('''SELECT DISTINCT logicCellId FROM (
            SELECT logicCellId,ue_uid,event_ts,count(DISTINCT (uwTtiCnt1,frmNum,subFrmNum,slot)) AS positions
            FROM audit_selected WHERE business_eligible AND ue_uid IS NOT NULL
            GROUP BY logicCellId,ue_uid,event_ts HAVING positions>1)''').fetchall()} if trend else set()
    for cell in _requested_cells(scope):
        by_cell.setdefault(cell, {**dict.fromkeys(names[1:], 0), 'logicCellId': cell, 'metrics': {}})
    if not by_cell:
        result['limitations'].append({'code': 'NO_SOURCE_ROWS', 'logicCellId': None,
                                     'message': '本次范围与父证据群体内没有加载记录。'})
    for cell, item in by_cell.items():
        if not item['source_rows']:
            result['limitations'].append({'code': 'NO_SOURCE_ROWS', 'logicCellId': cell,
                                         'message': '指定小区在本次范围内没有加载记录。'})
        elif not item['eligible_rows']:
            result['limitations'].append({'code': 'NO_ELIGIBLE_ROWS', 'logicCellId': cell,
                                         'message': '该小区有原始记录，但没有满足当前业务质量口径的记录。'})
        if trend and item['time_unavailable_rows']:
            result['limitations'].append({'code': 'TIME_UNAVAILABLE', 'logicCellId': cell,
                                         'rows': item['time_unavailable_rows'],
                                         'message': '部分记录无法解析日期时间；检查输入契约后重新加载。'})
        if trend and item['eligible_rows'] > item['located_rows']:
            result['limitations'].append({'code': 'IDENTITY_OR_TIME_UNAVAILABLE', 'logicCellId': cell,
                                         'rows': item['eligible_rows']-item['located_rows'],
                                         'message': '部分业务有效记录缺少可归属的身份或时间，无法形成时序点。'})
    for field in metric_fields if trend else []:
        matching = [a for a in artifacts if a.get('operator') == 'METRIC_TIME_SERIES' and a.get('field') == field]
        artifact = next((a for a in reversed(matching)
                         if a.get('dataset_id') == request['dataset_id']
                         and a.get('dataset_fingerprint') == request['dataset_fingerprint']
                         and a.get('input_cohort_id') == active_cohort_ref.get()
                         and _canonical_scope(a.get('scope', {})) == _canonical_scope(scope.__dict__)), None)
        evidence_counts = _trend_counts(artifact, max(.001, deadline-monotonic())) if artifact else {}
        if artifact:
            result['evidence_ids'].append(artifact['artifact_id'])
        repair_cells = []
        reasons = set()
        for cell, item in by_cell.items():
            counts = evidence_counts.get(cell, {'points': 0, 'valid_points': 0, 'represented_rows': 0})
            item['metrics'][field] = counts
            if not item['located_rows']:
                continue
            if artifact is None or counts['represented_rows'] < item['located_rows']:
                repair_cells.append(cell)
                reasons.add('MISSING_TREND_COVERAGE')
            elif cell in fine_cells and artifact.get('observation_version') != 'observations.v2':
                repair_cells.append(cell)
                reasons.add('COARSE_TIME_COLLAPSE')
            elif not counts['valid_points']:
                result['limitations'].append({'code': 'NO_VALID_METRIC_POINTS', 'logicCellId': cell,
                                             'field': field, 'message': '已覆盖原始记录，但指标缺失、冲突或质量阻断导致无有效点。'})
        if repair_cells:
            result['tasks'].append({'kind': 'METRIC_TIME_SERIES', 'field': field,
                                    'logic_cell_ids': repair_cells, 'reasons': sorted(reasons),
                                    'purpose': '按完整请求范围补齐该指标的精细时序证据；不得仅修正文案。'})
    result['per_cell'] = sorted(by_cell.values(), key=lambda r: (r['logicCellId'] is None, r['logicCellId'] or 0))
    result['status'] = 'NEEDS_DATA' if result['tasks'] else 'LIMITED' if result['limitations'] else 'PASS'
    result['meaning'] = '本审核仅证明范围与观测覆盖，不能替代问题目标闭环或根因验证。'
    return result
