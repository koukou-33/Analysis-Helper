"""Structured plans only: identifiers and operators never come from raw SQL."""
from .performance import measured
import json
import math
from pathlib import Path
from typing import Literal
from uuid import uuid4

import pyarrow.parquet as pq
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .database import open_dataset
from .queries import Scope


class Expression(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: Literal["eq", "ne", "gt", "ge", "lt", "le", "and", "or", "not", "is_null"]
    field: str | None = None
    value: str | float | int | bool | None = None
    args: list["Expression"] = Field(default_factory=list, max_length=20)


class Metric(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: str
    aggregate: Literal["avg", "count", "min", "max", "stddev_samp", "sum"]
    output: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_]{0,50}$")


class Step(BaseModel):
    model_config = ConfigDict(extra="forbid")
    step_id: str = Field(pattern=r"^s[0-9]{1,3}$")
    purpose: str = ""
    operator: Literal["FILTER", "GROUP_AGG", "CONDITION_COUNT", "COMPARE", "TIME_SLICE", "TEMPORAL_RELATE"]
    source: str = "resource"
    depends_on: list[str] = Field(default_factory=list)
    condition: Expression | None = None
    group_by: list[str] = Field(default_factory=list, max_length=8)
    metrics: list[Metric] = Field(default_factory=list, max_length=20)
    conditions: dict[str, Expression] = Field(default_factory=dict)
    compare_field: str | None = None
    other_source: str | None = None
    object_keys: list[str] = Field(default_factory=list)
    window_seconds: int | None = Field(default=None, ge=0, le=86400)
    start: str | None = None
    end: str | None = None

    @model_validator(mode="after")
    def operator_contract(self):
        if self.operator == 'FILTER' and self.condition is None:
            raise ValueError('FILTER requires condition')
        if self.operator == 'CONDITION_COUNT':
            if not 1 <= len(self.conditions) <= 20:
                raise ValueError('CONDITION_COUNT requires 1-20 entries in conditions, e.g. c1; condition and metrics are not substitutes')
            import re
            if any(not re.fullmatch(r'c[0-9]{1,3}', key) for key in self.conditions):
                raise ValueError('CONDITION_COUNT keys must be c0..c999')
            if self.condition is not None or self.metrics:
                raise ValueError('CONDITION_COUNT uses conditions only; remove condition and metrics')
        if self.operator == 'GROUP_AGG':
            outputs = [m.output for m in self.metrics]
            if not outputs or len(set(outputs)) != len(outputs) or set(outputs) & set(self.group_by+['sample_cnt']):
                raise ValueError('GROUP_AGG needs metrics with unique outputs, excluding group keys and sample_cnt')
        if self.operator == 'COMPARE' and not self.compare_field:
            raise ValueError('COMPARE requires compare_field')
        if self.operator == 'TIME_SLICE':
            from datetime import datetime
            if not self.start or not self.end:
                raise ValueError('TIME_SLICE requires start and end')
            Scope(start=datetime.fromisoformat(self.start),end=datetime.fromisoformat(self.end)).where()
        if self.operator == 'TEMPORAL_RELATE' and (not self.other_source or not self.object_keys or self.window_seconds is None):
            raise ValueError('TEMPORAL_RELATE requires other_source, object_keys and window_seconds')
        return self


class QueryPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan_id: str = Field(default_factory=lambda:str(uuid4()))
    revision: int = Field(default=1, ge=1)
    dataset_id: str
    dataset_fingerprint: str
    question: str
    quality_policy: Literal["INCLUDE_ALL_NUMERIC"] = "INCLUDE_ALL_NUMERIC"
    steps: list[Step] = Field(min_length=1, max_length=8)
    document_refs: list[dict] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    required_fields: list[str] = Field(default_factory=list, max_length=512)

    @model_validator(mode="after")
    def dependencies(self):
        seen = {"resource", "stream"}
        ids = set()
        for step in self.steps:
            if step.step_id in ids:
                raise ValueError("Duplicate step id")
            inputs = [step.source] + ([step.other_source] if step.other_source else [])
            if any(s not in seen for s in inputs) or any(s not in ids for s in step.depends_on):
                raise ValueError("Dependency is missing or forward-referenced")
            for source in inputs:
                if source not in {"resource", "stream"} and source.removesuffix("_rows") not in step.depends_on:
                    raise ValueError("Derived source must be declared in depends_on")
            ids.add(step.step_id)
            seen.add(step.step_id)
            if step.operator == "CONDITION_COUNT":
                seen.add(step.step_id + "_rows")
        return self


def identifier(name, columns):
    if name not in columns:
        raise ValueError(f"Unknown or unavailable field: {name}")
    return '"' + name.replace('"','""') + '"'


def expression_sql(expr, columns, params, depth=0):
    if depth > 12:
        raise ValueError("Expression nesting exceeds 12")
    if expr.op in {"and", "or", "not"}:
        if not expr.args or (expr.op == "not" and len(expr.args) != 1):
            raise ValueError("Invalid logical expression")
        parts = [expression_sql(e,columns,params,depth+1) for e in expr.args]
        return "(NOT " + parts[0] + ")" if expr.op == "not" else "("+(" AND " if expr.op=="and" else " OR ").join(parts)+")"
    # Missing feature fields are UNKNOWN, including missing-column is_null predicates.
    if expr.field not in columns:
        return "NULL::BOOLEAN"
    column = identifier(expr.field, columns)
    if expr.op == "is_null":
        return f"({column} IS NULL)"
    if isinstance(expr.value,float) and not math.isfinite(expr.value):
        raise ValueError("Non-finite expression value")
    params.append(expr.value)
    operators = {"eq":"=", "ne":"<>", "gt":">", "ge":">=", "lt":"<", "le":"<="}
    return f"({column} {operators[expr.op]} ?)"


def default_plan(meta, question):
    return QueryPlan(dataset_id=meta["dataset_id"],dataset_fingerprint=meta["dataset_fingerprint"],question=question,
                     limitations=["默认描述性统计；尚未进行特性根因判断"],steps=[Step(step_id="s1",operator="GROUP_AGG",
                     source="stream",group_by=["logicCellId","ue_uid","resourceId","streamId"],metrics=[
                         Metric(field="instBfSinrGain",aggregate="avg",output="gain_avg"),
                         Metric(field="instBfSinrGain",aggregate="count",output="gain_valid_cnt")])])


def supplemental_columns(con):
    """Read the small header catalog, never DISTINCT over measurement rows."""
    from collections import Counter
    from .pipeline import NUMBERS
    from .schema import bindings
    meta = json.loads(con.execute('SELECT metadata FROM _etl_meta').fetchone()[0])
    if meta['rule_version'] == 'm1.2':
        return []
    names = set()
    for entry in meta['files']:
        if entry['status'] != 'LOADED':
            continue
        header = entry['columns']
        mapped = set(bindings(header, meta['contract'], NUMBERS).values())
        counts = Counter(n.casefold() for n in header)
        reserved = {n.casefold() for n in NUMBERS} | {'uwdata','uwdate','uwtime','record_id','source_file','event_ts','ue_uid','streamid'}
        names.update(n for i,n in enumerate(header) if i not in mapped and counts[n.casefold()]==1
                     and n.casefold() not in reserved and '[' not in n and n and len(n)<=100)
    return sorted(names)


def plan_fields(plan):
    fields = set(plan.required_fields)
    def visit(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {'field','compare_field'} and isinstance(item,str):
                    fields.add(item)
                elif key in {'group_by','object_keys'}:
                    fields.update(item)
                else:
                    visit(item)
        elif isinstance(value,list):
            for item in value:
                visit(item)
    visit(plan.model_dump())
    return fields


@measured("schema")
def scope_columns(con):
    columns = {name: [d[0] for d in con.execute(f'SELECT * EXCLUDE(raw_values) FROM {table} LIMIT 0').description]
               for name,table in [('resource','l4_ue_resource_ts'),('stream','l4_ue_stream_ts')]}
    existing = {n.casefold() for n in columns['resource']}
    columns['resource'].extend(n for n in supplemental_columns(con) if n.casefold() not in existing)
    return columns


@measured("prepare_scope")
def prepare_scope(con,scope,required_fields=()):
    where,params = scope.where()
    con.execute(f"CREATE TEMP TABLE resource AS SELECT * EXCLUDE(raw_values) FROM l4_ue_resource_ts WHERE {where}",params)
    existing = {d[0].casefold() for d in con.execute("SELECT * FROM resource LIMIT 0").description}
    if required_fields is None or required_fields:
        names = [n for n in supplemental_columns(con) if n.casefold() not in existing
                 and (required_fields is None or n.casefold() in {f.casefold() for f in required_fields})]
        if len(names)>512:
            raise ValueError("More than 512 supplemental fields; select an explicit version mapping")
        if names:
            expressions = ','.join('max(value) FILTER(WHERE field_name=?) AS '+identifier(n,names) for n in names)
            placeholders = ','.join('?' for _ in names)
            con.execute('CREATE TEMP TABLE extra_values AS SELECT record_id,'+expressions+
                        ' FROM (SELECT e.record_id,e.field_name,e.value FROM l2_extra_numeric e '
                        'SEMI JOIN resource r USING(record_id) WHERE field_name IN ('+placeholders+')) GROUP BY record_id',names+names)
            con.execute('CREATE OR REPLACE TEMP TABLE resource AS SELECT r.*,e.* EXCLUDE(record_id) FROM resource r LEFT JOIN extra_values e USING(record_id)')
    con.execute("CREATE TEMP TABLE stream AS SELECT s.* EXCLUDE(raw_values) FROM l4_ue_stream_ts s JOIN resource r USING(record_id)")


@measured("query_plan")
def execute_plan(directory, scope, plan, output_dir):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True,exist_ok=True)
    results = []
    with open_dataset(directory) as (con,meta):
        if (plan.dataset_id,plan.dataset_fingerprint) != (meta["dataset_id"],meta["dataset_fingerprint"]):
            raise ValueError("Plan belongs to another dataset version")
        prepare_scope(con,scope,plan_fields(plan))
        for step in plan.steps:
            source = step.source  # validated relation IDs only
            columns = {d[0] for d in con.execute(f'SELECT * FROM "{source}" LIMIT 0').description}
            parameters = []
            group = [identifier(c,columns) for c in step.group_by]
            prefix = ",".join(group)
            if step.operator == "FILTER":
                if step.condition is None:
                    raise ValueError("FILTER requires a condition")
                sql = f'SELECT * FROM "{source}" WHERE '+expression_sql(step.condition,columns,parameters)
            elif step.operator == "TIME_SLICE":
                if "event_ts" not in columns or not step.start or not step.end:
                    raise ValueError("TIME_SLICE requires time field and both boundaries")
                from datetime import datetime
                start,end = datetime.fromisoformat(step.start),datetime.fromisoformat(step.end)
                Scope(start=start,end=end).where()
                parameters = [start,end]
                sql = f'SELECT * FROM "{source}" WHERE event_ts>=? AND event_ts<?'
            elif step.operator == "GROUP_AGG":
                if not step.metrics:
                    raise ValueError("GROUP_AGG requires metrics")
                if len({m.output for m in step.metrics}) != len(step.metrics) or any(m.output in step.group_by+['sample_cnt'] for m in step.metrics):
                    raise ValueError("Duplicate output name")
                measures = [f'{m.aggregate}({identifier(m.field,columns)}) AS "{m.output}"' for m in step.metrics]
                sql = 'SELECT '+ (prefix+',' if prefix else '')+'count(*) AS sample_cnt,'+','.join(measures)+f' FROM "{source}"'
                if "ue_uid" in step.group_by:
                    sql += " WHERE ue_uid IS NOT NULL"
                if prefix:
                    sql += ' GROUP BY '+prefix
            elif step.operator == "CONDITION_COUNT":
                if not step.conditions or len(step.conditions)>20:
                    raise ValueError("CONDITION_COUNT needs 1–20 conditions")
                rendered = []
                for name, expr in step.conditions.items():
                    import re
                    if not re.fullmatch(r"c[0-9]{1,3}",name) or name in columns:
                        raise ValueError("Condition IDs must be c0..c999 and not collide")
                    rendered.append(expression_sql(expr,columns,parameters)+f' AS "{name}"')
                rows_name = step.step_id+'_rows'
                con.execute(f'CREATE TEMP TABLE "{rows_name}" AS SELECT *, '+','.join(rendered)+f' FROM "{source}"',parameters)
                parameters = []
                measures = []
                for name in step.conditions:
                    measures.extend([f'count(*) FILTER(WHERE "{name}" IS {state}) AS "{name}_{label}"' for state,label in [('TRUE','true'),('FALSE','false'),('NULL','unknown')]])
                sql = 'SELECT '+(prefix+',' if prefix else '')+'count(*) AS sample_cnt,'+','.join(measures)+f' FROM "{rows_name}"'
                if prefix:
                    sql += ' GROUP BY '+prefix
                results.append(export_relation(con,rows_name,output_dir,plan,scope,"condition_evaluated_rows"))
            elif step.operator == "COMPARE":
                field = identifier(step.compare_field,columns)
                window = 'PARTITION BY '+prefix if prefix else ''
                sql = f'SELECT *, CASE WHEN count({field}) OVER ({window})>=2 THEN max({field}) OVER ({window})-min({field}) OVER ({window}) END AS comparison_diff FROM "{source}"'
            else:
                if not step.other_source or not step.object_keys or step.window_seconds is None:
                    raise ValueError("TEMPORAL_RELATE requires object keys, relation and explicit time window")
                other_cols = {d[0] for d in con.execute(f'SELECT * FROM "{step.other_source}" LIMIT 0').description}
                identifier("event_ts",columns); identifier("event_ts",other_cols)
                keys = [f'a.{identifier(k,columns)}=b.{identifier(k,other_cols)}' for k in step.object_keys]
                condition = ' AND '.join(keys)+" AND b.event_ts>=a.event_ts AND b.event_ts<=a.event_ts+? * INTERVAL 1 SECOND"
                parameters = [step.window_seconds]
                missing_keys = ' OR '.join(f'a.{identifier(k,columns)} IS NULL' for k in step.object_keys)
                unknown_time = ' AND '.join(keys)+" AND b.event_ts IS NULL"
                sql = f'SELECT a.*,CASE WHEN a.event_ts IS NULL OR {missing_keys} THEN NULL WHEN EXISTS(SELECT 1 FROM "{step.other_source}" b WHERE {condition}) THEN TRUE WHEN EXISTS(SELECT 1 FROM "{step.other_source}" b WHERE {unknown_time}) THEN NULL ELSE FALSE END AS related FROM "{source}" a'
            con.execute("EXPLAIN "+sql,parameters)
            con.execute(f'CREATE TEMP TABLE "{step.step_id}" AS '+sql,parameters)
            result = export_relation(con,step.step_id,output_dir,plan,scope,step.operator)
            result.update(sql=sql,parameters=parameters,purpose=step.purpose)
            results.append(result)
    return results


def export_relation(con, name, directory, plan, scope, operator):
    import hashlib
    aid = str(uuid4())
    path = directory / (aid+".parquet")
    cursor = con.execute(f'SELECT * FROM "{name}"')
    reader = cursor.fetch_record_batch(2048)
    count,preview = 0,[]
    with pq.ParquetWriter(path,reader.schema) as writer:
        for batch in reader:
            count += batch.num_rows
            if count > 1_000_000:
                raise ValueError("Result exceeds one million rows; narrow the scope")
            if len(preview)<100:
                preview.extend(batch.slice(0,100-len(preview)).to_pylist())
            writer.write_batch(batch)
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream,"sha256").hexdigest()
    return {"artifact_id":aid,"path":str(path.resolve()),"sha256":digest,
            "dataset_id":plan.dataset_id,"dataset_fingerprint":plan.dataset_fingerprint,"plan_id":plan.plan_id,
            "step_id":name,"operator":operator,"row_count":count,"preview":preview,"preview_truncated":count>100,
            "scope":scope.__dict__,"quality_policy":plan.quality_policy}
