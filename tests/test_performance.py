import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from srs_helper import performance


class PerformanceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.log = Path(temp.name)/'performance.txt'
        mock = patch.object(performance,'LOG_PATH',self.log)
        mock.start()
        self.addCleanup(mock.stop)

    def records(self):
        return [json.loads(line) for line in self.log.read_text(encoding='utf-8').splitlines()]

    def test_nested_trace_memory_and_sensitive_data_not_logged(self):
        @performance.measured('child')
        def child(secret):
            return secret
        @performance.measured('parent')
        def parent(secret):
            return child(secret)
        self.assertEqual(parent('PRIVATE_TOKEN'),'PRIVATE_TOKEN')
        a,b = self.records()
        self.assertEqual(a['trace_id'],b['trace_id'])
        self.assertNotEqual(a['span_id'],b['span_id'])
        self.assertGreaterEqual(b['elapsed_seconds'],a['elapsed_seconds'])
        self.assertEqual(b['memory_scope'],'whole_process')
        self.assertNotIn('PRIVATE_TOKEN',self.log.read_text())
        rss = performance.rss_bytes()
        if rss is not None:
            self.assertGreater(rss,0)

    def test_failure_preserved_and_concurrent_append(self):
        @performance.measured('failure')
        def fail():
            raise ValueError('SECRET_QUERY')
        with self.assertRaisesRegex(ValueError,'SECRET_QUERY'):
            fail()
        @performance.measured('parallel')
        def work(i):
            return i
        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertEqual(list(pool.map(work,range(12))),list(range(12)))
        records = self.records()
        self.assertEqual(len(records),13)
        self.assertEqual(records[0]['error_type'],'ValueError')
        self.assertEqual(records[0]['status'],'ERROR')
        self.assertEqual(len({r['trace_id'] for r in records}),13)
        self.assertNotIn('SECRET_QUERY',self.log.read_text())

    def test_unwritable_log_and_metadata_failure_do_not_break_result(self):
        @performance.measured('load_dataset')
        def work():
            return 'malformed metrics only'
        self.assertEqual(work(),'malformed metrics only')
        self.assertTrue(self.records()[0]['metrics_incomplete'])
        with patch.object(performance.portalocker,'Lock',side_effect=OSError('private path')):
            with self.assertWarns(RuntimeWarning):
                self.assertEqual(work(),'malformed metrics only')

    def test_load_counts_exclude_failed_files(self):
        directory = self.log.parent
        (directory/'analysis.duckdb').write_bytes(b'1234')
        meta = {'dataset_id':'test','status':'PARTIAL_READY','files':[
            {'status':'LOADED','records':3,'size':12,'columns':['a','b'],
             'supplemental_numeric_rows':6,'supplemental_field_count':2},
            {'status':'FAILED','records':99,'size':5,'supplemental_numeric_rows':99}]}
        @performance.measured('load_dataset')
        def load():
            return directory,meta
        load()
        row = self.records()[0]
        self.assertEqual((row['input_rows'],row['supplemental_numeric_rows'],row['database_bytes']),(3,6,4))
        self.assertEqual(row['dataset_status'],'PARTIAL_READY')
