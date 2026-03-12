import json
import os
import urllib.error
import urllib.parse
import urllib.request
import unittest
from uuid import uuid4


BASE_URL = os.getenv("MIM_TEST_BASE_URL", "http://127.0.0.1:8001")


def post_json(path: str, payload: dict) -> tuple[int, dict]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, json.loads(body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        parsed = json.loads(body) if body else {}
        return exc.code, parsed


def get_json(path: str, query: dict | None = None) -> tuple[int, dict | list]:
    url = f"{BASE_URL}{path}"
    if query:
        url = f"{url}?{urllib.parse.urlencode(query)}"
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, json.loads(body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        parsed = json.loads(body) if body else {}
        return exc.code, parsed


class Objective61LivePerceptionAdaptersTest(unittest.TestCase):
    def test_objective61_live_perception_adapters(self) -> None:
        run_id = uuid4().hex[:8]
        camera_device = f"cam-obj61-{run_id}"
        mic_device = f"mic-obj61-{run_id}"

        status, camera = post_json(
            "/gateway/perception/camera/events",
            {
                "device_id": camera_device,
                "source_type": "camera",
                "session_id": f"session-{run_id}",
                "is_remote": False,
                "observations": [
                    {
                        "object_label": f"tool-obj61-{run_id}",
                        "confidence": 0.93,
                        "zone": f"front-left-obj61-{run_id}",
                    }
                ],
                "min_interval_seconds": 2,
                "duplicate_window_seconds": 30,
                "observation_confidence_floor": 0.5,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, camera)
        self.assertEqual(str(camera.get("status", "")), "accepted")
        event = camera.get("event", {}) if isinstance(camera, dict) else {}
        self.assertEqual(str(event.get("source", "")), "vision")

        status, mic = post_json(
            "/gateway/perception/mic/events",
            {
                "device_id": mic_device,
                "source_type": "microphone",
                "session_id": f"session-{run_id}",
                "is_remote": True,
                "transcript": "run workspace check",
                "confidence": 0.88,
                "min_interval_seconds": 1,
                "duplicate_window_seconds": 30,
                "transcript_confidence_floor": 0.45,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, mic)
        self.assertEqual(str(mic.get("status", "")), "accepted")
        mic_event = mic.get("event", {}) if isinstance(mic, dict) else {}
        self.assertEqual(str(mic_event.get("source", "")), "voice")

        status, dup = post_json(
            "/gateway/perception/camera/events",
            {
                "device_id": camera_device,
                "source_type": "camera",
                "session_id": f"session-{run_id}",
                "is_remote": False,
                "observations": [
                    {
                        "object_label": f"tool-obj61-{run_id}",
                        "confidence": 0.93,
                        "zone": f"front-left-obj61-{run_id}",
                    }
                ],
                "min_interval_seconds": 2,
                "duplicate_window_seconds": 30,
                "observation_confidence_floor": 0.5,
                "metadata_json": {"run_id": run_id, "repeat": True},
            },
        )
        self.assertEqual(status, 200, dup)
        self.assertIn(str(dup.get("status", "")), {"suppressed_duplicate", "throttled_interval"})

        status, low = post_json(
            "/gateway/perception/mic/events",
            {
                "device_id": mic_device,
                "source_type": "microphone",
                "session_id": f"session-{run_id}",
                "is_remote": True,
                "transcript": "mumble",
                "confidence": 0.18,
                "min_interval_seconds": 1,
                "duplicate_window_seconds": 30,
                "transcript_confidence_floor": 0.45,
                "discard_low_confidence": True,
                "metadata_json": {"run_id": run_id, "low": True},
            },
        )
        self.assertEqual(status, 200, low)
        self.assertEqual(str(low.get("status", "")), "discarded_low_confidence")

        status, sources = get_json("/gateway/perception/sources", {"active_only": True, "limit": 100})
        self.assertEqual(status, 200, sources)
        source_rows = sources.get("sources", []) if isinstance(sources, dict) else []
        self.assertTrue(any(str(item.get("device_id", "")) == camera_device for item in source_rows if isinstance(item, dict)))
        self.assertTrue(any(str(item.get("device_id", "")) == mic_device for item in source_rows if isinstance(item, dict)))

        status, perception_status = get_json("/gateway/perception/status")
        self.assertEqual(status, 200, perception_status)
        self.assertIn("active_perception_adapters", perception_status)
        self.assertIn("camera_source_status", perception_status)
        self.assertIn("mic_source_status", perception_status)
        self.assertIn("adapter_health", perception_status)
        self.assertTrue(bool((perception_status.get("camera_source_status", {}) if isinstance(perception_status, dict) else {}).get("last_event")))
        self.assertTrue(bool((perception_status.get("mic_source_status", {}) if isinstance(perception_status, dict) else {}).get("last_transcript")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
