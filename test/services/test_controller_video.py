import asyncio
import os
import shutil
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.config import config
from app.controllers.manager.base_manager import TaskQueueFullError
from app.controllers.v1 import video as video_controller
from app.models import const
from app.models.exception import HttpException
from app.models.schema import TaskListResponse, TaskQueryResponse
from app.services import state as sm
from app.utils import utils


class TestVideoControllerHelpers(unittest.TestCase):
    @staticmethod
    def _request(range_header=None):
        headers = {"x-task-id": "request-123"}
        if range_header is not None:
            headers["Range"] = range_header
        return SimpleNamespace(headers=headers)

    def test_sanitize_upload_filename_removes_client_path(self):
        """Windows 和 POSIX 客户端路径都只能保留最后一段安全文件名。"""
        for filename, expected in (
            (r"C:\videos\clip.MOV", "clip.MOV"),
            ("../../images/photo.png", "photo.png"),
        ):
            with self.subTest(filename=filename):
                self.assertEqual(
                    video_controller._sanitize_upload_filename(
                        filename, "request-123"
                    ),
                    expected,
                )


    def test_sanitize_upload_filename_rejects_empty_name(self):
        """空文件名和目录占位符不能进入服务端存储路径。"""
        for filename in ("", ".", "..", "/"):
            with self.subTest(filename=filename):
                with self.assertRaises(HttpException) as raised:
                    video_controller._sanitize_upload_filename(
                        filename, "request-123"
                    )
                self.assertEqual(raised.exception.status_code, 400)

    def test_resolve_path_maps_missing_and_unsafe_files(self):
        """不存在文件返回 404，目录穿越等非法路径返回 403。"""
        for error, expected_status in (
            ("file does not exist", 404),
            ("path escapes base directory", 403),
        ):
            with self.subTest(error=error):
                with patch.object(
                    video_controller.file_security,
                    "resolve_path_within_directory",
                    side_effect=ValueError(error),
                ):
                    with self.assertRaises(HttpException) as raised:
                        video_controller._resolve_path_within_directory(
                            "/tasks", "../secret", "request-123"
                        )
                self.assertEqual(raised.exception.status_code, expected_status)

    def test_parse_byte_range_supports_common_player_requests(self):
        """播放器常见的闭区间、开放区间和后缀区间都应得到准确边界。"""
        cases = (
            (None, (0, 9)),
            ("bytes=2-5", (2, 5)),
            ("bytes=4-", (4, 9)),
            ("bytes=-4", (6, 9)),
            ("bytes=2-50", (2, 9)),
        )
        for header, expected in cases:
            with self.subTest(header=header):
                self.assertEqual(
                    video_controller._parse_byte_range(
                        header, 10, "request-123"
                    ),
                    expected,
                )

    def test_parse_byte_range_rejects_malformed_or_out_of_bounds_requests(self):
        """非法 Range 必须返回 416，不能因 split 或 int 转换异常变成 500。"""
        invalid_headers = (
            "items=0-1",
            "bytes=",
            "bytes=10-",
            "bytes=5-2",
            "bytes=0-1,3-4",
        )
        for header in invalid_headers:
            with self.subTest(header=header):
                with self.assertRaises(HttpException) as raised:
                    video_controller._parse_byte_range(
                        header, 10, "request-123"
                    )
                self.assertEqual(raised.exception.status_code, 416)


