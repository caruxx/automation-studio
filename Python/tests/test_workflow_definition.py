"""workflow.json の工程・資源定義: 依存が非循環で、別名と資源 id が整合する。"""
try:
    from . import _isolation  # noqa: F401
except ImportError:
    import _isolation  # noqa: F401
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import exit_codes as ec  # noqa: E402


class WorkflowDefinitionTest(unittest.TestCase):
    def test_stage_order_and_stages_agree(self):
        order = ec.stage_order()
        ids = [s["id"] for s in ec.stages()]
        self.assertEqual(order, ids)
        self.assertEqual(len(set(ids)), len(ids))
        self.assertEqual(order[0], "suno")
        self.assertIn("select", order)
        self.assertIn("publish_verify", order)

    def test_dependencies_are_acyclic_and_point_backwards(self):
        order = ec.stage_order()
        pos = {s: i for i, s in enumerate(order)}
        for st in ec.stages():
            for dep in list(st["depends_on"]) + list(st.get("soft_depends_on", [])):
                self.assertIn(dep, pos, msg=f"{st['id']} -> {dep}")
                self.assertLess(pos[dep], pos[st["id"]], msg=f"{st['id']} は {dep} より後に並ぶ")
        upload = next(s for s in ec.stages() if s["id"] == "upload")
        self.assertIn("localization", upload["soft_depends_on"])  # 翻訳の完了（または確定失敗）を待ってから投稿

    def test_resources_and_fields(self):
        res_ids = {r["id"] for r in ec.resources()}
        for r in ec.resources():
            self.assertIn(r["scope"], ("machine", "channel", "global"))
            self.assertGreaterEqual(int(r["capacity"]), 1)
        for st in ec.stages():
            self.assertIn(st["executor"], ("mac", "vps"))
            self.assertTrue(set(st["resources"]) <= res_ids, msg=st["id"])
            self.assertGreater(int(st["timeout_seconds"]), 0)
            self.assertGreaterEqual(int(st["retry"]["max_attempts"]), 1)
            self.assertTrue(st["done_validator"])

    def test_aliases_fall_back_to_builtin(self):
        saved = ec._WORKFLOW
        try:
            ec._WORKFLOW = {"exit_reasons": []}
            self.assertEqual(ec.canonical_stage("psd_composite"), "composite")
            ec._WORKFLOW = {"stage_aliases": "broken"}
            self.assertEqual(ec.canonical_stage("export"), "render")
        finally:
            ec._WORKFLOW = saved

    def test_aliases_map_old_names(self):
        import app_pipeline as p
        aliases = ec.stage_aliases()
        order = set(ec.stage_order())
        self.assertEqual(ec.canonical_stage("psd_composite"), "composite")
        self.assertEqual(ec.canonical_stage("export"), "render")
        self.assertIsNone(ec.canonical_stage("premiere"))
        self.assertEqual(ec.canonical_stage("qa"), "qa")
        for old in p.STEPS:
            new = ec.canonical_stage(old)
            if new is not None:
                self.assertIn(new, order, msg=f"{old} -> {new}")
        for new in aliases.values():
            if new is not None:
                self.assertIn(new, order)


if __name__ == "__main__":
    unittest.main()
