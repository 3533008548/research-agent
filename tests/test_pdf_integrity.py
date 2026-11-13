"""Regression tests for missing pseudocode and silent extraction loss."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, MagicMock

import pymupdf

from pdf_reader import PaperReader
from paper_artifacts import build_document_map, related_context
from paper_quality import prepare_document_map, verify_index_round_trip


class TestPdfIntegrity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "algorithm.pdf"

    def token_store(self):
        from paper_store import PaperStore
        from types import SimpleNamespace
        store = PaperStore.__new__(PaperStore)
        store._embed_fn = SimpleNamespace(tokenize=list)
        store.embedding_max_length = 256
        store._collection = MagicMock()
        store._invalidate_lexical_index = MagicMock()
        return store

    def test_token_split_map_matches_index_and_preserves_chinese_provenance(self):
        import copy
        from paper_artifacts import write_document_map, load_document_map
        from runtime_paths import RuntimePaths

        text = "时间敏感网络通过多径冗余保障可靠性。" * 55
        doc = {"paper_id": "p", "title": "中文论文", "chunks": [
            {"id": "p001-c01", "text": text, "kind": "text", "page": 1, "page_end": 2,
             "source_element_ids": ["p001-t01", "p002-t01"], "related_ids": ["p002-b01"]},
            {"id": "p003-c01", "text": text, "kind": "text", "page": 3,
             "source_element_ids": ["p003-t01"], "related_ids": []},
        ]}
        before = copy.deepcopy(doc)
        store = self.token_store()
        store.index_document_map(doc)
        written = store._collection.add.call_args.kwargs
        store._collection.get.return_value = {
            "ids": written["ids"], "documents": written["documents"], "metadatas": written["metadatas"],
        }
        readback = store.get_paper_chunks("p")
        self.assertGreater(len(readback), len(before["chunks"]))
        self.assertTrue(verify_index_round_trip(doc, readback)["accepted"])
        self.assertFalse(verify_index_round_trip(before, readback)["accepted"])
        self.assertEqual([c["element_id"] for c in readback], [c["id"] for c in doc["chunks"]])
        self.assertEqual(len(set(c["id"] for c in doc["chunks"])), len(doc["chunks"]))
        self.assertTrue(all(len(c["text"]) <= 256 for c in doc["chunks"]))
        for parent in before["chunks"]:
            children = [c for c in doc["chunks"] if c["parent_chunk_id"] == parent["id"]]
            coverage = set()
            base = children[0]["char_start"]
            for child in children:
                self.assertEqual(child["source_element_ids"], parent["source_element_ids"])
                self.assertEqual(child["related_ids"], parent["related_ids"])
                self.assertEqual(child.get("page_end"), parent.get("page_end"))
                start = child["char_start"] - base
                self.assertEqual(parent["text"][start:start + len(child["text"])], child["text"])
                coverage.update(range(start, start + len(child["text"])))
            self.assertEqual(coverage, set(range(len(parent["text"]))))
        paths = RuntimePaths.from_root(self.tmp.name)
        write_document_map(paths, paper_id="p", document_map=doc)
        self.assertTrue(verify_index_round_trip(load_document_map(paths, "p"), readback)["accepted"])
        # Repeated indexing of the final map must not split or rename it again.
        final = copy.deepcopy(doc)
        store.index_document_map(doc)
        self.assertEqual([c["id"] for c in doc["chunks"]], [c["id"] for c in final["chunks"]])

    def test_failed_write_does_not_change_document_map(self):
        import copy
        doc = {"chunks": [{"id": "c1", "text": "中" * 700}]}
        before = copy.deepcopy(doc)
        store = self.token_store()
        store._collection.add.side_effect = RuntimeError("write failed")
        with self.assertRaisesRegex(RuntimeError, "write failed"):
            store.index_document_map(doc)
        self.assertEqual(doc, before)

    def test_round_trip_rejects_lost_duplicate_and_truncated_chunks(self):
        doc = {"chunks": [{"id": "a", "text": "完整内容甲"}, {"id": "b", "text": "完整内容乙"}]}
        complete = [{"element_id": c["id"], "text": c["text"]} for c in doc["chunks"]]
        self.assertTrue(verify_index_round_trip(doc, complete)["accepted"])
        for corrupted in (complete[:1], [complete[0], complete[0]], [complete[0], {"element_id": "b", "text": "完整"}]):
            result = verify_index_round_trip(doc, corrupted)
            self.assertFalse(result["accepted"])
            self.assertIn("预期 2 块", result["issues"][0]["message"])
        self.assertFalse(verify_index_round_trip({"chunks": [doc["chunks"][0]] * 2}, complete[:1])["accepted"])

    def make_panel(self, *, raster=False, vector=False, tall=False,
                   caption="Algorithm 4: Least Bandwidth Consumption"):
        with pymupdf.open() as doc:
            page = doc.new_page(width=600, height=800)
            page.insert_text((50, 50), caption)
            for y in (36, 54, 260):
                page.draw_line((45, y), (305, y))
            code = "1 Function LBC(paths, r):\n2 for edge in paths do\n3   add edge to edgeList\n4 end\n5 Return paths with smallest averBand"
            if raster or vector:
                with pymupdf.open() as image_doc:
                    p = image_doc.new_page(width=260, height=180)
                    p.insert_text((8, 20), code, fontsize=10)
                    if vector:
                        with pymupdf.open(stream=p.get_svg_image(text_as_path=True).encode(), filetype="svg") as svg:
                            with pymupdf.open(stream=svg.convert_to_pdf(), filetype="pdf") as outlined:
                                page.show_pdf_page(pymupdf.Rect(45, 60, 305, 240), outlined, 0)
                    else:
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

    def test_vector_algorithm_without_caption_punctuation_is_not_silently_lost(self):
        self.make_panel(vector=True, tall=True, caption="Algorithm 4 Routing Algorithm")
        with pymupdf.open(self.path) as pdf:
            self.assertNotIn("averBand", pdf[0].get_text())
            self.assertFalse(pdf[0].get_image_info())
        body = "1 Function Route(paths):\n2 for edge in paths do\n3 Return paths with smallest averBand"
        with patch("pdf_ocr.ocr_region", return_value=body) as ocr:
            m, q = self.parse_map()
        self.assertTrue(q["accepted"], q)
        self.assertTrue(any(c["kind"] == "algorithm" and "averBand" in c["text"] for c in m["chunks"]))
        ocr.assert_called_once()

    def test_punctuationless_vector_panel_fails_closed_when_ocr_fails(self):
        self.make_panel(vector=True, tall=True, caption="Algorithm 4 Routing Algorithm")
        for tables in (True, False):
            with patch("pdf_ocr.ocr_region", side_effect=RuntimeError("OCR unavailable")):
                d = PaperReader(max_pages=1).parse_document(self.path, extract_tables=tables)
            _, q = prepare_document_map(build_document_map(d, paper_id="p", title="Paper", source_file=self.path.name), requested_pages=1)
            self.assertFalse(q["accepted"], q)
            self.assertIn("unresolved_visual_text", {i["code"] for i in q["issues"]})

    def test_native_punctuationless_panel_preserves_algorithm_identity(self):
        self.make_panel(caption="Algorithm 4 Routing Algorithm")
        with patch("pdf_ocr.ocr_region", side_effect=AssertionError("native body must not use OCR")):
            m, q = self.parse_map()
        self.assertTrue(q["accepted"], q)
        self.assertTrue(any(c["kind"] == "algorithm" and "averBand" in c["text"] for c in m["chunks"]))

    def test_mixed_native_header_and_vector_body_still_requires_ocr(self):
        self.make_panel(vector=True, caption="Algorithm 4 Routing Algorithm")
        with pymupdf.open(self.path) as pdf:
            pdf[0].insert_text((50, 220), "Input graph and candidate paths.\nOutput the best feasible path, then return to the caller.", fontsize=8)
            pdf.saveIncr()
        with patch("pdf_ocr.ocr_region", side_effect=RuntimeError("OCR unavailable")) as ocr:
            _, q = self.parse_map()
        ocr.assert_called_once()
        self.assertFalse(q["accepted"])

    def test_quality_gate_detects_algorithm_element_lost_after_extraction(self):
        self.make_panel()
        d = PaperReader().parse_document(self.path)
        d['pages'][0]['elements'] = [e for e in d['pages'][0]['elements'] if e['kind'] != 'algorithm']
        _, q = prepare_document_map(build_document_map(d, paper_id="p", title="Paper", source_file=self.path.name), requested_pages=1)
        self.assertIn("algorithm_region_loss", {i["code"] for i in q["issues"]})

    def test_prose_algorithm_mention_in_other_column_does_not_trigger_ocr(self):
        with pymupdf.open() as pdf:
            page = pdf.new_page()
            page.insert_text((50, 50), "Algorithm 4 provides the optimized path.\nThis is explanatory prose rather than an algorithm caption.")
            for y in (36, 54, 260):
                page.draw_line((350, y), (550, y))
            pdf.save(self.path)
        with patch("pdf_ocr.ocr_region", side_effect=AssertionError("prose must not be OCRed")) as ocr:
            d = PaperReader().parse_document(self.path)
        ocr.assert_not_called()
        self.assertFalse(d['pages'][0]['extraction_diagnostics'])

    def test_algorithm_transcription_does_not_reuse_generic_image_description(self):
        from tools.describe import handle_describe_image
        from runtime_paths import RuntimePaths
        paths = RuntimePaths.from_root(Path(self.tmp.name) / 'runtime')
        paths.ensure_initialized()
        image = paths.images_dir / 'algorithm.png'
        image.write_bytes(b'fixture')
        memory = MagicMock()
        memory.get_image_description.side_effect = lambda key: 'generic description' if key == str(image) else None
        client = MagicMock()
        client.post.return_value.json.return_value = {'choices': [{'message': {'content': '1 Function Route\n2 return path'}}]}
        with patch('tools.describe.get_runtime_paths', return_value=paths):
            result = handle_describe_image({'image_path': str(image), 'task': 'transcribe_algorithm'},
                                           memory_store=memory, llm_client=client)
        self.assertIn('return path', result)
        prompt = client.post.call_args.args[0]['messages'][0]['content'][0]['text']
        self.assertIn('逐行转写', prompt)
        self.assertIn('[无法辨认]', prompt)
        self.assertNotEqual(memory.save_image_description.call_args.args[0], str(image))

    def scoped_algorithm_store(self, *, source_file="algorithm.pdf"):
        import shutil
        from paper_artifacts import write_document_map
        from runtime_paths import RuntimePaths
        paths = RuntimePaths.from_root(Path(self.tmp.name) / "runtime")
        paths.ensure_initialized()
        shutil.copyfile(self.path, paths.papers_dir / "algorithm.pdf")
        # Mimic an old index whose map and embedding contain no pseudocode.
        write_document_map(paths, paper_id="p", document_map={
            "paper_id": "p", "title": "Routing Paper", "extraction_version": 2,
            "source_file": source_file, "total_pages": 1, "processed_pages": 1,
            "pages": [], "chunks": [],
        })
        store = MagicMock()
        store.list_papers.return_value = [{"paper_id": "p", "title": "Routing Paper"}]
        store.query_with_timeout.side_effect = AssertionError("Numbered algorithms must bypass top-k retrieval")
        return paths, store

    def test_old_index_recovers_full_algorithm_and_reuses_versioned_source_cache(self):
        from tools.search import handle_query_papers
        self.make_panel(vector=True, tall=True, caption="Algorithm 4 Routing Algorithm")
        paths, store = self.scoped_algorithm_store()
        body = "1 Function Route(paths):\n" + "\n".join(f"{i} for edge in path do keep candidate" for i in range(2, 90)) + "\n90 Return finalWitness"
        original_map = next(paths.paper_artifacts_dir.glob('*/document_map.json')).read_bytes()
        with patch("tools.search.get_runtime_paths", return_value=paths), \
             patch("pdf_ocr.ocr_region", return_value=body) as ocr:
            result = handle_query_papers({"query": "解释算法四", "paper_id_or_title": "p", "top_k": 1}, paper_store=store)
            again = handle_query_papers({"query": "Algorithm 4", "paper_id_or_title": "p"}, paper_store=store)
        self.assertIn(body, result)
        self.assertIn("OCR", result)
        self.assertIn("p.1", result)
        self.assertIn("describe_image", result)
        self.assertEqual(result, again)
        ocr.assert_called_once()
        store.index_document_map.assert_not_called()
        store.delete_paper.assert_not_called()
        self.assertEqual(next(paths.paper_artifacts_dir.glob('*/document_map.json')).read_bytes(), original_map)
        self.assertEqual(len(list(paths.images_dir.glob('*.png'))), 1)

    def test_algorithm_read_failure_is_explicit_and_retried_after_recovery(self):
        from paper_algorithms import query_algorithm_evidence
        self.make_panel(vector=True, tall=True, caption="Algorithm 4 Routing Algorithm")
        paths, _ = self.scoped_algorithm_store()
        with patch("pdf_ocr.ocr_region", side_effect=RuntimeError("OCR unavailable")):
            failed = query_algorithm_evidence("Algorithm 4", ["p"], paths)
        self.assertIn("OCR unavailable", failed)
        self.assertIn("尚未恢复", failed)
        self.assertIn("原页备用图", failed)
        self.assertTrue(list(paths.images_dir.glob('*algorithm-page.png')))
        self.assertFalse(list(paths.paper_artifacts_dir.glob('*/algorithms-*.json')))
        with patch("pdf_ocr.ocr_region", return_value="1 Function Route(paths):\n2 for edge in paths do\n3 Return repairedWitness"):
            recovered = query_algorithm_evidence("Algorithm 4", ["p"], paths)
        self.assertIn("repairedWitness", recovered)

    def test_algorithm_lookup_does_not_escape_paper_scope_or_source_directory(self):
        from paper_algorithms import query_algorithm_evidence
        self.make_panel()
        paths, _ = self.scoped_algorithm_store(source_file=str(self.path))
        with patch("paper_algorithms._read_regions", side_effect=AssertionError("must not open outside managed PDFs")):
            result = query_algorithm_evidence("Algorithm 4", ["p"], paths)
        self.assertIn("不可读", result)
        self.assertIsNone(query_algorithm_evidence("Algorithm 4", None, paths))
        self.assertIsNone(query_algorithm_evidence("routing approaches", ["p"], paths))
        self.assertIn("缺少原 PDF 路径", query_algorithm_evidence("Algorithm 4", ["other-paper"], paths))

    def test_algorithm_read_checks_source_pages_beyond_old_index_coverage(self):
        from paper_algorithms import query_algorithm_evidence
        self.make_panel(caption="Algorithm 4 Routing Algorithm")
        with pymupdf.open(self.path) as pdf:
            pdf.new_page(pno=0).insert_text((40, 50), "Introductory text before the algorithm page.")
            pdf.save(Path(self.tmp.name) / "two-pages.pdf")
        self.path = Path(self.tmp.name) / "two-pages.pdf"
        paths, _ = self.scoped_algorithm_store()
        result = query_algorithm_evidence("Algorithm 4", ["p"], paths)
        self.assertIn("p.2", result)
        self.assertIn("averBand", result)

    def test_scoped_pseudocode_request_without_number_recovers_algorithms(self):
        from paper_algorithms import query_algorithm_evidence
        self.make_panel(caption="Algorithm 4 Routing Algorithm")
        paths, _ = self.scoped_algorithm_store()
        for query in ('解释这篇论文的伪代码', 'show the pseudocode'):
            result = query_algorithm_evidence(query, ["p"], paths)
            self.assertIn('Algorithm 4', result)
            self.assertIn('averBand', result)

    def test_numbered_algorithm_lookup_does_not_substitute_another_number(self):
        from paper_algorithms import query_algorithm_evidence
        self.make_panel(caption="Algorithm 40 Routing Algorithm")
        paths, _ = self.scoped_algorithm_store()
        result = query_algorithm_evidence("Algorithm 4", ["p"], paths)
        self.assertNotIn('averBand', result)
        self.assertIn('尚未恢复算法 4', result)

    def test_algorithm_source_cache_invalidates_when_pdf_changes(self):
        from paper_algorithms import query_algorithm_evidence
        self.make_panel(vector=True, caption="Algorithm 4 Routing Algorithm")
        paths, _ = self.scoped_algorithm_store()
        source = paths.papers_dir / "algorithm.pdf"
        with patch("pdf_ocr.ocr_region", return_value="1 Function Route(paths):\n2 for edge in paths do\n3 Return oldWitness"):
            self.assertIn("oldWitness", query_algorithm_evidence("Algorithm 4", ["p"], paths))
        with pymupdf.open(source) as pdf:
            pdf.set_metadata({"title": "Changed source"})
            pdf.saveIncr()
        with patch("pdf_ocr.ocr_region", return_value="1 Function Route(paths):\n2 for edge in paths do\n3 Return newWitness") as ocr:
            result = query_algorithm_evidence("Algorithm 4", ["p"], paths)
        self.assertIn("newWitness", result)
        self.assertNotIn("oldWitness", result)
        ocr.assert_called_once()

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
            from pdf_ocr import resolve_ocr_tessdata
            tessdata = resolve_ocr_tessdata('eng')
        except RuntimeError:
            self.skipTest("Tesseract language data not installed")
        if not (Path(tessdata) / "eng.traineddata").is_file():
            self.skipTest("English Tesseract language data not installed")
        self.make_panel(raster=True)
        m, q = self.parse_map()
        self.assertTrue(q["accepted"], q)
        self.assertTrue(any(c["kind"] == "algorithm" and "Return" in c["text"] for c in m["chunks"]))

    def test_packaged_ocr_uses_bundled_language_data_without_system_install(self):
        import sys
        from pdf_ocr import resolve_ocr_tessdata
        bundled = Path(self.tmp.name) / 'bundle' / 'tessdata'
        bundled.mkdir(parents=True)
        for language in ('eng', 'chi_sim'):
            (bundled / f'{language}.traineddata').write_bytes(b'fixture')
        with patch.object(sys, '_MEIPASS', str(bundled.parent), create=True), \
             patch.dict(os.environ, {'TESSDATA_PREFIX': str(Path(self.tmp.name) / 'missing')}), \
             patch('pymupdf.get_tessdata', side_effect=RuntimeError('not installed')):
            self.assertEqual(resolve_ocr_tessdata('eng+chi_sim'), str(bundled))

    def test_missing_ocr_data_identifies_page_region_and_reason(self):
        from pdf_ocr import OCRLanguageDataError
        from paper_quality import quality_failure_summary
        self.make_panel(raster=True)
        with patch('pdf_ocr.resolve_ocr_tessdata', side_effect=OCRLanguageDataError('缺少 OCR 语言数据（eng）')):
            _, quality = self.parse_map()
        self.assertFalse(quality['accepted'])
        summary = quality_failure_summary(quality)
        for detail in ('第 1 页', 'Algorithm 4', '缺少 OCR 语言数据'):
            self.assertIn(detail, summary)

    def test_build_refuses_corrupt_ocr_resource_and_keeps_it_out_of_bundle(self):
        import io
        from scripts import fetch_ocr_data
        destination = Path(self.tmp.name) / 'download'
        with patch.dict(fetch_ocr_data.FILES, {'eng.traineddata': '0' * 64}, clear=True), \
             patch('urllib.request.urlopen', side_effect=lambda *a, **k: io.BytesIO(b'corrupt download')):
            with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
                fetch_ocr_data.ensure_ocr_data(destination)
        self.assertFalse((destination / 'eng.traineddata').exists())
        self.assertFalse(list(destination.glob('*.part')))


if __name__ == "__main__":
    unittest.main()
