import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from srs_helper.knowledge import KnowledgeIndex, parse_document


class KnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.case = self.root / "assets/cases/C1/C1.md"
        self.case.parent.mkdir(parents=True)
        self.body = "---\ncase_id: C1\ntitle: 流增益案例\nreview_status: REVIEWED\n---\n增益反转需要进一步排查。"
        self.case.write_text(self.body,encoding="utf-8")

    def test_no_images_assertions_sync_versions_and_removal(self):
        index = KnowledgeIndex(self.root/"assets",self.root/"index")
        self.assertEqual(index.sync()[0]["status"],"UPDATED")
        self.assertEqual(index.sync()[0]["status"],"UNCHANGED")
        hits, mode = index.search("流增益反转")
        self.assertEqual(len(hits),1)
        old = hits[0]["version"]
        self.case.write_text(self.body+" 修改",encoding="utf-8")
        index.sync()
        self.assertNotEqual(index.documents()[0]["version"],old)
        self.assertTrue((self.root/"index/versions"/old/"document.json").exists())
        self.case.unlink()
        index.sync()
        self.assertEqual(index.documents(),[])

    def test_unsafe_image_rejected_missing_image_degrades(self):
        self.case.write_text(self.body+"\n![图](../../secret.png)",encoding="utf-8")
        with self.assertRaises(ValueError):
            parse_document(self.case)
        self.case.write_text(self.body+"\n![图](images/missing.png)",encoding="utf-8")
        self.assertEqual(len(parse_document(self.case)["warnings"]),1)

    def test_draft_not_retrieved_and_vector_failure_fallback(self):
        index = KnowledgeIndex(self.root/"assets",self.root/"index")
        self.case.write_text(self.body.replace("REVIEWED","DRAFT"),encoding="utf-8")
        index.sync()
        self.assertEqual(index.documents(),[])
        self.case.write_text(self.body,encoding="utf-8")
        index.sync()
        class Broken:
            identity = "model1"
            def embed(self,texts):
                raise RuntimeError("offline")
        hits,mode = index.search("增益",embedder=Broken())
        self.assertTrue(hits)
        self.assertIn("降级",mode)

    def test_qdrant_fusion(self):
        index = KnowledgeIndex(self.root/"assets",self.root/"index")
        index.sync()
        class Embedder:
            identity = "synthetic-vector-test-v1"
            def embed(self,texts):
                return [[1.0,0.0,0.5] for _ in texts]
        hits,mode = index.search("增益",embedder=Embedder())
        self.assertIn("Qdrant",mode)
        self.assertEqual(hits[0]["metadata"]["case_id"],"C1")

    def test_zero_semantic_budget_keeps_bm25_without_embedding(self):
        index=KnowledgeIndex(self.root/'assets',self.root/'index')
        index.sync()
        class Unused:
            identity='unused-model'
            def embed(self,texts):
                raise AssertionError('No request with a zero semantic budget')
        expected,_=index.search('增益')
        hits,mode=index.search('增益',embedder=Unused(),semantic_seconds=0)
        self.assertEqual(hits,expected)
        self.assertIn('时间预算耗尽',mode)

    def test_slow_embedding_stops_before_next_request_and_preserves_bm25(self):
        index=KnowledgeIndex(self.root/'assets',self.root/'index')
        index.sync()
        expected,_=index.search('增益')
        clock=[100.0]
        class Slow:
            identity='synthetic-slow-vector'
            calls=0
            def embed(self,texts):
                self.calls+=1
                clock[0]+=6
                return [[1.0,0.0,0.5] for _ in texts]
        embedder=Slow()
        with patch('srs_helper.knowledge.time.monotonic',side_effect=lambda:clock[0]):
            hits,mode=index.search('增益',embedder=embedder,semantic_seconds=5)
        self.assertEqual(embedder.calls,1)
        self.assertEqual(hits,expected)
        self.assertIn('时间预算耗尽',mode)

    def test_service_budget_error_has_specific_fallback_mode(self):
        from srs_helper.services import ServiceError
        index=KnowledgeIndex(self.root/'assets',self.root/'index')
        index.sync()
        class Expired:
            identity='expired-model'
            def embed(self,texts):
                raise ServiceError('deadline',code='BUDGET_EXHAUSTED')
        hits,mode=index.search('增益',embedder=Expired(),semantic_seconds=5)
        self.assertTrue(hits)
        self.assertIn('时间预算耗尽',mode)

    def test_bounded_semantic_fusion_still_works(self):
        index=KnowledgeIndex(self.root/'assets',self.root/'index')
        index.sync()
        class Fast:
            identity='fast-model'
            def embed(self,texts):
                return [[1.0,0.0,0.5] for _ in texts]
        with patch('srs_helper.knowledge.time.monotonic',return_value=100.0):
            hits,mode=index.search('增益',embedder=Fast(),semantic_seconds=5)
        self.assertTrue(hits)
        self.assertIn('Qdrant',mode)
