import unittest

import test_synthetic_queries as fixtures
from srs_helper.queries import Scope
from srs_helper.sql_executor import execute_sql, validate_sql


class SqlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.SyntheticQueryTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_scoped_cte_and_parameters(self):
        artifacts = execute_sql(self.normal_db,Scope(logic_cell_id=0),
                                "WITH r AS (SELECT * FROM resource WHERE crnti=?) SELECT COUNT(*) AS n FROM r",[1000],self.root/"sql")
        self.assertEqual(artifacts[0]["preview"],[{"n":20}])
        self.assertEqual(artifacts[0]["operator"],"EXPLORATION_SQL")

    def test_external_writes_and_unscoped_tables_rejected(self):
        for sql in ["SELECT * FROM l4_ue_resource_ts", "SELECT * FROM read_csv('secret.csv')",
                    "SELECT * FROM information_schema.tables", "DROP TABLE resource", "ATTACH 'other.db' AS other",
                    "SELECT 1; SELECT 2", "SELECT current_setting('home_directory')",
                    "WITH RECURSIVE a AS (SELECT 1) SELECT * FROM a", "COPY resource TO 'out.csv'",
                    "WITH l4_ue_resource_ts AS (SELECT * FROM l4_ue_resource_ts) SELECT * FROM l4_ue_resource_ts",
                    "WITH resource AS (SELECT * FROM l4_ue_resource_ts) SELECT * FROM resource"]:
            with self.subTest(sql=sql),self.assertRaises(ValueError):
                validate_sql(sql)
