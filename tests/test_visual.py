import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from srs_helper.knowledge import KnowledgeIndex
from srs_helper.services import ServiceConfig
from srs_helper.visual import compare_visual, export_chart


class VisualTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.summary = {"scope":{},"dataset_fingerprint":"abc","groups":[{"ue_uid":"u","ue_total_records":2}],
                        "streams":[{"ue_uid":"u","resourceId":0,"streamId":i,"gain_avg":10-i} for i in range(4)]}

    def test_failed_static_export_preserves_html(self):
        with patch("srs_helper.visual.render_png",side_effect=ValueError("unavailable")):
            result = export_chart(self.summary,self.root/"chart")
        self.assertEqual(result["status"],"HTML_ONLY")
        self.assertTrue(Path(result["html"]).exists())

    def test_vision_requires_images_and_marks_qualitative_result(self):
        index = KnowledgeIndex(self.root/"assets",self.root/"index")
        image = index.storage/"versions/v1/images/reference.png"
        image.parent.mkdir(parents=True)
        Image.new("RGB",(10,10),"blue").save(image)
        documents = [{"path":"cases/c.md","version":"v1","images":[{"path":"images/reference.png","caption":"历史图","sha256":hashlib.sha256(image.read_bytes()).hexdigest()}]}]
        class Service:
            config = ServiceConfig("local_openai","http://localhost","test",supports_vision=True)
            def chat_json(self,messages,images):
                self.images = images
                return {"observations":["形态相似"],"differences":[],"unverified":["不确认根因"]}
        service = Service()
        with patch("srs_helper.visual.export_chart",return_value={"status":"READY","png":str(image),"preview":"test"}):
            result = compare_visual(service,self.summary,documents,index,self.root/"out")
        self.assertEqual(result["status"],"COMPARED")
        self.assertEqual(len(service.images),2)
        service.config.supports_vision = False
        self.assertEqual(compare_visual(service,self.summary,documents,index,self.root/"out")["status"],"NOT_RUN")
