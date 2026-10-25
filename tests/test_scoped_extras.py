import json
import unittest
from pathlib import Path

import duckdb
from srs_helper.plans import prepare_scope, scope_columns, QueryPlan, Step, Expression, plan_fields
from srs_helper.queries import Scope


class ScopedExtraTests(unittest.TestCase):
    def test_header_only_baseline_and_scoped_projection(self):
        con = duckdb.connect()
        self.addCleanup(con.close)
        con.execute("CREATE TABLE l4_ue_resource_ts AS SELECT i::VARCHAR record_id,i%2 logicCellId,NULL::VARCHAR raw_values FROM range(20) t(i)")
        con.execute('CREATE TABLE l4_ue_stream_ts AS SELECT * FROM l4_ue_resource_ts')
        contract = json.loads(Path('config/input_contract.json').read_text())
        header = contract['required_identity_columns'] + [f'field{i}' for i in range(440)]
        meta = {'rule_version':'m1.3','contract':contract,'files':[{'status':'LOADED','columns':header}]}
        con.execute('CREATE TABLE _etl_meta AS SELECT ? metadata',[json.dumps(meta)])
        # No numeric table exists: schema discovery and baseline must not touch it.
        self.assertIn('field439',scope_columns(con)['resource'])
        prepare_scope(con,Scope(logic_cell_id=0))
        self.assertEqual(con.execute('SELECT count(*) FROM resource').fetchone()[0],10)
        con.execute('DROP TABLE resource');con.execute('DROP TABLE stream')
        con.execute('CREATE TABLE l2_extra_numeric AS SELECT i::VARCHAR record_id,\'field\'||j field_name,(i+j)::DOUBLE AS "value" FROM range(20) t(i) CROSS JOIN range(440) u(j)')
        con.execute("UPDATE l2_extra_numeric SET value=NULL WHERE record_id='0' AND field_name='field7'")
        prepare_scope(con,Scope(logic_cell_id=0),{'field7'})
        self.assertEqual(con.execute('SELECT count(*),count(field7),sum(field7) FROM resource').fetchone(),(10,9,153.0))
        self.assertEqual(con.execute('SELECT count(*) FROM extra_values').fetchone()[0],10)
        self.assertNotIn('field8',[d[0] for d in con.execute('SELECT * FROM resource LIMIT 0').description])

    def test_nested_fields_and_flag_requirements(self):
        plan = QueryPlan(dataset_id='a',dataset_fingerprint='b',question='test',required_fields=['flag'],steps=[
            Step(step_id='s1',operator='FILTER',condition=Expression(op='and',args=[Expression(op='gt',field='field7',value=0)]))])
        self.assertEqual(plan_fields(plan),{'flag','field7'})