class TestVideoControllerTasks(unittest.TestCase):
    @staticmethod
    def _request():
        return SimpleNamespace(headers={"x-task-id": "request-123"})

    def test_create_task_queues_requested_pipeline_stage(self):
        """创建任务应持久化初始状态，并把原请求模型与停止阶段交给队列。"""
        body = MagicMock()
        body.model_dump.return_value = {"video_subject": "Coffee"}

        with (
            patch.object(video_controller.utils, "get_uuid", return_value="task-123"),
            patch.object(video_controller.sm.state, "update_task") as update_task,
            patch.object(video_controller.task_manager, "add_task") as add_task,
        ):
            response = video_controller.create_task(
                self._request(), body, stop_at="audio"
            )

        self.assertEqual(response["status"], 200)
        self.assertEqual(response["data"]["task_id"], "task-123")
        self.assertEqual(response["data"]["request_id"], "request-123")
        update_task.assert_called_once_with("task-123")
        add_task.assert_called_once_with(
            video_controller.tm.start,
            task_id="task-123",
            params=body,
            stop_at="audio",
        )

    def test_create_task_removes_state_when_queue_is_full(self):
        """队列已满时必须回滚刚创建的状态，并向调用方返回 429。"""
        body = MagicMock()
        body.model_dump.return_value = {"video_subject": "Coffee"}

        with (
            patch.object(video_controller.utils, "get_uuid", return_value="task-123"),
            patch.object(video_controller.sm.state, "update_task"),
            patch.object(
                video_controller.task_manager,
                "add_task",
                side_effect=TaskQueueFullError("queue full"),
            ),
            patch.object(video_controller.sm.state, "delete_task") as delete_task,
        ):
            with self.assertRaises(HttpException) as raised:
                video_controller.create_task(
                    self._request(), body, stop_at="video"
                )

        self.assertEqual(raised.exception.status_code, 429)
        delete_task.assert_called_once_with("task-123")



    def test_task_query_preserves_structured_failure_details(self):
        """失败阶段和错误信息必须通过任务查询接口原样返回。"""
        failed_task = {
            "task_id": "failed-task",
            "state": const.TASK_STATE_FAILED,
            "progress": 30,
            "failed_stage": "audio",
            "error": "TTS request timed out",
        }

        with patch.object(
            video_controller.sm.state,
            "get_task",
            return_value=failed_task,
        ):
            response = video_controller.get_task(
                self._request(), task_id="failed-task", query=MagicMock()
            )

        self.assertEqual(response["data"], failed_task)




    def test_get_and_delete_missing_task_return_404(self):
        """查询或删除未知任务都应返回一致的 404，而不是空成功响应。"""
        with patch.object(video_controller.sm.state, "get_task", return_value=None):
            for operation in (
                lambda: video_controller.get_task(
                    self._request(), task_id="missing", query=MagicMock()
                ),
                lambda: video_controller.delete_video(
                    self._request(), task_id="missing"
                ),
            ):
                with self.subTest(operation=operation):
                    with self.assertRaises(HttpException) as raised:
                        operation()
                    self.assertEqual(raised.exception.status_code, 404)


class TestVideoControllerFiles(unittest.TestCase):
    @staticmethod
    def _request(range_header=None):
        headers = {"x-task-id": "request-123"}
        if range_header is not None:
            headers["Range"] = range_header
        return SimpleNamespace(headers=headers)

    def test_upload_video_material_validates_complete_extension(self):
        """大写合法扩展名应接受，无点号伪扩展名应拒绝。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            upload = SimpleNamespace(
                filename=r"C:\videos\clip.MOV",
                file=BytesIO(b"video"),
            )
            with patch.object(
                video_controller.utils,
                "storage_dir",
                return_value=temp_dir,
            ):
                response = video_controller.upload_video_material_file(
                    self._request(), upload
                )

            self.assertEqual(response["data"]["file"], "clip.MOV")
            self.assertEqual(Path(temp_dir, "clip.MOV").read_bytes(), b"video")

            invalid_upload = SimpleNamespace(
                filename="photojpg",
                file=BytesIO(b"not-an-image"),
            )
            with self.assertRaises(HttpException) as raised:
                video_controller.upload_video_material_file(
                    self._request(), invalid_upload
                )
            self.assertEqual(raised.exception.status_code, 400)

    def test_stream_video_returns_requested_bytes(self):
        """Range 响应的正文和 Content-Range 必须与计算出的区间一致。"""

        async def consume(response):
            return b"".join([chunk async for chunk in response.body_iterator])

        with tempfile.TemporaryDirectory() as temp_dir:
            Path(temp_dir, "clip.mp4").write_bytes(b"0123456789")
            with patch.object(
                video_controller.utils,
                "task_dir",
                return_value=temp_dir,
            ):
                response = asyncio.run(
                    video_controller.stream_video(
                        self._request("bytes=2-5"), "clip.mp4"
                    )
                )
                body = asyncio.run(consume(response))

        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.headers["content-range"], "bytes 2-5/10")
        self.assertEqual(response.headers["content-length"], "4")
        self.assertEqual(body, b"2345")

    def test_download_video_uses_resolved_file(self):
        """下载响应应使用白名单目录解析后的真实路径和原始文件名。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            video_path = Path(temp_dir, "final-1.mp4")
            video_path.write_bytes(b"video")
            with patch.object(
                video_controller.utils,
                "task_dir",
                return_value=temp_dir,
            ):
                response = asyncio.run(
                    video_controller.download_video(
                        self._request(), "final-1.mp4"
                    )
                )

        # macOS 的 /var 是 /private/var 符号链接，安全解析会返回真实路径。
        self.assertEqual(response.path, os.path.realpath(video_path))
        self.assertEqual(response.filename, "final-1.mp4")
        self.assertEqual(response.media_type, "video/mp4")


if __name__ == "__main__":
    unittest.main()
