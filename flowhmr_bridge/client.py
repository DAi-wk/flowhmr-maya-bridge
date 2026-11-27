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
DEFAULT_PORT = 60550      # FlowHMR.exe / start-web.cmd
WEB_DEFAULT_PORT = 8080   # app.py started by hand
FINISHED, ACTIVE = "done", ("queued", "running", "waiting_user", "cancel_requested")
# Loopback only: never route through a system or environment proxy.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class FlowHMRError(RuntimeError):
    """FlowHMR is unreachable or answered with an error."""


def discover_ports(root=None):
    """Ports FlowHMR may be serving on, most likely first:
    the desktop app's record (.cache/desktop-port.txt), the web server's record
    (logs/web-process.json, written by start-web.cmd -ServerOnly), the launcher default, and
    app.py's own default when the web server is started by hand."""
    root = Path(root or DEFAULT_ROOT)
    ports = []
    try:
        ports.append(int((root / ".cache" / "desktop-port.txt").read_text(encoding="ascii").strip()))
    except (OSError, ValueError):
        pass
    try:  # PowerShell writes it with a byte-order mark
        ports.append(int(json.loads((root / "logs" / "web-process.json").read_text(encoding="utf-8-sig"))["port"]))
    except (OSError, ValueError, KeyError, TypeError):
        pass
    ports += [DEFAULT_PORT, WEB_DEFAULT_PORT]
    return [port for port in dict.fromkeys(ports) if 0 < port < 65536]


def discover_port(root=None):
    """The most likely port (see discover_ports)."""
    return discover_ports(root)[0]


class FlowHMRClient:
    def __init__(self, root=None, port=None, timeout=2.0):
        """``port`` fixes the port (a web server started on its own port, say); without it the
        client looks for FlowHMR on the ports discover_ports() lists (see locate)."""
        self.root = Path(root or DEFAULT_ROOT)
        self.fixed_port = int(port) if port else None
        self.port = self.fixed_port or discover_port(self.root)
        self.timeout = timeout

    def locate(self, timeout=0.6):
        """Without a fixed port, switch to the first listed port where FlowHMR answers.
        Returns True when one answered (the port stays as it was otherwise)."""
        if self.fixed_port:
            return False
        current = self.port
        for port in dict.fromkeys([current] + discover_ports(self.root)):
            self.port = port
            try:
                self._info(timeout)
                return True
            except FlowHMRError:
                continue
        self.port = current
        return False

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.port}"

    def _response(self, path, timeout=None, method="GET"):
        request = urllib.request.Request(self.base_url + path, method=method)
        try:
            with _OPENER.open(request, timeout=timeout or self.timeout) as response:
                return response.headers, response.read()
        except urllib.error.HTTPError as error:
            detail = ""
            try:
                detail = json.loads(error.read()).get("error", "")
            except (ValueError, AttributeError, OSError):
                pass
            raise FlowHMRError(f"FlowHMR HTTP {error.code} for {path}" + (f": {detail}" if detail else "")) from None
        except (urllib.error.URLError, OSError, ValueError) as error:
            raise FlowHMRError(f"FlowHMR is not reachable at {self.base_url} ({error})") from None

    def _get(self, path, timeout=None):
        return self._response(path, timeout)[1]

    def _json(self, path, timeout=None):
        try:
            return json.loads(self._get(path, timeout))
        except ValueError:
            raise FlowHMRError(f"FlowHMR returned invalid JSON for {path}") from None

    def info(self):
        """Server and runtime summary; raises FlowHMRError when FlowHMR is not running."""
        return self._info()

    def _info(self, timeout=None):
        data = self._json("/api/info", timeout)
        if not isinstance(data, dict) or "fps" not in data:
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

    def preview_source(self, job_id, use_character=True):
        """The same SMPL-H stream and saved single-actor assignment as the character viewer."""
        job_id = _safe_id(job_id)
        headers, data = self._response(f"/api/jobs/{job_id}/pmx-motion", timeout=120)
        try:
            meta = json.loads(headers["X-FlowHMR-PMX-Motion"])
            frames = meta["frames"]
            if type(frames) is not int or frames < 1:
                raise ValueError
            floats = 156 + frames * (156 + 208 + 4 + (4 if meta.get("hasSoles") else 0))
            if len(data) != 4 * floats:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise FlowHMRError("原始動作的標頭、幀數或資料長度不一致。") from None
        result = dict(root=self.root, meta=meta, data=data, model=None, offsets=None)
        if not use_character:
            return result
        catalog = self._json(f"/api/character-library?actor={job_id}")
        actor = catalog["actors"].get(job_id)
        saved = catalog["defaults"].get("single", {}) if actor is None else actor
        identifier = saved.get("id")
        if identifier:
            model = next((m for m in catalog["models"] if m["id"] == identifier), None)
            if not model or model["kind"] != "fbx":
                raise FlowHMRError("這個任務指定的預覽角色不是 FBX，無法用在 Maya rig。請選用同一個 rig 的蒙皮角色，"
                                   "或取消「使用 HMR 已儲存的角色設定」。")
            # Opening a library model only issues a local read ticket. It does not change the assignment.
            path = f"/api/character-library/{urllib.parse.quote(identifier, safe='')}/open"
            ticket = json.loads(self._response(path, method="POST")[1])["ticket"]
            offsets = actor.get("poses", {}).get(identifier) if actor is not None else saved.get("offsets")
            result.update(model=self._get("/api/character-resource/" + urllib.parse.quote(ticket, safe=""), timeout=120),
                          model_name=model["filename"], offsets=offsets)
        return result

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
