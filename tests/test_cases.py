import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from srs_helper.cases import publish_case
from srs_helper.knowledge import KnowledgeIndex


class CaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.index = KnowledgeIndex(self.root/"assets",self.root/"index")
        self.draft = self.root/"draft.md"
        self.text = """---
case_id: CASE_TEST
revision: 1
title: 合成测试案例
review_status: DRAFT
applicability: 仅开发测试
source_refs:
  - document_id: synthetic
    revision: '1'
    section: test
---
这是合成测试的排查记录，不确认实际根因。
"""
        self.draft.write_text(self.text,encoding="utf-8")

    def test_approval_required_and_index_failure_retry(self):
        with self.assertRaises(ValueError):
            publish_case(self.index,self.draft,"tester")
        with patch.object(self.index,"sync",side_effect=RuntimeError("offline")):
            result = publish_case(self.index,self.draft,"tester",approved=True)
        self.assertEqual(result["status"],"PENDING_INDEX")
        self.assertTrue(Path(result["path"]).exists())
        retry = publish_case(self.index,self.draft,"tester",approved=True)
        self.assertEqual(retry["status"],"READY")
        self.assertEqual(retry["path"],result["path"])
        self.assertEqual(len(self.index.documents()),1)

    def test_revision_immutable_latest_retrieved(self):
        first = publish_case(self.index,self.draft,"tester",approved=True)
        self.draft.write_text(self.text+"改变",encoding="utf-8")
        with self.assertRaises(ValueError):
            publish_case(self.index,self.draft,"tester",approved=True)
        self.draft.write_text(self.text.replace("revision: 1","revision: 2")+"改变",encoding="utf-8")
        publish_case(self.index,self.draft,"tester",approved=True)
        self.assertTrue(Path(first["path"]).exists())
        self.assertEqual(self.index.documents()[0]["metadata"]["revision"],2)
