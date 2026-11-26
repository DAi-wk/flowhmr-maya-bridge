"""Client tests against the running FlowHMR desktop app (skipped when it is not running).

    python -I tests/test_client.py -v
"""
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from flowhmr_bridge.client import FlowHMRClient, FlowHMRError, discover_port, summarize  # noqa: E402


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class OfflineTests(unittest.TestCase):
    def test_raw_unassigned_actor_overrides_default_character(self):
        client = FlowHMRClient()
        header = {"X-FlowHMR-PMX-Motion": '{"frames":1,"hasSoles":false}'}
        stream = bytes((156 + 156 + 208 + 4) * 4)
        catalog = {"actors": {"clip": {"id": None}}, "defaults": {"single": {"id": "other"}}}
        with patch.object(client, "_response", return_value=(header, stream)) as response, \
             patch.object(client, "_json", return_value=catalog):
            source = client.preview_source("clip")
        self.assertIsNone(source["model"])
        self.assertEqual(source["data"], stream)
        response.assert_called_once_with("/api/jobs/clip/pmx-motion", timeout=120)

    def test_raw_incomplete_frame_fails_before_character_lookup(self):
        client = FlowHMRClient()
        with patch.object(client, "_response", return_value=({"X-FlowHMR-PMX-Motion": '{"frames":1}'}, b"")), \
             patch.object(client, "_json") as catalog:
            with self.assertRaises(FlowHMRError):
                client.preview_source("clip")
        catalog.assert_not_called()

    def test_raw_scene_target_only_requests_motion(self):
        client = FlowHMRClient()
        header = {"X-FlowHMR-PMX-Motion": '{"frames":1,"hasSoles":true}'}
        stream = bytes((156 + 156 + 208 + 8) * 4)
        with patch.object(client, "_response", return_value=(header, stream)) as response, \
             patch.object(client, "_json") as catalog:
            source = client.preview_source("clip", use_character=False)
        self.assertIsNone(source["model"])
        catalog.assert_not_called()
        response.assert_called_once_with("/api/jobs/clip/pmx-motion", timeout=120)

    def test_wrong_port_raises_flowhmr_error(self):
        client = FlowHMRClient(port=_free_port(), timeout=0.5)
        with self.assertRaises(FlowHMRError):
            client.info()

    def test_missing_port_file_falls_back(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(discover_port(folder), 60550)

    def test_bad_job_id_is_refused(self):
        with self.assertRaises(FlowHMRError):
            FlowHMRClient(port=1).job("../etc")

    def test_scene_jobs_are_not_usable(self):
        self.assertFalse(summarize({"id": "a", "status": "done", "person_count": 2})["usable"])
        self.assertTrue(summarize({"id": "a", "status": "done"})["usable"])
        self.assertFalse(summarize({"id": "a", "status": "running"})["usable"])


class LiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = FlowHMRClient()
        try:
            cls.client.info()
        except FlowHMRError as error:
            raise unittest.SkipTest(str(error))

    def test_jobs_and_fbx(self):
        jobs = self.client.jobs()
        usable = [job for job in jobs if job["usable"]]
        if not usable:
            self.skipTest("no finished single-person job")
        job = usable[0]
        self.assertEqual(self.client.job(job["id"])["status"], "done")
        self.assertTrue((self.client.thumbnail(job["id"]) or b"").startswith(b"\xff\xd8"))
        with tempfile.TemporaryDirectory() as folder:
            path = self.client.download_fbx(job["id"], Path(folder) / "clip")
            self.assertEqual([p.name for p in path.parent.iterdir()], [path.name])
            self.assertGreater(path.stat().st_size, 10_000)

    def test_unknown_job_is_http_error(self):
        with self.assertRaises(FlowHMRError):
            self.client.job("does_not_exist_0")


if __name__ == "__main__":
    unittest.main()
