import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.models.schema import VideoParams
from app.services import task_artifacts


class TestTaskArtifacts(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.task_dir = Path(self.temp_dir.name)
        self.task_dir_patch = patch(
            "app.services.task_artifacts.utils.task_dir",
            return_value=str(self.task_dir),
        )
        self.task_dir_patch.start()

    def tearDown(self):
        self.task_dir_patch.stop()
        self.temp_dir.cleanup()



    def test_patch_missing_script_is_non_blocking(self):
        """独立调用素材下载时没有任务清单，应静默跳过而不是创建残缺 JSON。"""
        updated = task_artifacts.patch_script_data(
            "standalone",
            material_sources=[],
        )

        self.assertFalse(updated)
        self.assertFalse((self.task_dir / "script.json").exists())

    def test_patch_invalid_script_returns_false_without_overwrite(self):
        """历史 JSON 损坏时必须保留原文件、记录错误，并允许视频主流程继续。"""
        target = self.task_dir / "script.json"
        target.write_text("{invalid-json", encoding="utf-8")

        with patch.object(task_artifacts.logger, "warning") as warning:
            updated = task_artifacts.patch_script_data(
                "task-1",
                material_sources=[],
            )

        self.assertFalse(updated)
        self.assertEqual(target.read_text(encoding="utf-8"), "{invalid-json")
        self.assertTrue(warning.called)


if __name__ == "__main__":
    unittest.main()
