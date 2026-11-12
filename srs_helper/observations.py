"""Conservative file-local radio observations, without inventing wall-clock precision."""

VERSION = 'observations.v2'
AXIS_POLICY = 'PER_SERIES_OBSERVATION_SEQUENCE'


def create_observations(con, scope, metric_identifier):
    """Create ``trend`` from prepared resource values and scoped quality barriers.

    A position is an occurrence in a source file, not a globally unique TTI/frame.
    Only adjacent identical fine positions in the same resource are combined.
    Reversals/resets and unknown ordering break lines instead of sorting them away.
    """
    where, parameters = scope.where()
    con.execute('''CREATE TEMP TABLE observation_rows AS SELECT * FROM (
        SELECT l.record_id,l.source_file,l.file_row_id,l.event_ts,l.logicCellId,
          s.ue_uid,l.crnti,l.phyCellInst,l.resourceId,l.freqRbStart,l.freqRbNum,
          l.uwTtiCnt1,l.frmNum,l.subFrmNum,l.slot,l.csv_structure_complete,
          r.record_id IS NOT NULL AS eligible,
          r.'''+metric_identifier+''' AS metric_value,
          md5(to_json(struct_pack(cell := l.logicCellId, session := s.ue_uid,
            source := l.source_file, phy := l.phyCellInst, resource := l.resourceId,
            rb_start := l.freqRbStart, rb_count := l.freqRbNum))) AS series_id
        FROM l3_cleaned_srs l LEFT JOIN assigned_sessions s USING(record_id)
          LEFT JOIN resource r USING(record_id)
        ) scoped_observations WHERE ('''+where+''') AND event_ts IS NOT NULL AND ue_uid IS NOT NULL''', parameters)
    # Explicit struct equality is null-safe. Unknown fine coordinates stay as rows.
    con.execute('''CREATE TEMP TABLE observation_positions AS
        WITH positions AS (
          SELECT *, coalesce(uwTtiCnt1>=0 OR
            (frmNum>=0 AND subFrmNum>=0 AND slot>=0),false) AS fine_position,
            to_json(struct_pack(observed_at := event_ts, tti := uwTtiCnt1, frame := frmNum,
              subframe := subFrmNum, slot := slot)) AS position_key
          FROM observation_rows
        ), previous AS (
          SELECT *,lag(position_key) OVER w AS previous_position
          FROM positions WINDOW w AS (PARTITION BY series_id ORDER BY file_row_id,record_id)
        ) SELECT *,sum(CASE WHEN fine_position AND position_key=previous_position THEN 0 ELSE 1 END)
          OVER(PARTITION BY series_id ORDER BY file_row_id,record_id ROWS UNBOUNDED PRECEDING) AS position_run
        FROM previous''')
    con.execute('''CREATE TEMP TABLE observation_points AS
        SELECT series_id,position_run,logicCellId,ue_uid,source_file,phyCellInst,resourceId,
          freqRbStart,freqRbNum,event_ts,uwTtiCnt1,frmNum,subFrmNum,slot,
          min(file_row_id) AS first_file_row_id,max(file_row_id) AS last_file_row_id,
          bool_and(fine_position) AS fine_position,
          list(DISTINCT crnti ORDER BY crnti) AS crntis,
          list(record_id ORDER BY file_row_id,record_id) AS record_ids,
          count(*) AS sample_cnt,count(*) FILTER(WHERE eligible) AS eligible_count,
          bool_or(NOT eligible OR NOT csv_structure_complete) AS barrier,
          count(DISTINCT metric_value) FILTER(WHERE eligible)>1 AS conflict,
          CASE WHEN bool_and(eligible AND csv_structure_complete)
            AND count(DISTINCT metric_value)=1 AND count(metric_value)=count(*)
            THEN min(metric_value) END AS value
        FROM observation_positions GROUP BY ALL''')
    con.execute('''CREATE TEMP TABLE observation_ordered AS
        WITH previous AS (
          SELECT *,lag(event_ts) OVER w AS previous_ts,
            lag(uwTtiCnt1) OVER w AS previous_tti,
            lag(frmNum) OVER w AS previous_frame,
            lag(subFrmNum) OVER w AS previous_subframe,lag(slot) OVER w AS previous_slot,
            lag(value) OVER w AS previous_value,
            row_number() OVER w AS observation_index
          FROM observation_points WINDOW w AS (PARTITION BY series_id ORDER BY first_file_row_id)
        ) SELECT *,CASE
          WHEN observation_index=1 THEN 'START'
          WHEN event_ts<previous_ts THEN 'TIME_REVERSAL'
          WHEN uwTtiCnt1<previous_tti THEN 'COUNTER_RESET_OR_OUT_OF_ORDER'
          WHEN frmNum<previous_frame THEN 'FRAME_WRAP_OR_OUT_OF_ORDER'
          WHEN event_ts>previous_ts THEN 'TIMESTAMP_ORDER'
          WHEN uwTtiCnt1>=0 AND previous_tti>=0 AND uwTtiCnt1>previous_tti THEN 'COUNTER_ORDER'
          WHEN frmNum>=0 AND previous_frame>=0 AND (frmNum>previous_frame OR (frmNum=previous_frame AND
            (subFrmNum>previous_subframe OR (subFrmNum=previous_subframe AND slot>previous_slot))))
            THEN 'FRAME_ORDER'
          ELSE 'UNRESOLVED_ORDER' END AS ordering_status
        FROM previous''')
    con.execute('''CREATE TEMP TABLE trend AS
        WITH segmented AS (
          SELECT *,sum(CASE WHEN value IS NULL OR previous_value IS NULL OR
            ordering_status NOT IN ('START','TIMESTAMP_ORDER','COUNTER_ORDER','FRAME_ORDER')
            THEN 1 ELSE 0 END) OVER(PARTITION BY series_id ORDER BY observation_index
              ROWS UNBOUNDED PRECEDING) AS segment
          FROM observation_ordered
        ) SELECT * EXCLUDE(previous_tti,previous_frame,previous_subframe,previous_slot,previous_value,position_run),
          CASE WHEN value IS NOT NULL AND previous_value IS NOT NULL AND
            ordering_status IN ('TIMESTAMP_ORDER','COUNTER_ORDER','FRAME_ORDER')
            THEN value<>previous_value ELSE NULL END AS changed
        FROM segmented''')


LIMITATIONS = [
    '横轴为各来源文件、资源内的观测序列，不代表毫秒间隔；原始时间、TTI、帧/子帧/时隙保留供回查。',
    '不同小区、来源文件和资源分别成线；未证明计数同步，不由序列对齐推断同时发生或切换时刻。',
    '计数/帧倒退及无法判断先后的观测分段；不猜测回绕次数，不跨不明边界连线。',
]
