import tempfile
import unittest
from pathlib import Path

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
