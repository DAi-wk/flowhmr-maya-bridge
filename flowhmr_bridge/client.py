"""Read-only client for the FlowHMR desktop app's local HTTP API.

No Maya imports, so it runs under any Python 3.7+ (including FlowHMR's own
runtime/python.exe for tests). Only loopback addresses are used.
"""
from __future__ import annotations

import json
import os
import shutil
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path



def _default_root():
    """FLOWHMR_ROOT if set, else a "FlowHMR" folder beside this repository.

    Only used to find .cache/desktop-port.txt and FlowHMR-Version.txt; without it the
    client uses the launcher's default port. The panel can also point at another folder.
    """
    env = os.environ.get("FLOWHMR_ROOT")
    return Path(env) if env else Path(__file__).resolve().parents[2] / "FlowHMR"


DEFAULT_ROOT = _default_root()
DEFAULT_PORT = 60550
FINISHED, ACTIVE = "done", ("queued", "running", "waiting_user", "cancel_requested")
# Loopback only: never route through a system or environment proxy.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class FlowHMRError(RuntimeError):
    """FlowHMR is unreachable or answered with an error."""


def discover_port(root=None):
    """FlowHMR writes the desktop port here; fall back to the launcher default."""
    path = Path(root or DEFAULT_ROOT) / ".cache" / "desktop-port.txt"
    try:
        port = int(path.read_text(encoding="ascii").strip())
        return port if 0 < port < 65536 else DEFAULT_PORT
    except (OSError, ValueError):
        return DEFAULT_PORT


class FlowHMRClient:
    def __init__(self, root=None, port=None, timeout=2.0):
        self.root = Path(root or DEFAULT_ROOT)
        self.port = int(port or discover_port(self.root))
        self.timeout = timeout

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.port}"

    def _get(self, path, timeout=None):
        request = urllib.request.Request(self.base_url + path, headers={"Accept": "application/json"})
        try:
            with _OPENER.open(request, timeout=timeout or self.timeout) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            detail = ""
            try:
                detail = json.loads(error.read()).get("error", "")
            except (ValueError, AttributeError, OSError):
                pass
            raise FlowHMRError(f"FlowHMR HTTP {error.code} for {path}" + (f": {detail}" if detail else "")) from None
        except (urllib.error.URLError, OSError, ValueError) as error:
            raise FlowHMRError(f"FlowHMR is not reachable at {self.base_url} ({error})") from None

    def _json(self, path, timeout=None):
        try:
            return json.loads(self._get(path, timeout))
        except ValueError:
            raise FlowHMRError(f"FlowHMR returned invalid JSON for {path}") from None

    def info(self):
        """Server and runtime summary; raises FlowHMRError when FlowHMR is not running."""
        data = self._json("/api/info")
        if "fps" not in data:
            raise FlowHMRError(f"{self.base_url} does not look like FlowHMR")
        return data

    def version(self):
        try:
            return (self.root / "FlowHMR-Version.txt").read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def jobs(self, limit=50):
        """Newest first, normalised to the fields the bridge uses."""
        rows = self._json(f"/api/jobs?limit={int(limit)}").get("jobs", [])
        return [summarize(row) for row in rows]

    def job(self, job_id):
        return self._json(f"/api/jobs/{_safe_id(job_id)}")

    def thumbnail(self, job_id):
        try:
            return self._get(f"/api/jobs/{_safe_id(job_id)}/thumbnail.jpg", timeout=5)
        except FlowHMRError:
            return None

    def download_fbx(self, job_id, folder, profile="maya_adv", timeout=600, **params):
        """Write the job's FBX as the only file in ``folder`` (ADV lists the folder).

        Extra query parameters pass through, e.g. ``mesh="0", first_pose="t"`` for the
        ``flowhmr`` profile (``maya_adv`` always has the T pose at frame -1).
        """
        folder = Path(folder)
        if folder.exists():
            shutil.rmtree(folder)
        folder.mkdir(parents=True)
        path = folder / f"{_safe_id(job_id)}_{profile}.fbx"
        query = urllib.parse.urlencode({"profile": profile, **{k: str(v) for k, v in params.items()}})
        data = self._get(f"/api/jobs/{_safe_id(job_id)}/fbx?{query}", timeout=timeout)
        if not data.startswith(b"Kaydara FBX Binary"):
            raise FlowHMRError("FlowHMR did not return a binary FBX")
        path.write_bytes(data)
        return path


def summarize(row):
    scene = row.get("person_count") is not None or row.get("scene_state") is not None
    return {
        "id": row.get("id"),
        "filename": row.get("filename") or "",
        "status": row.get("status") or "",
        "progress": row.get("progress"),
        "message": row.get("message") or "",
        "frames": row.get("num_frames"),
        "created": row.get("created"),
        "finished": row.get("finished"),
        "scene": scene,
        "usable": row.get("status") == FINISHED and not scene,
    }


def _safe_id(job_id):
    text = str(job_id)
    if not text or not all(c.isalnum() or c in "_-" for c in text) or len(text) > 64:
        raise FlowHMRError(f"Invalid FlowHMR job id: {job_id!r}")
    return text
