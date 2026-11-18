import base64
import io
import tempfile
import unittest
from unittest.mock import patch

import fitz
from pydantic import ValidationError

from agent.graph import build_graph
from papers.library import LibraryStore
from integrations.llm_client import LLMClient
from papers.selections import PaperSelection, element_citations, referenced_message, region_preview
from infra.runtime_paths import RuntimePaths
from tests.test_verification_policy import Response


class PaperSelectionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        paths = RuntimePaths.from_root(directory.name)
        self.library = LibraryStore(paths)
        with fitz.open() as pdf:
            page = pdf.new_page(width=200, height=120)
            page.draw_rect(fitz.Rect(10, 10, 70, 45), color=None, fill=(1, 0, 0))
            page.set_rotation(90)
            self.doc = self.library.import_file("figure [1].pdf", io.BytesIO(pdf.tobytes()))
        self.fields = dict(document_id=self.doc["id"], page=1, bounds=(700000, 120000, 800000, 180000))
        self.graph = build_graph(api_key="test", checkpoint_db=":memory:",
                                 model="text-test", vision_model="vision-test", selection_paths=paths,
                                 llm_client=LLMClient("test", "https://example.test/chat"))
        self.addCleanup(self.graph.checkpointer.conn.close)

    def test_rotated_region_and_text_reach_the_model_on_the_correct_turn(self):
        area = PaperSelection(**self.fields, kind="area")
        text = PaperSelection(**self.fields, kind="text", quote="First line\nSecond line")
        config = {"configurable": {"thread_id": "selected-region"}}
        for reference, model in ((area, "vision-test"), (text, "text-test")):
            question = referenced_message("我的问题", [reference], self.library)
            with patch("integrations.llm_client.requests.post", return_value=Response("答复 [S1]")) as post:
                state = self.graph.invoke({"messages": [{"role": "user", "content": question}]}, config=config)
            payload = post.call_args.kwargs["json"]
            self.assertEqual(payload["model"], model)
            content = payload["messages"][-1]["content"]
            if reference.kind == "text":
                self.assertIn("> First line\n> Second line", content)
            else:
                self.assertEqual(content[0]["text"], question)
                self.assertIn("S1", content[1]["text"])
                encoded = content[2]["image_url"]["url"].split(",", 1)[1]
                crop = fitz.Pixmap(base64.b64decode(encoded))
                self.assertEqual((crop.width, crop.height), (36, 36))
                self.assertEqual(crop.pixel(18, 18), (255, 0, 0))
            self.assertNotIn("data:image/", str(state["messages"]))

    def test_verifier_can_inspect_the_same_figure_when_tools_were_used(self):
        area = PaperSelection(**self.fields, kind="area")
        state = {"messages": [
            {"role": "user", "content": referenced_message("图中有什么？", [area], self.library)},
            {"role": "tool", "content": "📚 Related evidence.", "tool_call_id": "t"},
            {"role": "assistant", "content": "原图展示的颜色。" * 20},
        ]}
        with patch("integrations.llm_client.requests.post", return_value=Response("OK")) as post:
            self.graph.get_graph().nodes["verify"].data.invoke(state)
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["model"], "vision-test")
        self.assertEqual(payload["messages"][0]["content"][-1]["type"], "image_url")

    def test_invalid_geometry_page_and_missing_source(self):
        with self.assertRaises(ValidationError):
            PaperSelection(**{**self.fields, "bounds": (4, 2, 4, 8)}, kind="area")
        with self.assertRaises(ValidationError):
            PaperSelection(**self.fields, kind="text", quote=" ")
        past_end = PaperSelection(**{**self.fields, "page": 2}, kind="area")
        with self.assertRaises(ValueError):
            referenced_message("问题", [past_end], self.library)
        self.library.update(self.doc["id"], {"trashed": True})
        with self.assertRaises(KeyError):
            referenced_message("问题", [PaperSelection(**self.fields, kind="area")], self.library)

    def test_retrieved_passage_uses_immutable_pdf_even_after_source_changes(self):
        paths = self.library.paths
        source = paths.papers_dir / "method.pdf"
        with fitz.open() as pdf:
            page = pdf.new_page()
            page.insert_text((60, 80), "Independent measurement.")
            bbox = list(page.search_for("Independent measurement.")[0])
            pdf.save(source)
        mapping = {"source_file": source.name, "pages": [{"page": 1, "elements": [
            {"id": "p1", "kind": "text", "text": "Independent measurement.", "bbox": bbox},
        ]}]}
        links, snapshot = element_citations(paths, mapping, {"p1"})
        self.assertIn(snapshot, links["p1"])
        coords = links["p1"].split("/")[-1].rstrip(")").split(",")
        selection = PaperSelection(document_id=snapshot, page=1, bounds=tuple(map(int, coords)), kind="area")
        source.write_bytes(self.library.file(self.doc["id"]).read_bytes())
        changed_links, _ = element_citations(paths, mapping, {"p1"})
        self.assertFalse(changed_links)
        source.unlink()
        restored, _ = element_citations(paths, {**mapping, "document_id": snapshot}, {"p1"})
        self.assertEqual(restored, links)
        image, text = region_preview(self.library, selection, 900)
        self.assertIn("Independent measurement.", text)
        self.assertTrue(image.startswith("data:image/png;base64,"))
