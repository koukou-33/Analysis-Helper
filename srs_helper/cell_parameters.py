"""Versioned cell-shared parameter inference; raw measurements are never overwritten."""
from pathlib import Path
from .field_metadata import compile_mapping


def snapshot_policy(contract):
    path=Path(contract.get('field_semantics_file') or Path(__file__).resolve().parent.parent/'knowledge_assets/field_mappings/nr_srs_field_mapping.md')
    catalog=compile_mapping(path)
    return {'version':'cell-continuity.v1','mapping_version':catalog['version'],
        'fields':sorted(n for n,e in catalog['fields'].items() if e.get('data_level')=='小区级' and not e.get('data_level_conflict')),
        'group_by':['logicCellId'],'max_gap_seconds':None,'method':'BRACKET_EQUAL',
        'source_population':'BUSINESS_VALID_WITHOUT_PARSE_ERRORS','scope':'IMMUTABLE_DATASET'}


def materialize(con, policy):
    con.execute('''CREATE TABLE cell_parameter_fills (
        record_id VARCHAR, field_name VARCHAR, inferred_value DOUBLE,
        logicCellId BIGINT, event_ts TIMESTAMP,
        previous_record_id VARCHAR, next_record_id VARCHAR,
        previous_time TIMESTAMP, next_time TIMESTAMP,
        previous_support BIGINT,next_support BIGINT,method VARCHAR)''')
    core={c[0] for c in con.execute('DESCRIBE l4_ue_resource_ts').fetchall()}
    for field in policy['fields']:
        if field not in core and not con.execute('SELECT count(*) FROM field_catalog WHERE raw_name=?',[field]).fetchone()[0]:
            continue
        quoted='"'+field.replace('"','""')+'"'
        if field in core:
            values=f'''SELECT record_id,try_cast({quoted} AS DOUBLE) AS value,
                {quoted} IS NOT NULL AND (try_cast({quoted} AS DOUBLE) IS NULL OR NOT isfinite(try_cast({quoted} AS DOUBLE))) AS bad
                FROM l4_ue_resource_ts'''
            params=[]
        else:
            values="SELECT record_id,value,parse_state<>'VALID' AS bad FROM l2_extra_numeric WHERE field_name=?"
            params=[field]
        # Only one selected field is scanned and materialized, never pivot the entire long table.
        extra_bad=' OR e.value NOT IN (0,1)' if field=='srsSpfSwitch' else ''
        con.execute(f'''CREATE OR REPLACE TEMP TABLE cell_values AS
            SELECT r.record_id,r.logicCellId,r.event_ts,e.value,
                coalesce(e.bad{extra_bad},false) AS bad
            FROM l4_ue_resource_ts r LEFT JOIN ({values}) e USING(record_id)
            WHERE r.logicCellId IS NOT NULL AND r.event_ts IS NOT NULL AND NOT r.has_parse_error''',params)
        con.execute('''CREATE OR REPLACE TEMP TABLE cell_times AS
            SELECT logicCellId,event_ts,count(DISTINCT value) AS distinct_values,
                count(value) AS support,min(value) AS value,min(record_id) FILTER(WHERE value IS NOT NULL) AS source_id,
                bool_or(bad) AS bad
            FROM cell_values GROUP BY logicCellId,event_ts''')
        con.execute('''CREATE OR REPLACE TEMP TABLE cell_bounds AS
            WITH anchors AS (
              SELECT *, CASE WHEN distinct_values>0 OR bad THEN
                struct_pack(value:=value,source_id:=source_id,observed_at:=event_ts,support:=support,
                            valid:=distinct_values=1 AND NOT bad) END AS anchor
              FROM cell_times
            )
            SELECT logicCellId,event_ts,
                last_value(anchor IGNORE NULLS) OVER (
                    PARTITION BY logicCellId ORDER BY event_ts ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS prev,
                first_value(anchor IGNORE NULLS) OVER (
                    PARTITION BY logicCellId ORDER BY event_ts ROWS BETWEEN CURRENT ROW AND UNBOUNDED FOLLOWING) AS next
            FROM anchors''')
        con.execute('''INSERT INTO cell_parameter_fills
            SELECT v.record_id,?,b.prev.value,v.logicCellId,v.event_ts,
                b.prev.source_id,b.next.source_id,b.prev.observed_at,b.next.observed_at,
                b.prev.support,b.next.support,'BRACKET_EQUAL'
            FROM cell_values v JOIN cell_bounds b USING(logicCellId,event_ts)
            WHERE v.value IS NULL AND NOT v.bad AND b.prev.valid AND b.next.valid
              AND b.prev.value=b.next.value''',[field])
    con.execute('CREATE UNIQUE INDEX cell_fill_key ON cell_parameter_fills(record_id,field_name)')
    for table in ('cell_values','cell_times','cell_bounds'):
        con.execute('DROP TABLE IF EXISTS '+table)


def apply_to_scope(con, fields):
    if not con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name='cell_parameter_fills'").fetchone()[0]:
        return
    names={c[0] for c in con.execute('DESCRIBE resource').fetchall()}
    for field in sorted(set(fields or ())):
        if field not in names:
            continue
        q=lambda v:'"'+v.replace('"','""')+'"'
        raw='__cell_raw_'+field
        origin='__cell_fill_'+field
        if raw in names or origin in names:
            raise ValueError('Reserved cell provenance column collision')
        # An inferred field is selected only when needed by the plan, with lineage alongside it.
        con.execute(f'''CREATE OR REPLACE TEMP TABLE resource AS
            SELECT r.* REPLACE(coalesce(r.{q(field)},f.inferred_value) AS {q(field)}),
                r.{q(field)} AS {q(raw)},
                CASE WHEN f.record_id IS NOT NULL THEN struct_pack(
                    method:=f.method,previous_record_id:=f.previous_record_id,next_record_id:=f.next_record_id,
                    previous_time:=f.previous_time,next_time:=f.next_time,
                    previous_support:=f.previous_support,next_support:=f.next_support) END AS {q(origin)}
            FROM resource r LEFT JOIN cell_parameter_fills f ON r.record_id=f.record_id AND f.field_name=?''',[field])
        names.update((raw,origin))
