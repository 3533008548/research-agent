"""Regression tests for missing pseudocode and silent extraction loss."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, MagicMock

import pymupdf

from pdf_reader import PaperReader
from paper_artifacts import build_document_map, related_context
from paper_quality import prepare_document_map


class TestPdfIntegrity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "algorithm.pdf"

    def make_panel(self, *, raster=False, tall=False):
        with pymupdf.open() as doc:
            page = doc.new_page(width=600, height=800)
            page.insert_text((50, 50), "Algorithm 4: Least Bandwidth Consumption")
            for y in (36, 54, 260):
                page.draw_line((45, y), (305, y))
            code = "1 Function LBC(paths, r):\n2 for edge in paths do\n3   add edge to edgeList\n4 end\n5 Return paths with smallest averBand"
            if raster:
                with pymupdf.open() as image_doc:
                    p = image_doc.new_page(width=260, height=180)
                    p.insert_text((8, 20), code, fontsize=10)
                    page.insert_image(pymupdf.Rect(45, 60, 305, 240), stream=p.get_pixmap(matrix=pymupdf.Matrix(3, 3)).tobytes("png"))
            else:
                page.insert_text((50, 80), code, fontsize=10)
            if tall:
                page.insert_text((325, 50), "\n".join(f"Long body evidence line {i}" for i in range(45)), fontsize=10)
            doc.save(self.path)

    def parse_map(self):
        d = PaperReader(max_pages=1).parse_document(self.path)
        return prepare_document_map(build_document_map(d, paper_id="p", title="Paper", source_file=self.path.name), requested_pages=1)

    def test_native_algorithm_and_tall_column_survive_into_chunks(self):
        self.make_panel(tall=True)
        with patch("pdf_ocr.ocr_region", side_effect=AssertionError("native text must not use OCR")):
            m, q = self.parse_map()
        self.assertTrue(q["accepted"], q)
        content = "\n".join(c["text"] for c in m["chunks"])
        self.assertIn("Long body evidence line 44", content)
        self.assertTrue(any(c["kind"] == "algorithm" and "Return" in c["text"] for c in m["chunks"]))

    def test_explicit_algorithm_locator_bypasses_semantic_distractors(self):
        from paper_store import PaperStore
        store = PaperStore.__new__(PaperStore)
        store._collection = MagicMock()
        store._collection.get.return_value = {
            "documents": ["Algorithm 40: Distractor\nReturn wrong", "Algorithm 4: LBC\nReturn averBand"],
            "metadatas": [{"paper_id": "p", "chunk_index": i, "page": 9, "element_kind": "algorithm"} for i in (0, 1)],
        }
        store.query = MagicMock(side_effect=AssertionError("Locator should not depend on embeddings"))
        for query in ("Algorithm 4 pseudocode input output", "解释算法四"):
            hits = store.query_hybrid(query, paper_ids=["p"])
            self.assertEqual(len(hits), 1)
            self.assertIn("averBand", hits[0]["text"])
        where = store._collection.get.call_args.kwargs["where"]
        self.assertIn({"paper_id": {"$in": ["p"]}}, where["$and"])
        store._collection.reset_mock()
        self.assertEqual(store._query_algorithm_label("Algorithm 4", 3, None, None), [])
        self.assertEqual(store._query_algorithm_label("Algorithm 4", 3, [], None), [])
        self.assertEqual(store._query_algorithm_label("算法四十一", 3, ["p"], None), [])
        self.assertEqual(store._query_algorithm_label("Algorithm 4.1", 3, ["p"], None), [])
        store._collection.get.assert_not_called()

    def test_ocr_failure_cannot_pass_either_parser_strategy(self):
        self.make_panel(raster=True)
        for tables in (True, False):
            with patch("pdf_ocr.ocr_region", side_effect=RuntimeError("missing language data")):
                d = PaperReader(max_pages=1).parse_document(self.path, extract_tables=tables)
            m, q = prepare_document_map(build_document_map(d, paper_id="p", title="Paper", source_file=self.path.name), requested_pages=1)
            self.assertFalse(q["accepted"])
            self.assertIn("unresolved_visual_text", {i["code"] for i in q["issues"]})

    def test_caption_only_ocr_is_rejected(self):
        self.make_panel(raster=True)
        with patch("pdf_ocr.ocr_region", return_value="Algorithm 4: Least Bandwidth Consumption"):
            _, q = self.parse_map()
        self.assertFalse(q["accepted"])

    def test_ocr_is_searchable_and_keeps_provenance_and_crop(self):
        from pdf_ocr import attach_visual_assets
        self.make_panel(raster=True)
        body = "1 Function LBC(paths, r):\n2 for edge in paths do\n3 add edge to edgeList\n4 Return paths with smallest averBand"
        with patch("pdf_ocr.ocr_region", return_value=body):
            m, q = self.parse_map()
        self.assertTrue(q["accepted"], q)
        assets = attach_visual_assets(m, self.path, Path(self.tmp.name) / "images")
        self.assertTrue(all(Path(a).is_file() for a in assets))
        chunk = next(c for c in m["chunks"] if c["kind"] == "algorithm")
        self.assertIn("Algorithm 4", chunk["text"])
        self.assertIn("averBand", chunk["text"])
        context, locator = related_context(m, chunk["id"])
        self.assertIn("OCR", locator)
        self.assertIn("describe_image", context)

    def test_partial_table_cannot_mask_unrepresented_text(self):
        block = {"bbox": [0, 0, 100, 100], "text": "Input Output Return essential evidence"}
        partial = {"bbox": [0, 0, 100, 100], "text": "| Input | Output |"}
        self.assertFalse(PaperReader._block_represented(block, partial))
        partial["text"] = "| Input | Output | Return essential evidence |"
        self.assertTrue(PaperReader._block_represented(block, partial))

    def test_independent_audit_catches_dropped_native_body(self):
        self.make_panel(tall=True)
        original = PaperReader._extract_text_blocks
        def drop_body(reader, page):
            return [b for b in original(reader, page) if "Long body" not in b["text"]]
        with patch.object(PaperReader, "_extract_text_blocks", drop_body):
            _, q = self.parse_map()
        self.assertFalse(q["accepted"])
        self.assertIn("native_text_loss", {i["code"] for i in q["issues"]})

    def test_prose_algorithm_reference_is_not_a_missing_panel(self):
        from pdf_ocr import ALGORITHM_CAPTION
        self.assertIsNone(ALGORITHM_CAPTION.match("Algorithm 4 provides the optimized path."))
        self.assertIsNotNone(ALGORITHM_CAPTION.match("Algorithm 4: Optimized path"))

    def test_scan_ocr_failure_is_not_accepted_as_figure_only(self):
        self.make_panel(raster=True)
        # Remove the native title while retaining the raster body.
        with pymupdf.open(self.path) as doc:
            page = doc[0]
            page.add_redact_annot(pymupdf.Rect(40, 30, 310, 55))
            page.apply_redactions()
            doc.save(Path(self.tmp.name) / "scan.pdf")
        self.path = Path(self.tmp.name) / "scan.pdf"
        with patch("pdf_ocr.ocr_region", side_effect=RuntimeError("OCR unavailable")):
            _, q = self.parse_map()
        self.assertFalse(q["accepted"])

    def test_local_tesseract_recovers_raster_algorithm(self):
        try:
            tessdata = pymupdf.get_tessdata()
        except RuntimeError:
            self.skipTest("Tesseract language data not installed")
        if not (Path(tessdata) / "eng.traineddata").is_file():
            self.skipTest("English Tesseract language data not installed")
        self.make_panel(raster=True)
        m, q = self.parse_map()
        self.assertTrue(q["accepted"], q)
        self.assertTrue(any(c["kind"] == "algorithm" and "Return" in c["text"] for c in m["chunks"]))


if __name__ == "__main__":
    unittest.main()
