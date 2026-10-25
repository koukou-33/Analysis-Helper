import json,time,duckdb
from srs_helper.plans import prepare_scope,scope_columns
from srs_helper.queries import Scope
from pathlib import Path
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
for label,fields in [('baseline',()),('one_field',{'field7'})]:
 t=time.perf_counter();prepare_scope(con,Scope(logic_cell_id=0),fields);results[label+'_seconds']=time.perf_counter()-t
 if fields:
  assert con.execute('SELECT count(*),sum(field7) FROM resource').fetchone()==(38637,float(sum(i+7 for i in range(0,77273,2))))
 con.execute('DROP TABLE stream');con.execute('DROP TABLE resource')
con.close()
(root/'results.json').write_text(json.dumps(results,indent=2));print(results)
