import json,time,duckdb
from srs_helper.plans import prepare_scope,scope_columns
from srs_helper.queries import Scope
from pathlib import Path
from threading import Timer
from srs_helper.performance import rss_bytes
root=Path('workspace_storage/scope_benchmark');root.mkdir(exist_ok=True)
con=duckdb.connect(str(root/'bench.duckdb'),config={'memory_limit':'512MB','threads':'2'})
con.execute('CREATE OR REPLACE TABLE l4_ue_resource_ts AS SELECT i::VARCHAR record_id, i%2 logicCellId, NULL::VARCHAR raw_values FROM range(77273) t(i)')
con.execute('CREATE OR REPLACE TABLE l4_ue_stream_ts AS SELECT *, 0 streamId FROM l4_ue_resource_ts')
con.execute('CREATE OR REPLACE TABLE l2_extra_numeric AS SELECT i::VARCHAR record_id,\'field\'||j field_name,(i+j)::DOUBLE AS "value" FROM range(77273) t(i) CROSS JOIN range(440) u(j)')
contract=json.loads(Path('config/input_contract.json').read_text())
header=contract['required_identity_columns']+['field'+str(i) for i in range(440)]
meta={'rule_version':'m1.3','contract':contract,'files':[{'status':'LOADED','columns':header}]}
con.execute('CREATE OR REPLACE TABLE _etl_meta AS SELECT ? metadata',[json.dumps(meta)])
results={'rows':con.execute('SELECT count(*) FROM l2_extra_numeric').fetchone()[0],'fields':440,'memory_limit':'512MB','threads':2}
t=time.perf_counter();scope_columns(con);results['schema_seconds']=time.perf_counter()-t
for label,fields,scope in [('baseline',(),Scope(logic_cell_id=0)),('one_field',{'field7'},Scope(logic_cell_id=0)),('ten_fields',{f'field{i}' for i in range(10)},Scope(logic_cell_id=0)),('wide_ten_fields',{f'field{i}' for i in range(10)},Scope()),('repeat_one_field',{'field7'},Scope(logic_cell_id=0))]:
 timer=Timer(30,con.interrupt);timer.start()
 t=time.perf_counter()
 try:
  prepare_scope(con,scope,fields)
 finally:
  timer.cancel()
 results[label+'_seconds']=time.perf_counter()-t
 results[label+'_rss_bytes']=rss_bytes()
 if fields:
  selected=range(0,77273,2) if scope.logic_cell_id is not None else range(77273)
  assert con.execute('SELECT count(*),sum(field7) FROM resource').fetchone()==(len(selected),float(sum(i+7 for i in selected)))
 con.execute('DROP TABLE stream');con.execute('DROP TABLE resource');con.execute('DROP TABLE IF EXISTS extra_values')
con.close()
(root/'results.json').write_text(json.dumps(results,indent=2));print(results)
