"""Retired dossier tools must not be advertised or executable via old requests."""

import unittest

from prompts import SYSTEM_PROMPT
from tool_catalog import get_tool_schemas
from tools import execute_tool


class TestRemovedResearchDocuments(unittest.TestCase):
    def test_removed_tools_are_neither_exposed_nor_dispatched(self):
        names = {schema["function"]["name"] for schema in get_tool_schemas()}
        self.assertTrue({"read_pdf", "query_papers", "save_paper_relation", "read_profile"} <= names)
        self.assertFalse(any("research_document" in name for name in names))
        self.assertNotIn("research_document", SYSTEM_PROMPT)
        self.assertNotIn("研究档案", SYSTEM_PROMPT)
        for name in (
            "list_relation_candidates",
            "save_research_document", "search_research_documents", "read_research_document",
            "list_research_document_sections", "read_research_document_section",
            "prepare_research_document_patch_context", "apply_research_document_patch",
            "list_research_document_versions", "restore_research_document_version",
            "compare_papers_to_research_document", "read_research_document_ledger",
            "apply_research_document_ledger_patch", "review_research_document_innovation",
            "review_new_paper_impact_on_research_document",
        ):
            with self.subTest(name=name):
                self.assertNotIn(name, names)
                self.assertIn("未知工具", execute_tool(name, {}))
