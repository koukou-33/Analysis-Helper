"""Time coverage for one explicitly selected object, retaining segment boundaries."""
from contextlib import contextmanager
from threading import Timer
import duckdb


@contextmanager
def chart_connection(artifact):
    con=duckdb.connect(config={'memory_limit':'256MB','threads':'2'})
    timer=Timer(15,con.interrupt); timer.daemon=True
    try:
        timer.start()
        con.read_parquet(artifact['path']).create_view('points')
        yield con
    finally:
        timer.cancel(); con.close()


def chart_objects(artifact, search=''):
    with chart_connection(artifact) as con:
        columns={r[0] for r in con.execute('DESCRIBE points').fetchall()}
        cell='cast(logicCellId AS VARCHAR)' if 'logicCellId' in columns else "''"
        return con.execute(f'''SELECT DISTINCT {cell} AS cell,ue_uid FROM points
            WHERE contains(cast(ue_uid AS VARCHAR),?) ORDER BY cell,ue_uid LIMIT 501''',[search]).fetchall()


def time_coverage(artifact, cell, ue_uid, limit=2000):
    """Pick per-segment time buckets' endpoints and extrema, never join across gaps."""
    with chart_connection(artifact) as con:
        columns={r[0] for r in con.execute('DESCRIBE points').fetchall()}
        where='ue_uid=?'; params=[ue_uid]
        if 'logicCellId' in columns:
            where+=' AND cast(logicCellId AS VARCHAR)=?'; params.append(cell)
        order='event_ts'+(',record_id' if 'record_id' in columns else '')
        segment='segment' if 'segment' in columns else 'segment_index' if 'segment_index' in columns else '0'
        con.execute(f'CREATE TEMP TABLE chosen AS SELECT *, {segment} AS preview_segment FROM points WHERE {where}',params)
        total=con.execute('SELECT count(*) FROM chosen').fetchone()[0]
        if total<=limit:
            return con.execute('SELECT * FROM chosen ORDER BY '+order).fetchdf(),{'total_points':total,'shown_points':total,'omitted_segments':0,'method':'FULL_OBJECT'}
        segments=con.execute('SELECT preview_segment,count(*) FROM chosen GROUP BY 1 ORDER BY 1').fetchall()
        # Up to four representatives per bucket. Too many segments are explicitly omitted.
        max_segments=max(1,limit//4)
        selected=segments[:max_segments]
        per_segment=max(1,limit//(4*len(selected)))
        con.execute('CREATE TEMP TABLE selected_segments(id BIGINT)')
        con.executemany('INSERT INTO selected_segments VALUES (?)',[(r[0],) for r in selected])
        value='value' if 'value' in columns else 'NULL'
        sql=f'''WITH ranked AS (
          SELECT *, row_number() OVER(PARTITION BY preview_segment ORDER BY {order}) AS point_rank,
          count(*) OVER(PARTITION BY preview_segment) AS segment_size
          FROM chosen WHERE preview_segment IN (SELECT id FROM selected_segments)
        ), bucketed AS (
          SELECT *,floor((point_rank-1)*{per_segment}/segment_size) AS bucket FROM ranked
        ), picks AS (
          SELECT *,row_number() OVER(PARTITION BY preview_segment,bucket ORDER BY point_rank) AS first_pick,
          row_number() OVER(PARTITION BY preview_segment,bucket ORDER BY point_rank DESC) AS last_pick,
          row_number() OVER(PARTITION BY preview_segment,bucket ORDER BY {value} ASC NULLS LAST,point_rank) AS min_pick,
          row_number() OVER(PARTITION BY preview_segment,bucket ORDER BY {value} DESC NULLS LAST,point_rank) AS max_pick
          FROM bucketed
        ) SELECT * FROM picks WHERE first_pick=1 OR last_pick=1 OR min_pick=1 OR max_pick=1 ORDER BY {order}'''
        frame=con.execute(sql).fetchdf()
        return frame,{'total_points':total,'shown_points':len(frame),'omitted_segments':len(segments)-len(selected),
                      'method':'SEGMENT_TIME_BUCKET_ENDPOINTS_EXTREMA','buckets_per_segment':per_segment}
