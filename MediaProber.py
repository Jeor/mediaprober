from __future__ import annotations

import html
import hashlib
import json
import os
import platform
import plistlib
import queue
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import hdr_st2094
import report_privacy
import diagnostic_report


APP_NAME = "Media Prober"
APP_SLUG = "media-prober"


def runtime_root() -> Path:
    if not getattr(sys, "frozen", False):
        return Path(__file__).resolve().parent
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    if sys.platform.startswith("linux"):
        return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / APP_SLUG
    if sys.platform.startswith("win"):
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / APP_NAME
    return Path(sys.executable).resolve().parent


APP_ROOT = runtime_root()
TOOLS_DIR = APP_ROOT / "tools"
LOG_DIR = APP_ROOT / "logs"
REPORT_DIR = APP_ROOT / "reports"
LOG_DIR.mkdir(parents=True, exist_ok=True)
TOOLS_DIR.mkdir(parents=True, exist_ok=True)
REPORT_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class ToolSpec:
    name: str
    executable: str
    required: bool
    purpose: str
    winget_id: str = ""
    brew_formula: str = ""
    apt_package: str = ""
    github_repo: str = ""


def is_windows() -> bool:
    return sys.platform.startswith("win")


def is_macos() -> bool:
    return sys.platform == "darwin"


def is_linux() -> bool:
    return sys.platform.startswith("linux")


def tool_executable(name: str) -> str:
    return f"{name}.exe" if is_windows() else name


TOOL_SPECS = [
    ToolSpec("ffprobe", tool_executable("ffprobe"), True, "Required probe engine from FFmpeg.", "Gyan.FFmpeg", "ffmpeg", "ffmpeg"),
    ToolSpec("mediainfo", tool_executable("mediainfo"), False, "Recommended for clearer HDR and Dolby Vision labels.", "", "media-info", "mediainfo"),
    ToolSpec("dovi_tool", tool_executable("dovi_tool"), False, "Optional Dolby Vision RPU workflows.", github_repo="quietvoid/dovi_tool"),
    ToolSpec("hdr10plus_tool", tool_executable("hdr10plus_tool"), False, "Optional HDR10+ metadata workflows.", github_repo="quietvoid/hdr10plus_tool"),
    ToolSpec("ffmpeg", tool_executable("ffmpeg"), False, "Required for sampled ST 2094-10 HEVC SEI analysis.", "Gyan.FFmpeg", "ffmpeg", "ffmpeg"),
]


class Logger:
    def __init__(self) -> None:
        self.path = LOG_DIR / f"MediaProber-{datetime.now():%Y%m%d-%H%M%S}.log"
        self.lock = threading.Lock()

    def write(self, message: str, level: str = "INFO") -> None:
        line = f"[{datetime.now():%Y-%m-%d %H:%M:%S.%f}"[:-3] + f"] [{level}] {message}"
        with self.lock:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")


LOGGER = Logger()


def now() -> str:
    return datetime.now().strftime("%H:%M:%S")


def is_url(value: object) -> bool:
    parsed = urllib.parse.urlparse(str(value))
    return parsed.scheme.lower() in {"http", "https", "ftp", "sftp"}


def media_display_name(source: object) -> str:
    text = str(source)
    if not is_url(text):
        return Path(text).name
    parsed = urllib.parse.urlparse(text)
    name = Path(urllib.parse.unquote(parsed.path)).name
    if len(name.encode("utf-8")) > 120:
        return "remote-media-" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    return name or "remote-media"


def report_path_for_source(source: object) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    safe_name = re.sub(r'[\\/:*?"<>|\[\]\x00-\x1f\x7f]', "", Path(media_display_name(source)).stem).strip(" .") or "media"
    if len(safe_name.encode("utf-8")) > 80:
        digest = hashlib.sha256(str(source).encode("utf-8")).hexdigest()[:12]
        safe_name = safe_name.encode("utf-8")[:80].decode("utf-8", errors="ignore").rstrip(" .") + "-" + digest
    # Media may live on a read-only mount or network share.
    safe_name = report_privacy.media_label(safe_name)
    safe_name = re.sub(r'[\\/:*?"<>|\[\]]', "", safe_name)
    return REPORT_DIR / f"{safe_name} - Media Prober Report {stamp}.md"


def emit_event(emit, message: str = "", level: str = "INFO", progress: int | None = None) -> None:
    payload = {"message": message, "level": level}
    if progress is not None:
        payload["progress"] = max(0, min(100, int(progress)))
    emit(payload)


def quote_command(args: list[str]) -> str:
    return subprocess.list2cmdline(args) if is_windows() else shlex.join(args)


def creation_flags() -> int:
    return subprocess.CREATE_NO_WINDOW if is_windows() else 0


def open_path(path: Path) -> None:
    if is_windows():
        os.startfile(path)
    elif is_macos():
        subprocess.Popen(["open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def reveal_path(path: Path) -> None:
    if is_windows():
        subprocess.Popen(["explorer.exe", "/select,", str(path)])
    elif is_macos():
        subprocess.Popen(["open", "-R", str(path)])
    else:
        open_path(path.parent)


def install_hint(spec: ToolSpec) -> str:
    if is_windows() and spec.name == "mediainfo":
        return "Install the MediaInfo CLI (not the GUI) and place mediainfo.exe in the tools folder."
    if is_windows() and spec.winget_id:
        return f"winget install --id {spec.winget_id} --exact"
    if is_macos() and spec.brew_formula:
        return f"brew install {spec.brew_formula}"
    if is_linux() and spec.apt_package:
        if shutil.which("dnf"):
            package = "ffmpeg-free" if spec.name in {"ffprobe", "ffmpeg"} else spec.apt_package
            return f"sudo dnf install {package}"
        return f"sudo apt install {spec.apt_package}"
    if spec.github_repo:
        return f"latest GitHub release from {spec.github_repo} into {TOOLS_DIR}"
    return "install and put the executable on PATH"


def local_tool_path(executable: str) -> Path | None:
    folders = [APP_ROOT, TOOLS_DIR, APP_ROOT / "bin"]
    if getattr(sys, "frozen", False):
        folders.append(Path(sys.executable).resolve().parent)
    if is_macos():
        # Finder-launched apps do not inherit a terminal's Homebrew PATH.
        folders.extend([Path("/opt/homebrew/bin"), Path("/usr/local/bin")])
    if is_windows():
        local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        folders.append(local / "Microsoft" / "WinGet" / "Links")
        folders.append(Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "MediaInfo")
    for folder in folders:
        candidate = folder / executable
        if candidate.is_file() and (is_windows() or os.access(candidate, os.X_OK)):
            return candidate
    found = shutil.which(executable) or shutil.which(Path(executable).stem)
    return Path(found) if found else None


def tool_is_runnable(path: Path, spec: ToolSpec) -> bool:
    if not path:
        return False
    try:
        result = subprocess.run(
            [str(path), "-version" if spec.name in {"ffprobe", "ffmpeg"} else "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            creationflags=creation_flags(),
        )
        return result.returncode == 0
    except Exception:
        return False


def resolve_tool(spec: ToolSpec) -> dict:
    path = local_tool_path(spec.executable)
    present = bool(path)
    runnable = bool(path and tool_is_runnable(path, spec))
    return {
        "spec": spec,
        "path": path,
        "present": present,
        "found": runnable,
        "status": "Found"
        if runnable
        else ("Present but not runnable" if present else ("Missing - required" if spec.required else "Missing - optional")),
    }


def get_inventory() -> list[dict]:
    return [resolve_tool(spec) for spec in TOOL_SPECS]


def required_tools_ok() -> bool:
    return all(item["found"] for item in get_inventory() if item["spec"].required)


def format_inventory(show_paths: bool = True) -> str:
    lines = ["Tool check:", ""]
    for item in get_inventory():
        spec = item["spec"]
        lines.append(f"{spec.name}: {item['status']}")
        if show_paths and item["path"]:
            lines.append(f"  Path: {item['path']}")
        lines.append(f"  Use: {spec.purpose}")
        if not item["found"]:
            lines.append(f"  Install: {install_hint(spec)}")
        lines.append("")
    return "\n".join(lines)


def tool_log_level(item: dict) -> str:
    if item["found"]:
        return "SUCCESS"
    return "ERROR" if item["spec"].required else "WARN"


def tool_status_line(item: dict) -> str:
    spec = item["spec"]
    install = "" if item["found"] else f" Install: {install_hint(spec)}"
    return f"{spec.name}: {item['status']} - {spec.purpose}{install}"


class Runner:
    def __init__(self, emit):
        self.emit = emit

    def log(self, message: str, level: str = "INFO") -> None:
        LOGGER.write(message, level)
        emit_event(self.emit, message, level)

    def run(self, args: list[str], timeout: int | None = None) -> subprocess.CompletedProcess:
        self.log(f"Starting: {quote_command(args)}")
        try:
            proc = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=creation_flags(),
            )
            stdout, stderr = proc.communicate(timeout=timeout)
            LOGGER.write(f"Finished: {args[0]} exit code {proc.returncode}")
            if stdout.strip():
                LOGGER.write(f"STDOUT {args[0]}:\n{stdout.strip()}")
            if stderr.strip():
                LOGGER.write(f"STDERR {args[0]}:\n{stderr.strip()}", "WARN")
            return subprocess.CompletedProcess(args, proc.returncode, stdout, stderr)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
            LOGGER.write(f"Timed out: {quote_command(args)}", "ERROR")
            return subprocess.CompletedProcess(args, 124, stdout, stderr)


def safe_get(obj, *keys, default=""):
    current = obj
    for key in keys:
        if not isinstance(current, dict) or key not in current or current[key] in (None, ""):
            return default
        current = current[key]
    return current


def parse_json(text: str) -> dict:
    try:
        return json.loads(text) if text.strip() else {}
    except json.JSONDecodeError:
        return {}


def platform_tokens() -> list[str]:
    if is_windows():
        return ["windows", "win64", "win-x64", "pc-windows", "msvc", ".exe"]
    if is_macos():
        return ["macos", "darwin", "apple", "universal"]
    if is_linux():
        return ["linux", "unknown-linux", "musl", "gnu"]
    return [sys.platform]


def architecture_tokens() -> tuple[list[str], list[str]]:
    machine = os.environ.get("PROCESSOR_ARCHITECTURE", platform.machine()) if is_windows() else platform.machine()
    machine = f"{machine} {os.environ.get('PROCESSOR_ARCHITEW6432', '')}".lower()
    if "arm64" in machine or "aarch64" in machine:
        return ["aarch64", "arm64", "universal"], ["x86_64", "x64", "amd64"]
    return ["x86_64", "x64", "amd64", "universal"], ["aarch64", "arm64"]


def supported_archive(name: str) -> bool:
    lowered = name.lower()
    return lowered.endswith((".zip", ".tar.gz", ".tgz", ".exe"))


def select_release_asset(assets: list[dict], executable: str) -> dict | None:
    tool_base = Path(executable).stem.lower()
    os_tokens = platform_tokens()
    candidates = [
        a for a in assets
        if a.get("browser_download_url")
        and tool_base in a.get("name", "").lower()
        and supported_archive(a.get("name", ""))
        and any(token in a.get("name", "").lower() for token in os_tokens)
    ]
    if not candidates:
        return None
    preferred_arch, avoided_arch = architecture_tokens()
    # A wrong-architecture executable is not a usable fallback.
    candidates = [a for a in candidates if
                  any(t in a["name"].lower() for t in preferred_arch)
                  and ("universal" in a["name"].lower() or
                       not any(t in a["name"].lower() for t in avoided_arch))]
    if not candidates:
        return None

    def score(asset: dict) -> tuple[int, int, int, str]:
        name = asset.get("name", "").lower()
        arch_score = 0 if any(token in name for token in preferred_arch) else 1
        if any(token in name for token in avoided_arch):
            arch_score += 3
        os_score = 0 if any(token in name for token in os_tokens[:2]) else 1
        ext_score = 0 if name.endswith(".zip") else (1 if name.endswith((".tar.gz", ".tgz")) else 2)
        return arch_score, os_score, ext_score, name

    return sorted(candidates, key=score)[0]


def github_latest_asset(repo: str, executable: str) -> tuple[str, str]:
    api_url = f"https://api.github.com/repos/{repo}/releases/latest"
    req = urllib.request.Request(api_url, headers={"User-Agent": APP_NAME, "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            release = json.loads(response.read().decode("utf-8"))
        asset = select_release_asset(release.get("assets", []), executable)
        if asset:
            return asset["name"], asset["browser_download_url"]
    except Exception as exc:
        LOGGER.write(f"GitHub API lookup failed for {repo}: {exc}", "WARN")

    raise RuntimeError("No verified release asset for this operating system and processor. Install the matching tool manually into the tools folder.")


def extract_tool(download_path: Path, extract_dir: Path, executable: str) -> Path | None:
    if download_path.suffix.lower() == ".exe":
        return download_path
    lowered = download_path.name.lower()
    if lowered.endswith(".zip"):
        with zipfile.ZipFile(download_path) as zf:
            zf.extractall(extract_dir)
    elif lowered.endswith((".tar.gz", ".tgz")):
        with tarfile.open(download_path) as tf:
            tf.extractall(extract_dir)
    else:
        return None

    exact = list(extract_dir.rglob(executable))
    if exact:
        return exact[0]
    wanted_stem = Path(executable).stem.lower()
    stem_matches = [p for p in extract_dir.rglob("*") if p.is_file() and p.stem.lower() == wanted_stem]
    if stem_matches:
        return stem_matches[0]
    exe_matches = list(extract_dir.rglob("*.exe")) if is_windows() else []
    return exe_matches[0] if exe_matches else None


def install_package_tool(spec: ToolSpec, runner: Runner) -> bool:
    if is_windows() and spec.winget_id:
        if not shutil.which("winget"):
            runner.log("winget was not found. Install App Installer from Microsoft Store.", "ERROR")
            return False
        runner.log(f"Installing {spec.name} with winget package {spec.winget_id}...")
        result = runner.run([
            "winget", "install", "--id", spec.winget_id, "--exact",
            "--accept-package-agreements", "--accept-source-agreements",
        ])
        runner.log(f"winget finished for {spec.name} with exit code {result.returncode}")
        return result.returncode == 0
    if is_macos() and spec.brew_formula:
        brew = local_tool_path("brew")
        if not brew:
            runner.log("Homebrew was not found. Install Homebrew, then run the tool check again.", "ERROR")
            return False
        runner.log(f"Installing {spec.name} with Homebrew formula {spec.brew_formula}...")
        result = runner.run([str(brew), "install", spec.brew_formula])
        runner.log(f"brew finished for {spec.name} with exit code {result.returncode}")
        return result.returncode == 0
    if is_linux() and spec.apt_package:
        runner.log(f"Automatic Linux package installs are not run from the GUI. Install with: {install_hint(spec)}", "WARN")
        return False
    return False


def install_missing_tools(emit) -> None:
    runner = Runner(emit)
    for item in get_inventory():
        spec: ToolSpec = item["spec"]
        if item["found"]:
            continue
        if not spec.github_repo:
            install_package_tool(spec, runner)
        else:
            runner.log(f"Installing {spec.name} from GitHub releases...")
            asset_name, url = github_latest_asset(spec.github_repo, spec.executable)
            runner.log(f"Downloading {asset_name}")
            with tempfile.TemporaryDirectory(prefix="MediaProberTools-") as temp:
                temp_path = Path(temp)
                download_path = temp_path / asset_name
                urllib.request.urlretrieve(url, download_path)
                extract_dir = temp_path / "extract"
                runner.log(f"Extracting {asset_name}")
                exe_path = extract_tool(download_path, extract_dir, spec.executable)
                if not exe_path:
                    raise RuntimeError(f"Could not find {spec.executable} in {asset_name}")
                destination = TOOLS_DIR / spec.executable
                shutil.copy2(exe_path, destination)
                if not is_windows():
                    destination.chmod(destination.stat().st_mode | 0o755)
                runner.log(f"Installed {spec.name} to {destination}")
    emit_event(emit, "Tool check after install:", "INFO")
    for item in get_inventory():
        emit_event(emit, tool_status_line(item), tool_log_level(item))


def relevant_lines(text: str) -> list[str]:
    pattern = re.compile(
        r"Video:|Audio:|Subtitle:|HDR|HDR10\+|Dynamic HDR|Dolby Vision|DOVI|dvhe|dvh1|"
        r"RPU|EL|BL|SMPTE|2094|2086|2084|Mastering|MaxCLL|MaxFALL|Content light|"
        r"bt2020|arib-std-b67|HLG|color|transfer|primaries|matrix",
        re.I,
    )
    return [line for line in text.splitlines() if pattern.search(line)]


def side_data_items(probe: dict, frame_probe: dict) -> list[dict]:
    items = []
    for stream in probe.get("streams", []):
        if stream.get("codec_type") == "video":
            items.extend(stream.get("side_data_list", []) or [])
    for frame in frame_probe.get("frames", []) if frame_probe else []:
        items.extend(frame.get("side_data_list", []) or [])
    return [item for item in items if isinstance(item, dict)]


def side_data_text(item: dict) -> str:
    side_type = item.get("side_data_type", "side data")
    parts = [f"{k}={v}" for k, v in item.items() if k != "side_data_type" and v not in (None, "")]
    return f"{side_type}: {', '.join(parts)}" if parts else side_type


def side_data_fields(item: dict) -> list[tuple[str, object]]:
    return [(key, value) for key, value in item.items() if key != "side_data_type" and value not in (None, "")]


def format_duration(seconds) -> str:
    try:
        total = float(seconds)
    except (TypeError, ValueError):
        return ""
    whole = int(total)
    millis = int(round((total - whole) * 1000))
    hours = whole // 3600
    minutes = (whole % 3600) // 60
    secs = whole % 60
    if millis:
        return f"{hours}:{minutes:02d}:{secs:02d}.{millis:03d}"
    return f"{hours}:{minutes:02d}:{secs:02d}"


def format_size(size_bytes) -> str:
    try:
        value = float(size_bytes)
    except (TypeError, ValueError):
        return ""
    gib = value / (1024 ** 3)
    gb = value / 1_000_000_000
    return f"{gib:.2f} GiB / {gb:.2f} GB"


def bit_depth_from_video(video: dict) -> str:
    raw = video.get("bits_per_raw_sample")
    if raw:
        return f"{raw}-bit"
    pix_fmt = video.get("pix_fmt", "")
    match = re.search(r"p(\d+)", pix_fmt)
    if match:
        return f"{match.group(1)}-bit"
    if pix_fmt:
        return "8-bit"
    return ""


def chroma_from_pix_fmt(pix_fmt: str) -> str:
    lowered = (pix_fmt or "").lower()
    if "yuv420" in lowered:
        return "4:2:0"
    if "yuv422" in lowered:
        return "4:2:2"
    if "yuv444" in lowered:
        return "4:4:4"
    if "rgb" in lowered or "gbr" in lowered:
        return "RGB"
    return ""


def normalized_level(video: dict) -> str:
    codec = (video.get("codec_name") or "").lower()
    raw = video.get("level")
    try:
        level = int(raw)
    except (TypeError, ValueError):
        return str(raw) if raw not in ("", None) else ""
    if codec in {"hevc", "h265"}:
        return f"Level {level / 30:.1f} (raw {level})"
    if codec in {"h264", "avc"}:
        return f"Level {level / 10:.1f} (raw {level})"
    return str(level)


def stream_disposition(stream: dict) -> str:
    disposition = stream.get("disposition") or {}
    flags = [name for name, value in disposition.items() if value == 1 and name in {"default", "forced", "hearing_impaired", "visual_impaired", "comment"}]
    return ", ".join(flags)


def stream_flag(stream: dict, flag: str) -> str:
    return yes_no((stream.get("disposition") or {}).get(flag) == 1)


def codec_id(stream: dict) -> str:
    tags = stream.get("tags") or {}
    for key in ("codec_id", "CODEC_ID", "handler_name"):
        if tags.get(key):
            return tags[key]
    return stream.get("codec_tag_string", "")


def hevc_tier(video: dict, all_text: str) -> str:
    tier = video.get("tier") or safe_get(video, "tags", "tier")
    if tier:
        return str(tier)
    match = re.search(r"\b(Main|High)\s+tier\b", all_text, re.I)
    return match.group(1).title() if match else ""


def audio_has_atmos(audio: dict, all_text: str) -> bool:
    values = [
        audio.get("profile", ""),
        audio.get("codec_long_name", ""),
        safe_get(audio, "tags", "title"),
        all_text,
    ]
    return bool(re.search(r"Atmos|JOC|Dolby Digital Plus with Dolby Atmos|E-AC-3 JOC", "\n".join(map(str, values)), re.I))


def audio_has_dtsx(audio: dict, all_text: str) -> bool:
    values = [
        audio.get("profile", ""),
        audio.get("codec_long_name", ""),
        safe_get(audio, "tags", "title"),
        all_text,
    ]
    return bool(re.search(r"DTS[: -]?X|DTS Express Object|XLL X", "\n".join(map(str, values)), re.I))


def compatibility_checklist(videos: list[dict], audios: list[dict], subs: list[dict], fmt: dict, analysis: dict, all_text: str) -> list[tuple[str, object]]:
    primary_video = videos[0] if videos else {}
    video_requirements = []
    if primary_video:
        video_requirements.extend([
            primary_video.get("codec_name", "").upper(),
            primary_video.get("profile", ""),
            hevc_tier(primary_video, all_text),
            normalized_level(primary_video),
            bit_depth_from_video(primary_video),
            chroma_from_pix_fmt(primary_video.get("pix_fmt", "")),
        ])
    hdr_requirements = []
    if analysis["has_dv"]:
        hdr_requirements.append("Dolby Vision")
        if analysis["dv_profile"]:
            hdr_requirements.append(f"Profile {analysis['dv_profile']}")
        if analysis["dv_level"]:
            hdr_requirements.append(f"Level {analysis['dv_level']}")
        if analysis["dv_compat"]:
            hdr_requirements.append(f"BL compat {analysis['dv_compat']}")
    if analysis["has_hdr10p"]:
        hdr_requirements.append("HDR10+ dynamic metadata")
    if analysis["has_pq"] and analysis["has_bt2020"]:
        hdr_requirements.append("HDR10 base layer")
    if analysis["has_hlg"]:
        hdr_requirements.append("HLG")

    audio_codecs = []
    for audio in audios:
        codec = audio.get("codec_name", "").upper()
        if audio_has_atmos(audio, all_text):
            codec = f"{codec} Atmos/JOC"
        elif audio_has_dtsx(audio, all_text):
            codec = f"{codec} DTS:X"
        if codec and codec not in audio_codecs:
            audio_codecs.append(codec)

    subtitle_codecs = []
    forced_count = 0
    for sub in subs:
        codec = sub.get("codec_name", "")
        if codec and codec not in subtitle_codecs:
            subtitle_codecs.append(codec)
        if (sub.get("disposition") or {}).get("forced") == 1:
            forced_count += 1

    notes = []
    format_name = fmt.get("format_name", "")
    if analysis["has_dv"] and "matroska" in format_name:
        notes.append("Dolby Vision in MKV")
    if analysis["has_hdr10p"] and "matroska" in format_name:
        notes.append("HDR10+ in MKV")
    if any(audio_has_atmos(a, all_text) for a in audios):
        notes.append("Object audio present")
    if forced_count:
        notes.append(f"{forced_count} forced subtitle track(s)")

    return [
        ("Primary video requirements", ", ".join(item for item in video_requirements if item)),
        ("HDR requirements", ", ".join(hdr_requirements) if hdr_requirements else "SDR or unclear"),
        ("Audio decode requirements", ", ".join(audio_codecs) if audio_codecs else ""),
        ("Subtitle render requirements", ", ".join(subtitle_codecs) if subtitle_codecs else "None detected"),
        ("Container", fmt.get("format_long_name") or fmt.get("format_name")),
        ("Compatibility notes", ", ".join(notes) if notes else "No additional checklist flags; see Findings for scan issues"),
    ]


def playback_summary(video: dict, audio: dict | None, fmt: dict, analysis: dict, stream_counts: dict, all_text: str) -> list[tuple[str, object]]:
    video_summary = ""
    if video:
        parts = [
            video.get("codec_name", "").upper(),
            video.get("profile", ""),
            f"{video.get('width', '')}x{video.get('height', '')}" if video.get("width") and video.get("height") else "",
            video.get("avg_frame_rate", ""),
            bit_depth_from_video(video),
            chroma_from_pix_fmt(video.get("pix_fmt", "")),
        ]
        video_summary = ", ".join(part for part in parts if part)

    hdr_parts = []
    if analysis["has_dv"]:
        hdr_parts.append("Dolby Vision")
    if analysis["has_hdr10p"]:
        hdr_parts.append("HDR10+")
    elif analysis["has_pq"] and analysis["has_bt2020"]:
        hdr_parts.append("HDR10")
    if analysis["has_hlg"]:
        hdr_parts.append("HLG")
    if analysis["has_mastering"]:
        hdr_parts.append("mastering metadata")
    if analysis["has_light"]:
        hdr_parts.append("MaxCLL/MaxFALL")

    audio_summary = ""
    if audio:
        audio_parts = [
            audio.get("codec_name", "").upper(),
            audio.get("profile", ""),
            f"{audio.get('channels')} channels" if audio.get("channels") else "",
            audio.get("channel_layout", ""),
            "Atmos/JOC" if audio_has_atmos(audio, all_text) else "",
        ]
        audio_summary = ", ".join(part for part in audio_parts if part)

    return [
        ("Video", video_summary),
        ("HDR", ", ".join(hdr_parts) if hdr_parts else "SDR/unclear"),
        ("Audio", audio_summary),
        ("Container", fmt.get("format_long_name") or fmt.get("format_name")),
        ("Streams", f"{stream_counts['video']} video, {stream_counts['audio']} audio, {stream_counts['subtitle']} subtitle, {stream_counts['chapter']} chapters"),
    ]


def hdr_analysis(probe: dict, frame_probe: dict, readable: str, mediainfo_text: str) -> dict:
    video = next((s for s in probe.get("streams", []) if s.get("codec_type") == "video"), {})
    # Exclude filenames and arbitrary title/comment tags from format detection.
    video_fields = {k: v for k, v in video.items() if k != "tags"}
    mi_hdr_fields = "\n".join(
        line for line in (mediainfo_text or "").splitlines()
        if re.match(r"\s*(HDR format|Transfer characteristics|Color primaries|Matrix coefficients|Mastering display|Maximum Content Light Level|Maximum Frame-Average Light Level)\s*:", line, re.I)
    )
    all_text = "\n".join([mi_hdr_fields, json.dumps(video_fields), json.dumps(frame_probe or {})])
    side_data = side_data_items(probe, frame_probe)
    dv_side = [x for x in side_data if re.search(r"DOVI|Dolby Vision", x.get("side_data_type", ""), re.I)]
    hdr10p_side = [x for x in side_data if re.search(r"Dynamic HDR Plus|HDR10\+|2094-40", x.get("side_data_type", ""), re.I)]
    mastering_side = [x for x in side_data if re.search(r"Mastering display metadata", x.get("side_data_type", ""), re.I)]
    light_side = [x for x in side_data if re.search(r"Content light level metadata", x.get("side_data_type", ""), re.I)]

    transfer = video.get("color_transfer", "")
    primaries = video.get("color_primaries", "")
    space = video.get("color_space", "")
    codec_tag = video.get("codec_tag_string", "")

    has_dv = bool(re.search(r"Dolby Vision|DOVI|dvhe|dvh1|dv_profile|HDR_Format.*Dolby Vision", all_text, re.I) or dv_side or re.search(r"dvhe|dvh1", codec_tag, re.I))
    has_hdr10p = bool(re.search(r"HDR10\+|Dynamic HDR Plus|Dynamic HDR10|SMPTE ST 2094 App(?:lication)? 4\b|2094-40|application/x-hdr10plus", all_text, re.I) or hdr10p_side)
    has_pq = bool("smpte2084" in transfer.lower() or re.search(r"smpte2084|PQ", all_text, re.I))
    has_bt2020 = bool("bt2020" in primaries.lower() or "bt2020" in space.lower() or re.search(r"BT\.2020|bt2020", all_text, re.I))
    has_hlg = bool("arib-std-b67" in transfer.lower() or re.search(r"HLG|arib-std-b67", all_text, re.I))
    has_mastering = bool(mastering_side or re.search(r"Mastering display|Mastering Display|SMPTE ST 2086", all_text, re.I))
    has_light = bool(light_side or re.search(r"MaxCLL|MaxFALL|Content light", all_text, re.I))

    dv_profile = safe_get(dv_side[0], "dv_profile") if dv_side else ""
    dv_level = safe_get(dv_side[0], "dv_level") if dv_side else ""
    dv_compat = safe_get(dv_side[0], "dv_bl_signal_compatibility_id") if dv_side else ""
    if not dv_profile:
        match = re.search(r"Dolby Vision.*?Profile\s*([0-9.]+)", mediainfo_text or "", re.I | re.S)
        if match:
            dv_profile = match.group(1)

    if has_dv and has_hdr10p:
        verdict = "Dolby Vision and HDR10+ indicators detected."
    elif has_dv:
        verdict = "Dolby Vision indicators detected."
    elif has_hdr10p:
        verdict = "HDR10+ dynamic metadata indicators detected."
    elif has_pq and has_bt2020:
        verdict = "HDR10 base indicators detected: BT.2020 plus SMPTE 2084/PQ."
    elif has_hlg:
        verdict = "HLG HDR indicators detected."
    else:
        verdict = "HDR format unclear or SDR: no Dolby Vision, HDR10+, PQ HDR10, or HLG indicators were found."

    return {
        "verdict": verdict,
        "has_dv": has_dv,
        "dv_profile": dv_profile,
        "dv_level": dv_level,
        "dv_compat": dv_compat,
        "has_hdr10p": has_hdr10p,
        "has_pq": has_pq,
        "has_bt2020": has_bt2020,
        "has_hlg": has_hlg,
        "has_mastering": has_mastering,
        "has_light": has_light,
        "side_data": side_data,
        "dv_side": dv_side,
        "hdr10p_side": hdr10p_side,
        "hdr10p_count": len(hdr10p_side),
        "mastering_side": mastering_side,
        "light_side": light_side,
    }


def yes_no(value: bool) -> str:
    return "Yes" if value else "No"


def sanitize_report_value(value, source: object):
    return report_privacy.clean(value, source, report_privacy.media_label(media_display_name(source)))


def filtered_json_for_report(probe: dict, frame_probe: dict, options: dict, source: object) -> tuple[dict, dict]:
    clean_probe = sanitize_report_value(probe, source)
    clean_frame_probe = sanitize_report_value(frame_probe, source)

    if not options.get("IncludeContainer"):
        clean_probe.pop("format", None)
    if not options.get("IncludeChapters"):
        clean_probe.pop("chapters", None)

    allowed_stream_types = set()
    if options.get("IncludeVideo") or options.get("IncludeHdr") or options.get("IncludeSideData"):
        allowed_stream_types.add("video")
    if options.get("IncludeAudio"):
        allowed_stream_types.add("audio")
    if options.get("IncludeSubtitles"):
        allowed_stream_types.add("subtitle")

    if "streams" in clean_probe:
        clean_probe["streams"] = [
            stream for stream in clean_probe.get("streams", [])
            if stream.get("codec_type") in allowed_stream_types
        ]

    if not options.get("IncludeSideData"):
        for stream in clean_probe.get("streams", []):
            stream.pop("side_data_list", None)
        clean_frame_probe = {}

    return clean_probe, clean_frame_probe


def md_table(rows: list[tuple[str, object]]) -> list[str]:
    clean_rows = []
    for label, value in rows:
        if value in ("", None, "x"):
            continue
        clean_label = str(label).replace("|", "\\|").replace("\n", " ")
        clean_value = str(value).replace("|", "\\|").replace("\n", " ")
        clean_rows.append((clean_label, clean_value))
    if not clean_rows:
        return []

    field_width = max(len("Field"), *(len(label) for label, _ in clean_rows))
    value_width = max(len("Value"), *(len(value) for _, value in clean_rows))
    lines = [
        f"| {'Field'.ljust(field_width)} | {'Value'.ljust(value_width)} |",
        f"| {'-' * field_width} | {'-' * value_width} |",
    ]
    for label, value in clean_rows:
        lines.append(f"| {label.ljust(field_width)} | {value.ljust(value_width)} |")
    return lines


def md_bool_table(rows: list[tuple[str, bool]]) -> list[str]:
    return md_table([(label, yes_no(value)) for label, value in rows])


def probe_app1_sei(source: str, video: dict, packet_limit: int, runner: Runner) -> dict:
    if video.get("codec_name") != "hevc":
        return {"status": "unsupported", "reason": "The App 1 SEI scanner currently supports HEVC only"}
    ffmpeg = resolve_tool(next(s for s in TOOL_SPECS if s.name == "ffmpeg"))
    if not ffmpeg["found"]:
        return {"status": "unavailable", "reason": "ffmpeg is missing or not runnable"}
    network_args = ["-rw_timeout", "15000000"] if is_url(source) else []
    try:
        with tempfile.TemporaryDirectory(prefix="MediaProber-SEI-") as folder:
            destination = Path(folder) / "sample.hevc"
            result = runner.run([
                str(ffmpeg["path"]), "-nostdin", "-hide_banner", "-v", "error", *network_args,
                "-i", source, "-map", "0:v:0", "-c:v", "copy", "-frames:v", str(packet_limit),
                "-bsf:v", "hevc_mp4toannexb,filter_units=pass_types=39|40",
                "-f", "hevc", "-fs", "33554432", str(destination),
            ], timeout=120)
            if result.returncode != 0 or result.stderr.strip():
                return {"status": "failed", "reason": "ffmpeg SEI extraction failed", "errors": [f"Extraction failed (exit {result.returncode}); private diagnostic details remain in the local log"]}
            if not destination.exists():
                return {"status": "failed", "reason": "No SEI extraction output was created"}
            if destination.stat().st_size >= 33554432:
                return {"status": "failed", "reason": "SEI sample exceeded the 32 MiB analysis limit"}
            return hdr_st2094.scan_hevc_sei(destination.read_bytes())
    except (OSError, ValueError) as exc:
        return {"status": "failed", "reason": str(exc)}


def probe_media(media_source: object, frame_count: int, emit) -> dict:
    runner = Runner(emit)
    source_text = str(media_source)
    file_name = media_display_name(media_source)
    remote = is_url(media_source)
    ffprobe = resolve_tool(TOOL_SPECS[0])["path"]
    if not ffprobe or not tool_is_runnable(ffprobe, TOOL_SPECS[0]):
        raise RuntimeError("ffprobe was not found or is not runnable.")
    if frame_count < 1:
        raise ValueError("The packet scan limit must be positive.")
    if not remote:
        source_text = str(Path(source_text).expanduser().resolve())

    emit_event(emit, "Preparing probe...", "INFO", 3)
    runner.log(f"Starting probe report for {file_name}")
    if remote:
        emit_event(emit, "Remote URL mode: ffprobe will read stream metadata/ranges without intentionally downloading the whole file.", "WARN", 5)
    network_args = ["-rw_timeout", "15000000"] if remote else []
    command_timeout = 120
    emit_event(emit, "Reading container and stream metadata with ffprobe...", "INFO", 10)
    metadata = runner.run([
        str(ffprobe), "-v", "error", *network_args, "-show_format", "-show_streams", "-show_chapters", "-of", "json", source_text
    ], timeout=command_timeout)
    if metadata.returncode != 0:
        detail = (metadata.stderr or metadata.stdout or "ffprobe returned no details").strip()
        raise RuntimeError(f"ffprobe could not read the media source. {detail}")
    probe = parse_json(metadata.stdout)
    if not probe:
        raise RuntimeError("ffprobe returned no parseable metadata for this media source.")

    emit_event(emit, "Reading human-readable ffprobe summary...", "INFO", 28)
    readable_result = runner.run([str(ffprobe), "-hide_banner", *network_args, "-i", source_text], timeout=command_timeout)
    readable = (readable_result.stdout + "\n" + readable_result.stderr).replace(source_text, file_name)

    emit_event(emit, f"Scanning up to {frame_count} video packets for HDR frame side data...", "INFO", 45)
    runner.log(f"Scanning up to {frame_count} video packets for side data...")
    frame_result = runner.run([
        str(ffprobe), "-v", "error", "-select_streams", "v:0",
        *network_args,
        "-read_intervals", f"%+#{frame_count}",
        "-show_frames", "-show_entries", "frame=best_effort_timestamp_time,pict_type,side_data_list",
        "-of", "json", source_text,
    ], timeout=command_timeout)
    try:
        frame_probe = hdr_st2094.frame_json(frame_result.stdout)
    except json.JSONDecodeError:
        frame_probe = {}
    if frame_result.returncode != 0 or not frame_probe or frame_result.stderr.strip():
        detail = (frame_result.stderr or "No valid frame data returned").strip()
        raise RuntimeError(f"Video frame scan failed; HDR detection is incomplete. {detail}")

    mediainfo_text = ""
    mediainfo_json = {}
    mediainfo_item = next((item for item in get_inventory() if item["spec"].name == "mediainfo"), None)
    mediainfo_path = mediainfo_item["path"] if mediainfo_item and mediainfo_item["found"] else None
    if mediainfo_path and not remote:
        emit_event(emit, "Reading MediaInfo text output...", "INFO", 68)
        runner.log("Reading MediaInfo text output...")
        mi = runner.run([str(mediainfo_path), source_text], timeout=command_timeout)
        mediainfo_text = (mi.stdout + "\n" + mi.stderr).replace(source_text, file_name)
        emit_event(emit, "Reading MediaInfo JSON output...", "INFO", 82)
        runner.log("Reading MediaInfo JSON output...")
        mi_json = runner.run([str(mediainfo_path), "--Output=JSON", source_text], timeout=command_timeout)
        mediainfo_json = parse_json(mi_json.stdout)
    elif remote:
        emit_event(emit, "Skipping MediaInfo for remote URL; ffprobe URL probing is more reliable for streams.", "WARN", 82)
    else:
        emit_event(emit, "MediaInfo unavailable; continuing with ffprobe data.", "WARN", 82)

    emit_event(emit, "Inspecting ST 2094-10 HEVC SEI carriage...", "INFO", 88)
    video = next((s for s in probe.get("streams", []) if s.get("codec_type") == "video"), {})
    app1_scan = probe_app1_sei(source_text, video, frame_count, runner)
    if app1_scan.get("status") == "failed" or app1_scan.get("errors"):
        runner.log("ST 2094-10 analysis is incomplete; see the report for details.", "WARN")
    emit_event(emit, "Probe data captured. Rendering report...", "SUCCESS", 92)

    return {
        "file_path": source_text,
        "file_name": file_name,
        "is_remote": remote,
        "frame_count": frame_count,
        "probe": probe,
        "readable": readable,
        "frame_probe": frame_probe,
        "mediainfo_text": mediainfo_text,
        "mediainfo_json": mediainfo_json,
        "st2094_10_sei": app1_scan,
    }


def render_report(cache: dict, options: dict) -> tuple[list[str], str]:
    source = cache["file_path"]
    cache = dict(cache)
    cache["filename_claims"] = diagnostic_report.filename_claims(media_display_name(source))
    cache["file_name"] = report_privacy.media_label(media_display_name(source))
    for key in ("probe", "frame_probe", "mediainfo_json", "st2094_10_sei"):
        if key in cache:
            cache[key] = sanitize_report_value(cache[key], source)
    cache["readable"] = report_privacy.technical_text(cache.get("readable", ""), source)
    cache["mediainfo_text"] = report_privacy.technical_text(cache.get("mediainfo_text", ""), source, "mediainfo")
    media_source = cache["file_path"]
    file_name = cache["file_name"]
    frame_count = cache["frame_count"]
    probe = cache["probe"]
    readable = cache["readable"]
    frame_probe = cache["frame_probe"]
    mediainfo_text = cache["mediainfo_text"]
    mediainfo_json = cache["mediainfo_json"]
    analysis = hdr_analysis(probe, frame_probe, readable, mediainfo_text)
    streams = probe.get("streams", [])
    videos = [s for s in streams if s.get("codec_type") == "video"]
    audios = [s for s in streams if s.get("codec_type") == "audio"]
    subs = [s for s in streams if s.get("codec_type") == "subtitle"]
    chapters = probe.get("chapters", []) or []
    fmt = probe.get("format", {})
    first_video = videos[0] if videos else {}
    first_audio = audios[0] if audios else None
    all_text = "\n".join([readable or "", mediainfo_text or "", json.dumps(probe), json.dumps(frame_probe or {})])
    stream_counts = {
        "video": len(videos),
        "audio": len(audios),
        "subtitle": len(subs),
        "chapter": len(chapters),
    }

    clean_probe, clean_frame_probe = filtered_json_for_report(probe, frame_probe, options, media_source)
    clean_mediainfo_json = sanitize_report_value(mediainfo_json, media_source)

    lines = [
        "# Media Prober Report",
        "",
        "Metadata inspection and a bounded FFmpeg frame scan; this does not certify player, hardware-decoder, display, or audio-passthrough support.",
        "",
        *md_table([
            ("File", file_name),
            ("Source", "Remote URL" if cache.get("is_remote") else "Local file"),
            ("Generated", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            ("Verdict", analysis["verdict"]),
        ]),
        "",
    ]

    lines += diagnostic_report.front_page(cache, options, md_table)

    if options.get("IncludeTools"):
        lines += ["## Tool Status", "", "```text", format_inventory(show_paths=False).strip(), "```", ""]

    lines += ["## Playback Compatibility Summary", ""]
    lines += md_table(playback_summary(first_video, first_audio, fmt, analysis, stream_counts, all_text))
    lines.append("")

    if options.get("IncludeChecklist", True):
        lines += ["## Player Compatibility Checklist", ""]
        lines += md_table(compatibility_checklist(videos, audios, subs, fmt, analysis, all_text))
        lines.append("")

    if options.get("IncludeContainer"):
        lines += ["## Container", ""]
        container_rows = []
        for label, key in (
            ("Format", "format_name"), ("Format long name", "format_long_name"),
            ("Duration seconds", "duration"), ("Size bytes", "size"), ("Overall bitrate bps", "bit_rate"),
        ):
            if fmt.get(key):
                container_rows.append((label, fmt[key]))
        container_rows.extend([
            ("Duration", format_duration(fmt.get("duration"))),
            ("Size", format_size(fmt.get("size"))),
            ("Video streams", len(videos)),
            ("Audio streams", len(audios)),
            ("Subtitle streams", len(subs)),
            ("Chapters", len(chapters)),
        ])
        lines += md_table(container_rows) or ["Could not parse container metadata from ffprobe JSON."]
        lines.append("")

    if options.get("IncludeVideo"):
        lines += ["## Video", ""]
        if not videos:
            lines.append("No video streams parsed.")
        for v in videos:
            if len(videos) > 1:
                lines += [f"### Stream #{v.get('index', '')}", ""]
            fields = [
                ("Codec", v.get("codec_name", "")),
                ("Codec long name", v.get("codec_long_name", "")),
                ("Profile", v.get("profile", "")),
                ("Codec ID / FourCC", codec_id(v)),
                ("Codec tag", v.get("codec_tag_string", "")),
                ("Resolution", f"{v.get('width', '')}x{v.get('height', '')}"),
                ("Pixel format", v.get("pix_fmt", "")),
                ("Bit depth", bit_depth_from_video(v)),
                ("Chroma subsampling", chroma_from_pix_fmt(v.get("pix_fmt", ""))),
                ("Tier", hevc_tier(v, all_text)),
                ("Level", normalized_level(v)),
                ("Frame rate", v.get("avg_frame_rate", "")),
                ("Bitrate bps", v.get("bit_rate", "")), ("Bits per raw sample", v.get("bits_per_raw_sample", "")),
                ("Color range", v.get("color_range", "")), ("Color primaries", v.get("color_primaries", "")),
                ("Color transfer", v.get("color_transfer", "")), ("Color space/matrix", v.get("color_space", "")),
                ("Default", stream_flag(v, "default")),
                ("Forced", stream_flag(v, "forced")),
                ("Disposition", stream_disposition(v)),
            ]
            lines += md_table(fields)
            lines.append("")

    if options.get("IncludeHdr"):
        lines += ["## HDR / Dolby Vision / HDR10+", ""]
        hdr_rows = [
            ("Dolby Vision", analysis["has_dv"]),
            ("HDR10+ dynamic metadata", analysis["has_hdr10p"]),
            ("BT.2020", analysis["has_bt2020"]),
            ("SMPTE 2084 / PQ", analysis["has_pq"]),
            ("HLG", analysis["has_hlg"]),
            ("Mastering display metadata", analysis["has_mastering"]),
            ("MaxCLL / MaxFALL", analysis["has_light"]),
        ]
        lines += md_bool_table(hdr_rows)
        lines.append("")
        hdr_detail_rows = [
            ("HDR10+ side-data entries", analysis["hdr10p_count"] if analysis["has_hdr10p"] else ""),
        ]
        if analysis["mastering_side"]:
            hdr_detail_rows.append(("Mastering metadata entries", len(analysis["mastering_side"])))
        if analysis["light_side"]:
            hdr_detail_rows.append(("Content light entries", len(analysis["light_side"])))
        hdr_detail_table = md_table(hdr_detail_rows)
        if hdr_detail_table:
            lines += hdr_detail_table
            lines.append("")
        if analysis["light_side"]:
            lines += ["### Content Light Metadata", ""]
            lines += md_table(side_data_fields(analysis["light_side"][0]))
            lines.append("")
        if analysis["mastering_side"]:
            lines += ["### Mastering Display Metadata", ""]
            lines += md_table(side_data_fields(analysis["mastering_side"][0]))
            lines.append("")
        dv_rows = []
        if analysis["dv_profile"]:
            dv_rows.append(("Dolby Vision profile", analysis["dv_profile"]))
        if analysis["dv_level"]:
            dv_rows.append(("Dolby Vision level", analysis["dv_level"]))
        if analysis["dv_compat"]:
            dv_rows.append(("Dolby Vision BL compatibility ID", analysis["dv_compat"]))
        if options.get("IncludeSideData"):
            dv_rows.append(("Video packet scan limit", frame_count))
            dv_rows.append(("Frames decoded", len(frame_probe.get("frames", []))))
        detail_table = md_table(dv_rows)
        if detail_table:
            lines += detail_table
            lines.append("")
        if options.get("IncludeSideData"):
            for title, items in (
                ("Dolby Vision side data", analysis["dv_side"]),
                ("HDR10+ side data", analysis["hdr10p_side"]),
                ("Mastering display side data", analysis["mastering_side"]),
                ("Content light side data", analysis["light_side"]),
            ):
                if items:
                    lines.append(f"### {title}")
                    lines.append("")
                    lines += [f"- {side_data_text(item)}" for item in items]
                    lines.append("")

    if options.get("IncludeST2094", True):
        lines += hdr_st2094.report(cache, md_table)

    if options.get("IncludeAudio"):
        lines += ["## Audio", ""]
        if not audios:
            lines.append("No audio streams parsed.")
        for a in audios:
            tags = a.get("tags", {}) or {}
            rows = [
                ("Stream", f"#{a.get('index', '')}"),
                ("Codec", a.get("codec_name", "")),
                ("Codec long name", a.get("codec_long_name", "")),
                ("Profile", a.get("profile", "")),
                ("Atmos / JOC", yes_no(audio_has_atmos(a, all_text))),
                ("DTS:X", yes_no(audio_has_dtsx(a, all_text))),
                ("Language", tags.get("language", "")), ("Title", tags.get("title", "")),
                ("Channels", a.get("channels", "")), ("Channel layout", a.get("channel_layout", "")),
                ("Sample rate", a.get("sample_rate", "")), ("Bitrate bps", a.get("bit_rate", "")),
                ("Default", stream_flag(a, "default")),
                ("Forced", stream_flag(a, "forced")),
                ("Disposition", stream_disposition(a)),
            ]
            lines += md_table(rows)
            lines.append("")

    if options.get("IncludeSubtitles"):
        lines += ["## Subtitles", "", f"Subtitle tracks detected: {len(subs)}", ""]
        for s in subs:
            tags = s.get("tags", {}) or {}
            if len(subs) > 1:
                lines += [f"### Stream #{s.get('index', '')}", ""]
            rows = [
                ("Stream", f"#{s.get('index', '')}"),
                ("Codec", s.get("codec_name", "")),
                ("Codec long name", s.get("codec_long_name", "")),
                ("Codec ID / FourCC", codec_id(s)),
                ("Language", tags.get("language", "")),
                ("Title", tags.get("title", "")),
                ("Default", stream_flag(s, "default")),
                ("Forced", stream_flag(s, "forced")),
                ("Hearing impaired", stream_flag(s, "hearing_impaired")),
                ("Disposition", stream_disposition(s)),
            ]
            lines += md_table(rows)
            lines.append("")

    if options.get("IncludeChapters"):
        lines += ["## Chapters", "", f"Chapter count: {len(chapters)}", ""]
        for chapter in chapters:
            tags = chapter.get("tags", {}) or {}
            lines.append(f"- Chapter {chapter.get('id', '')}: {chapter.get('start_time', '')} - {chapter.get('end_time', '')}: {tags.get('title', 'Untitled')}")
        lines.append("")

    if options.get("IncludeMediaInfo"):
        lines += ["## MediaInfo Relevant Lines", "", "```text"]
        mi_lines = relevant_lines(mediainfo_text)
        lines += mi_lines if mi_lines else ["No MediaInfo HDR/codec-specific lines matched or MediaInfo was unavailable."]
        lines += ["```", ""]

    if options.get("IncludeRawLines"):
        lines += ["## Relevant Raw ffprobe Lines", "", "```text"]
        raw = relevant_lines(readable)
        lines += raw if raw else ["No filtered raw ffprobe lines matched."]
        lines += ["```", ""]

    if options.get("IncludeSideData"):
        lines += ["## All Video Side Data Found", ""]
        all_side = analysis["side_data"]
        lines += [f"- {side_data_text(item)}" for item in all_side] if all_side else ["No stream/frame side data was found in the scanned range."]
        lines.append("")

    if options.get("IncludeJson"):
        lines += ["## Sanitized ffprobe JSON", "", "```json", json.dumps(clean_probe, indent=2), "```", ""]
        if frame_probe:
            lines += ["## Sanitized Frame Side-Data JSON", "", "```json", json.dumps(clean_frame_probe, indent=2), "```", ""]
        if mediainfo_json:
            lines += ["## Sanitized MediaInfo JSON", "", "```json", json.dumps(clean_mediainfo_json, indent=2), "```", ""]
        if options.get("IncludeST2094", True):
            lines += ["## ST 2094-10 Parsed SEI JSON", "", "```json", json.dumps(sanitize_report_value(cache.get("st2094_10_sei", {}), media_source), indent=2), "```", ""]

    # Final common boundary covers every optional section and cached re-render.
    return report_privacy.redact("\n".join(lines), source).splitlines(), analysis["verdict"]


def build_report(file_path: Path, frame_count: int, options: dict, emit) -> tuple[list[str], str]:
    return render_report(probe_media(file_path, frame_count, emit), options)


class ProbeApp:
    def __init__(self, root: Tk) -> None:
        self.root = root
        self.root.title(APP_NAME)
        self.root.geometry("1020x760")
        self.root.minsize(900, 650)
        self.queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.last_report: Path | None = None
        self.last_probe_cache: dict | None = None
        self.running = False

        self.file_var = StringVar()
        self.frame_var = IntVar(value=300)
        self.vars = {
            "IncludeTools": BooleanVar(value=True),
            "IncludeContainer": BooleanVar(value=True),
            "IncludeVideo": BooleanVar(value=True),
            "IncludeHdr": BooleanVar(value=True),
            "IncludeSideData": BooleanVar(value=True),
            "IncludeAudio": BooleanVar(value=True),
            "IncludeSubtitles": BooleanVar(value=False),
            "IncludeChapters": BooleanVar(value=False),
            "IncludeMediaInfo": BooleanVar(value=True),
            "IncludeRawLines": BooleanVar(value=True),
            "IncludeJson": BooleanVar(value=False),
        }

        self.configure_theme()
        self.build_ui()
        self.log(f"{APP_NAME} ready.")
        self.log(f"Log file: {LOGGER.path}")
        self.log("")
        self.log(format_inventory())
        self.root.after(100, self.drain_queue)

    def configure_theme(self) -> None:
        self.colors = {
            "bg": "#181a1f", "panel": "#22252b", "input": "#111318", "button": "#303640",
            "button_hover": "#3e4654", "text": "#ebeff5", "muted": "#b9c1cc", "accent": "#4ea1ff",
        }
        self.root.configure(bg=self.colors["bg"])
        style = ttk.Style()
        style.theme_use("clam")
        style.configure(".", background=self.colors["bg"], foreground=self.colors["text"], fieldbackground=self.colors["input"])
        style.configure("TFrame", background=self.colors["bg"])
        style.configure("Panel.TLabelframe", background=self.colors["panel"], foreground=self.colors["text"], bordercolor=self.colors["accent"])
        style.configure("Panel.TLabelframe.Label", background=self.colors["panel"], foreground=self.colors["text"])
        style.configure("TLabel", background=self.colors["bg"], foreground=self.colors["muted"])
        style.configure("TCheckbutton", background=self.colors["panel"], foreground=self.colors["text"])
        style.map("TCheckbutton", background=[("active", self.colors["panel"])], foreground=[("active", self.colors["text"])])
        style.configure("TButton", background=self.colors["button"], foreground=self.colors["text"], bordercolor=self.colors["accent"], focusthickness=1)
        style.map("TButton", background=[("active", self.colors["button_hover"])])
        style.configure("TEntry", fieldbackground=self.colors["input"], foreground=self.colors["text"], insertcolor=self.colors["text"])
        style.configure("TCombobox", fieldbackground=self.colors["input"], foreground=self.colors["text"], arrowcolor=self.colors["text"])

    def build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=16)
        outer.pack(fill="both", expand=True)

        ttk.Label(outer, text="Media file").pack(anchor="w")
        file_row = ttk.Frame(outer)
        file_row.pack(fill="x", pady=(4, 12))
        ttk.Entry(file_row, textvariable=self.file_var).pack(side="left", fill="x", expand=True)
        ttk.Button(file_row, text="Browse...", command=self.browse).pack(side="left", padx=(8, 0))

        sections = ttk.LabelFrame(outer, text="Report sections", style="Panel.TLabelframe", padding=12)
        sections.pack(fill="x", pady=(0, 12))
        checks = [
            ("Tool status", "IncludeTools"), ("Container", "IncludeContainer"), ("Video streams", "IncludeVideo"),
            ("HDR / DV / HDR10+", "IncludeHdr"), ("All side data", "IncludeSideData"),
            ("Audio streams", "IncludeAudio"), ("Subtitles", "IncludeSubtitles"), ("Chapters", "IncludeChapters"),
            ("MediaInfo lines", "IncludeMediaInfo"), ("Raw ffprobe lines", "IncludeRawLines"),
            ("Raw JSON appendices", "IncludeJson"),
        ]
        for idx, (label, key) in enumerate(checks):
            ttk.Checkbutton(sections, text=label, variable=self.vars[key]).grid(row=idx // 5, column=idx % 5, sticky="w", padx=8, pady=5)
        ttk.Label(sections, text="Video packet scan limit").grid(row=3, column=0, sticky="w", padx=8, pady=(12, 4))
        ttk.Combobox(sections, textvariable=self.frame_var, values=(50, 300, 1000, 3000), width=10).grid(row=3, column=1, sticky="w", padx=8, pady=(12, 4))
        ttk.Label(sections, text="Higher values can catch metadata that appears later, but scans take longer.").grid(row=3, column=2, columnspan=3, sticky="w", padx=8, pady=(12, 4))

        buttons = ttk.Frame(outer)
        buttons.pack(fill="x", pady=(0, 12))
        self.check_btn = ttk.Button(buttons, text="Check Tools", command=self.check_tools)
        self.install_btn = ttk.Button(buttons, text="Install Missing", command=self.install_missing)
        self.run_btn = ttk.Button(buttons, text="Run Probe", command=self.run_probe)
        self.generate_btn = ttk.Button(buttons, text="Generate Report", command=self.generate_report, state="disabled")
        self.report_btn = ttk.Button(buttons, text="Open Report", command=self.open_report, state="disabled")
        self.folder_btn = ttk.Button(buttons, text="Open Folder", command=self.open_folder, state="disabled")
        self.log_btn = ttk.Button(buttons, text="Open Log", command=self.open_log)
        for btn in (self.check_btn, self.install_btn, self.run_btn, self.generate_btn, self.report_btn, self.folder_btn, self.log_btn):
            btn.pack(side="left", padx=(0, 8))

        self.output = tk.Text(
            outer, bg=self.colors["input"], fg=self.colors["text"], insertbackground=self.colors["text"],
            relief="flat", wrap="word", font=("Consolas", 10), height=22,
        )
        self.output.pack(fill="both", expand=True)

    def browse(self) -> None:
        path = filedialog.askopenfilename(
            title="Select media file",
            filetypes=[
                ("Media files", "*.mkv *.mp4 *.m4v *.mov *.m2ts *.mts *.ts *.webm *.avi *.wmv *.flv *.mpg *.mpeg"),
                ("All files", "*.*"),
            ],
        )
        if path:
            self.file_var.set(path)

    def log(self, message: str) -> None:
        LOGGER.write(message)
        self.output.insert("end", f"[{now()}] {message}\n" if message else "\n")
        self.output.see("end")

    def emit(self, message: str) -> None:
        self.queue.put(("log", message))

    def set_running(self, running: bool) -> None:
        self.running = running
        state = "disabled" if running else "normal"
        for btn in (self.check_btn, self.install_btn, self.run_btn):
            btn.configure(state=state)
        self.generate_btn.configure(state=("disabled" if running or not self.last_probe_cache else "normal"))

    def drain_queue(self) -> None:
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "log":
                    self.log(str(payload))
                elif kind == "done":
                    self.on_worker_done(payload)
        except queue.Empty:
            pass
        self.root.after(100, self.drain_queue)

    def check_tools(self) -> None:
        self.log("")
        self.log(format_inventory())

    def install_missing(self) -> None:
        if self.running:
            return
        self.set_running(True)
        self.log("Installing missing tools...")

        def worker():
            try:
                install_missing_tools(self.emit)
                self.queue.put(("done", {"ok": True, "kind": "install"}))
            except Exception as exc:
                LOGGER.write(f"Install failed: {exc}", "ERROR")
                self.queue.put(("done", {"ok": False, "kind": "install", "error": str(exc)}))

        threading.Thread(target=worker, daemon=True).start()

    def run_probe(self) -> None:
        if self.running:
            return
        file_path = Path(self.file_var.get().strip())
        if not file_path.is_file():
            messagebox.showwarning(APP_NAME, "Select a valid media file first.")
            return
        if not required_tools_ok():
            if messagebox.askyesno(APP_NAME, "ffprobe is missing. Install missing tools now?"):
                self.install_missing()
            return

        self.set_running(True)
        self.report_btn.configure(state="disabled")
        self.folder_btn.configure(state="disabled")
        self.generate_btn.configure(state="disabled")
        self.log("")
        self.log(f"Running probe for {file_path}")
        options = {key: var.get() for key, var in self.vars.items()}
        LOGGER.write(f"Report options: {json.dumps(options, sort_keys=True)}")
        frame_count = int(self.frame_var.get() or 300)
        report_path = report_path_for_source(file_path)

        def worker():
            try:
                cache = probe_media(file_path, frame_count, self.emit)
                lines, verdict = render_report(cache, options)
                report_path.write_text("\n".join(lines), encoding="utf-8")
                LOGGER.write(f"Report saved to {report_path}")
                self.queue.put(("done", {"ok": True, "kind": "probe", "report": report_path, "verdict": verdict, "cache": cache}))
            except Exception as exc:
                LOGGER.write(f"Probe failed: {exc}", "ERROR")
                self.queue.put(("done", {"ok": False, "kind": "probe", "error": str(exc)}))

        threading.Thread(target=worker, daemon=True).start()

    def current_options(self) -> dict:
        return {key: var.get() for key, var in self.vars.items()}

    def report_path_for_cache(self, cache: dict) -> Path:
        file_path = Path(cache["file_path"])
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        safe_name = re.sub(r'[\\/:*?"<>|\[\]]', "", file_path.stem)
        return file_path.with_name(f"{safe_name} - Media Prober Report {stamp}.md")

    def generate_report(self) -> None:
        if self.running:
            return
        if not self.last_probe_cache:
            messagebox.showinfo(APP_NAME, "Run a probe first, then adjust the checkboxes and generate a report.")
            return

        options = self.current_options()
        LOGGER.write(f"Generating report from cached probe. Report options: {json.dumps(options, sort_keys=True)}")
        try:
            report_path = self.report_path_for_cache(self.last_probe_cache)
            lines, verdict = render_report(self.last_probe_cache, options)
            report_path.write_text("\n".join(lines), encoding="utf-8")
            self.last_report = report_path
            self.report_btn.configure(state="normal")
            self.folder_btn.configure(state="normal")
            self.log("")
            self.log("Report generated from last probe.")
            self.log("Verdict:")
            self.log(verdict)
            self.log(f"Report saved to: {report_path}")
        except Exception as exc:
            LOGGER.write(f"Generate report failed: {exc}", "ERROR")
            self.log(f"Generate report failed: {exc}")

    def on_worker_done(self, payload: dict) -> None:
        self.set_running(False)
        if payload.get("ok"):
            if payload.get("kind") == "probe":
                self.last_report = Path(payload["report"])
                self.last_probe_cache = payload.get("cache")
                self.generate_btn.configure(state="normal")
                self.report_btn.configure(state="normal")
                self.folder_btn.configure(state="normal")
                self.log("")
                self.log("Probe complete.")
                self.log("Verdict:")
                self.log(payload["verdict"])
                self.log(f"Report saved to: {self.last_report}")
                self.log("Adjust checkboxes and click Generate Report to re-render without probing again.")
            else:
                self.log("Install complete.")
        else:
            self.log("")
            self.log(f"{payload.get('kind', 'Worker')} failed: {payload.get('error')}")
        self.log(f"Log file: {LOGGER.path}")

    def open_report(self) -> None:
        if self.last_report and self.last_report.is_file():
            open_path(self.last_report)

    def open_folder(self) -> None:
        if self.last_report and self.last_report.is_file():
            reveal_path(self.last_report)

    def open_log(self) -> None:
        open_path(LOGGER.path)


def main() -> int:
    try:
        from PySide6.QtCore import QTimer
        from PySide6.QtGui import QFont
        from PySide6.QtWidgets import (
            QApplication,
            QCheckBox,
            QComboBox,
            QFileDialog,
            QFrame,
            QGridLayout,
            QGroupBox,
            QHBoxLayout,
            QLabel,
            QLineEdit,
            QMainWindow,
            QMessageBox,
            QProgressBar,
            QPushButton,
            QScrollArea,
            QSplitter,
            QStatusBar,
            QTabWidget,
            QTextBrowser,
            QTextEdit,
            QVBoxLayout,
            QWidget,
        )
    except ImportError as exc:
        print("PySide6 is required for the Qt version of Media Prober.")
        print("Install it with: python -m pip install PySide6")
        raise SystemExit(1) from exc

    class QtProbeApp(QMainWindow):
        def __init__(self) -> None:
            super().__init__()
            self.queue: queue.Queue[tuple[str, object]] = queue.Queue()
            self.last_report: Path | None = None
            self.last_probe_cache: dict | None = None
            self.running = False
            self.option_checks: dict[str, QCheckBox] = {}

            self.setWindowTitle(APP_NAME + " — ST 2094")
            self.resize(1180, 780)
            self.setMinimumSize(980, 680)
            self.setStatusBar(QStatusBar())
            self.apply_theme()
            self.build_ui()

            self.timer = QTimer(self)
            self.timer.timeout.connect(self.drain_queue)
            self.timer.start(100)

            self.log(f"{APP_NAME} ready.")
            self.log(f"Log file: {LOGGER.path}")
            self.log("")
            self.log_tool_inventory()

        def apply_theme(self) -> None:
            self.setStyleSheet("""
                QMainWindow, QWidget {
                    background: #15171c;
                    color: #eef2f8;
                    font-family: "Segoe UI", "Inter", Arial, sans-serif;
                    font-size: 10.5pt;
                }
                QLabel#Title {
                    font-size: 20pt;
                    font-weight: 700;
                    color: #ffffff;
                }
                QLabel#Subtitle {
                    color: #aeb8c7;
                }
                QLineEdit, QComboBox, QTextEdit {
                    background: #0f1116;
                    color: #eef2f8;
                    border: 1px solid #303846;
                    border-radius: 6px;
                    padding: 8px;
                    selection-background-color: #3b82f6;
                }
                QTextEdit {
                    font-family: Consolas, "SF Mono", monospace;
                    font-size: 10pt;
                }
                QProgressBar {
                    background: #0f1116;
                    color: #eaf2ff;
                    border: 1px solid #303846;
                    border-radius: 6px;
                    min-height: 18px;
                    text-align: center;
                    font-weight: 700;
                }
                QProgressBar::chunk {
                    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #22c55e, stop:0.5 #38bdf8, stop:1 #a78bfa);
                    border-radius: 5px;
                }
                QPushButton {
                    background: #29313d;
                    color: #eef2f8;
                    border: 1px solid #3a4656;
                    border-radius: 6px;
                    padding: 8px 12px;
                    font-weight: 600;
                }
                QPushButton:hover {
                    background: #364253;
                    border-color: #5a6b82;
                }
                QPushButton:pressed {
                    background: #1f2630;
                }
                QPushButton:disabled {
                    background: #20242b;
                    color: #687385;
                    border-color: #2a3039;
                }
                QGroupBox {
                    background: #1d2129;
                    border: 1px solid #303846;
                    border-radius: 8px;
                    margin-top: 14px;
                    padding: 12px;
                    font-weight: 700;
                }
                QGroupBox::title {
                    subcontrol-origin: margin;
                    left: 10px;
                    padding: 0 5px;
                    color: #dce6f5;
                }
                QCheckBox {
                    spacing: 8px;
                    color: #e8edf5;
                }
                QCheckBox::indicator {
                    width: 16px;
                    height: 16px;
                    border-radius: 4px;
                    border: 1px solid #59677a;
                    background: #11151b;
                }
                QCheckBox::indicator:checked {
                    background: #3b82f6;
                    border-color: #60a5fa;
                }
                QSplitter::handle {
                    background: #20252e;
                }
                QStatusBar {
                    background: #111318;
                    color: #aeb8c7;
                }
            """)

        def build_ui(self) -> None:
            root = QWidget()
            outer = QVBoxLayout(root)
            outer.setContentsMargins(18, 18, 18, 14)
            outer.setSpacing(14)
            self.setCentralWidget(root)

            title = QLabel(APP_NAME)
            title.setObjectName("Title")
            subtitle = QLabel("Inspect media codecs and HDR metadata, review playback requirements, and generate reports.")
            subtitle.setObjectName("Subtitle")
            outer.addWidget(title)
            outer.addWidget(subtitle)

            file_row = QHBoxLayout()
            self.file_edit = QLineEdit()
            self.file_edit.setPlaceholderText("Select a media file or paste a direct HTTP/HTTPS video URL")
            browse_btn = QPushButton("Browse...")
            browse_btn.clicked.connect(self.browse)
            file_row.addWidget(self.file_edit, 1)
            file_row.addWidget(browse_btn)
            outer.addLayout(file_row)

            splitter = QSplitter()
            outer.addWidget(splitter, 1)

            controls = QFrame()
            controls_layout = QVBoxLayout(controls)
            controls_layout.setContentsMargins(0, 0, 12, 0)
            controls_layout.setSpacing(12)
            controls_scroll = QScrollArea()
            controls_scroll.setWidgetResizable(True)
            controls_scroll.setWidget(controls)
            splitter.addWidget(controls_scroll)

            options_box = QGroupBox("Report Sections")
            options_grid = QGridLayout(options_box)
            checks = [
                ("Tool status", "IncludeTools", True, "Lists which probe tools were found and which optional tools are available."),
                ("Container", "IncludeContainer", True, "Adds format, duration, size, bitrate, stream counts, and chapter count."),
                ("Video streams", "IncludeVideo", True, "Adds codec, profile, FourCC, resolution, bit depth, chroma, level, tier, and color metadata."),
                ("Playback checklist", "IncludeChecklist", True, "Summarizes what your media player must support for video, HDR, audio, subtitles, and container quirks."),
                ("HDR / DV / HDR10+", "IncludeHdr", True, "Adds Dolby Vision, HDR10+, HDR10, HLG, mastering display, and MaxCLL/MaxFALL indicators."),
                ("ST 2094-40 / -10", "IncludeST2094", True, "Analyzes sampled HDR10+ frame fields and App 1 HEVC SEI; separates Dolby Vision RPU evidence."),
                ("Audio streams", "IncludeAudio", True, "Adds codec, profile, Atmos/JOC, DTS:X, channel layout, language, bitrate, and stream flags."),
                ("Subtitles", "IncludeSubtitles", True, "Adds subtitle codecs, languages, titles, default/forced flags, and accessibility flags."),
                ("Chapters", "IncludeChapters", False, "Adds chapter timestamps and titles when present."),
                ("All side data", "IncludeSideData", False, "Adds raw video side-data entries found in stream metadata and scanned frames."),
                ("MediaInfo lines", "IncludeMediaInfo", False, "Adds filtered MediaInfo text lines related to codecs, HDR, Dolby Vision, and color metadata."),
                ("Raw ffprobe lines", "IncludeRawLines", False, "Adds filtered human-readable ffprobe lines for troubleshooting parser differences."),
                ("Raw JSON appendices", "IncludeJson", False, "Adds sanitized ffprobe, frame side-data, and MediaInfo JSON appendices."),
            ]
            for idx, (label, key, default, tip) in enumerate(checks):
                box = QCheckBox(label)
                box.setChecked(default)
                box.setToolTip(tip)
                self.option_checks[key] = box
                options_grid.addWidget(box, idx // 2, idx % 2)
            controls_layout.addWidget(options_box)

            scan_box = QGroupBox("Probe Depth")
            scan_layout = QGridLayout(scan_box)
            scan_layout.addWidget(QLabel("Video packet scan limit"), 0, 0)
            self.frame_combo = QComboBox()
            self.frame_combo.addItems(["50", "300", "1000", "3000"])
            self.frame_combo.setCurrentText("300")
            scan_layout.addWidget(self.frame_combo, 0, 1)
            scan_note = QLabel("Higher counts can catch HDR side data that appears later, but take longer.")
            scan_note.setWordWrap(True)
            scan_layout.addWidget(scan_note, 1, 0, 1, 2)
            controls_layout.addWidget(scan_box)

            playback_box = QGroupBox("Playback Troubleshooting (Optional)")
            playback_layout = QGridLayout(playback_box)
            self.expected_output = QComboBox()
            self.expected_output.addItems(diagnostic_report.OUTPUTS)
            self.observed_output = QComboBox()
            self.observed_output.addItems(diagnostic_report.OUTPUTS)
            self.metadata_priority = QComboBox()
            self.metadata_priority.addItems(diagnostic_report.PRIORITIES)
            for row, (label, combo) in enumerate((
                ("Desired output", self.expected_output),
                ("Observed output", self.observed_output),
                ("Player priority", self.metadata_priority),
            )):
                playback_layout.addWidget(QLabel(label), row, 0)
                playback_layout.addWidget(combo, row, 1)
            controls_layout.addWidget(playback_box)

            actions_box = QGroupBox("Actions")
            actions_layout = QGridLayout(actions_box)
            self.check_btn = QPushButton("Check Tools")
            self.install_btn = QPushButton("Install Missing")
            self.run_btn = QPushButton("Run Probe")
            self.generate_btn = QPushButton("Generate Report")
            self.report_btn = QPushButton("Open Report")
            self.folder_btn = QPushButton("Open Folder")
            self.log_btn = QPushButton("Open Log")
            self.copy_report_btn = QPushButton("Copy Report for AI")
            self.copy_report_btn.setEnabled(False)
            self.copy_report_btn.clicked.connect(self.copy_report)
            self.generate_btn.setEnabled(False)
            self.report_btn.setEnabled(False)
            self.folder_btn.setEnabled(False)
            self.check_btn.clicked.connect(self.check_tools)
            self.install_btn.clicked.connect(self.install_missing)
            self.run_btn.clicked.connect(self.run_probe)
            self.generate_btn.clicked.connect(self.generate_report)
            self.report_btn.clicked.connect(self.open_report)
            self.folder_btn.clicked.connect(self.open_folder)
            self.log_btn.clicked.connect(self.open_log)
            for idx, button in enumerate((self.check_btn, self.install_btn, self.run_btn, self.generate_btn, self.report_btn, self.folder_btn, self.copy_report_btn, self.log_btn)):
                actions_layout.addWidget(button, idx // 2, idx % 2)
            controls_layout.addWidget(actions_box)
            controls_layout.addStretch(1)

            logs_panel = QFrame()
            logs_layout = QVBoxLayout(logs_panel)
            logs_layout.setContentsMargins(12, 0, 0, 0)
            logs_label = QLabel("Live Logs")
            logs_label.setObjectName("Title")
            logs_label.setStyleSheet("font-size: 13pt;")
            self.progress = QProgressBar()
            self.progress.setRange(0, 100)
            self.progress.setValue(0)
            self.progress.setFormat("Idle")
            self.output = QTextEdit()
            self.output.setReadOnly(True)
            self.output.setFont(QFont("Consolas", 10))
            logs_layout.addWidget(logs_label)
            logs_layout.addWidget(self.progress)
            logs_layout.addWidget(self.output, 1)
            self.tabs = QTabWidget()
            self.report_preview = QTextBrowser()
            self.report_preview.setOpenExternalLinks(False)
            self.report_preview.setMarkdown("# Your report\n\nRun a probe to see findings, filename checks, and metadata-priority guidance here.")
            self.tabs.addTab(self.report_preview, "Report")
            self.tabs.addTab(logs_panel, "Live Logs (Private)")
            splitter.addWidget(self.tabs)
            splitter.setSizes([380, 760])

        def browse(self) -> None:
            path, _ = QFileDialog.getOpenFileName(
                self,
                "Select media file",
                "",
                "Media files (*.mkv *.mp4 *.m4v *.mov *.m2ts *.mts *.ts *.webm *.avi *.wmv *.flv *.mpg *.mpeg);;All files (*.*)",
            )
            if path:
                self.file_edit.setText(path)

        def log(self, message: str, level: str = "INFO") -> None:
            LOGGER.write(message, level)
            if message:
                colors = {
                    "INFO": "#cbd5e1",
                    "WARN": "#fbbf24",
                    "ERROR": "#fb7185",
                    "SUCCESS": "#4ade80",
                    "DEBUG": "#93c5fd",
                }
                color = colors.get(level.upper(), "#cbd5e1")
                stamp = html.escape(now())
                clean = html.escape(message)
                self.output.append(f'<span style="color:#64748b">[{stamp}]</span> <span style="color:{color}; font-weight:700">[{html.escape(level.upper())}]</span> <span style="color:{color}">{clean}</span>')
            else:
                self.output.append("")
            self.statusBar().showMessage(message[:180] if message else f"Log file: {LOGGER.path}")

        def emit(self, message: str) -> None:
            self.queue.put(("log", message))

        def log_tool_inventory(self) -> None:
            inventory = get_inventory()
            installed = sum(1 for item in inventory if item["found"])
            self.log(f"Tool check: {installed}/{len(inventory)} tools ready", "INFO")
            for item in inventory:
                self.log(tool_status_line(item), tool_log_level(item))

        def set_progress(self, value: int, label: str = "") -> None:
            self.progress.setValue(max(0, min(100, int(value))))
            self.progress.setFormat(label or f"{self.progress.value()}%")

        def set_running(self, running: bool) -> None:
            self.running = running
            for button in (self.check_btn, self.install_btn, self.run_btn):
                button.setEnabled(not running)
            self.generate_btn.setEnabled((not running) and bool(self.last_probe_cache))
            if running:
                self.set_progress(0, "Starting...")

        def drain_queue(self) -> None:
            while True:
                try:
                    kind, payload = self.queue.get_nowait()
                except queue.Empty:
                    break
                if kind == "log":
                    if isinstance(payload, dict):
                        if "progress" in payload:
                            label = str(payload.get("message") or f"{payload['progress']}%")
                            self.set_progress(int(payload["progress"]), f"{payload['progress']}% - {label}")
                        if payload.get("message"):
                            self.log(str(payload.get("message", "")), str(payload.get("level", "INFO")))
                    else:
                        self.log(str(payload))
                elif kind == "done":
                    self.on_worker_done(payload)

        def check_tools(self) -> None:
            self.log("")
            self.log_tool_inventory()

        def install_missing(self) -> None:
            if self.running:
                return
            self.set_running(True)
            self.log("Installing missing tools...", "INFO")
            self.set_progress(5, "Installing tools...")

            def worker() -> None:
                try:
                    install_missing_tools(self.emit)
                    emit_event(self.emit, "Tool install/check finished.", "SUCCESS", 100)
                    self.queue.put(("done", {"ok": True, "kind": "install"}))
                except Exception as exc:
                    LOGGER.write(f"Install failed: {exc}", "ERROR")
                    self.queue.put(("done", {"ok": False, "kind": "install", "error": str(exc)}))

            threading.Thread(target=worker, daemon=True).start()

        def current_options(self) -> dict:
            return {**{key: box.isChecked() for key, box in self.option_checks.items()},
                    "ExpectedOutput": self.expected_output.currentText(),
                    "ObservedOutput": self.observed_output.currentText(),
                    "MetadataPriority": self.metadata_priority.currentText()}

        def update_report_preview(self) -> None:
            if self.last_report and self.last_report.is_file():
                self.report_preview.setMarkdown(self.last_report.read_text(encoding="utf-8"))
                self.copy_report_btn.setEnabled(True)
                self.tabs.setCurrentWidget(self.report_preview)

        def copy_report(self) -> None:
            if self.last_report and self.last_report.is_file():
                QApplication.clipboard().setText(self.last_report.read_text(encoding="utf-8"))
                self.statusBar().showMessage("Sanitized report copied. Private logs are not included.")

        def report_path_for_cache(self, cache: dict) -> Path:
            return report_path_for_source(cache["file_path"])

        def run_probe(self) -> None:
            if self.running:
                return
            source_text = self.file_edit.text().strip()
            if not source_text:
                QMessageBox.warning(self, APP_NAME, "Select a media file or paste a direct video URL first.")
                return
            remote = is_url(source_text)
            if not remote and not Path(source_text).is_file():
                QMessageBox.warning(self, APP_NAME, "Select a valid local media file or paste a direct HTTP/HTTPS video URL.")
                return
            if not required_tools_ok():
                choice = QMessageBox.question(self, APP_NAME, "ffprobe is missing. Install missing tools now?")
                if choice == QMessageBox.Yes:
                    self.install_missing()
                return

            self.set_running(True)
            self.report_btn.setEnabled(False)
            self.folder_btn.setEnabled(False)
            self.generate_btn.setEnabled(False)
            self.log("")
            self.set_progress(2, "Starting probe...")
            self.log(f"Running probe for {media_display_name(source_text)}", "INFO")
            if remote:
                self.log("Remote URL mode works best with direct media URLs. Stremio/add-on/magnet links may need to resolve to a real stream URL first.", "WARN")
            options = self.current_options()
            LOGGER.write(f"Report options: {json.dumps(options, sort_keys=True)}")
            frame_count = int(self.frame_combo.currentText() or "300")
            report_path = self.report_path_for_cache({"file_path": source_text})

            def worker() -> None:
                try:
                    cache = probe_media(source_text, frame_count, self.emit)
                    emit_event(self.emit, "Rendering Markdown report...", "INFO", 94)
                    lines, verdict = render_report(cache, options)
                    report_path.write_text("\n".join(lines), encoding="utf-8")
                    LOGGER.write(f"Report saved to {report_path}")
                    emit_event(self.emit, "Report written successfully.", "SUCCESS", 100)
                    self.queue.put(("done", {"ok": True, "kind": "probe", "report": report_path, "verdict": verdict, "cache": cache}))
                except Exception as exc:
                    LOGGER.write(f"Probe failed: {exc}", "ERROR")
                    self.queue.put(("done", {"ok": False, "kind": "probe", "error": str(exc)}))

            threading.Thread(target=worker, daemon=True).start()

        def generate_report(self) -> None:
            if self.running:
                return
            if not self.last_probe_cache:
                QMessageBox.information(self, APP_NAME, "Run a probe first, then adjust the report sections and generate again.")
                return
            options = self.current_options()
            LOGGER.write(f"Generating report from cached probe. Report options: {json.dumps(options, sort_keys=True)}")
            try:
                self.set_progress(90, "Rendering cached report...")
                report_path = self.report_path_for_cache(self.last_probe_cache)
                lines, verdict = render_report(self.last_probe_cache, options)
                report_path.write_text("\n".join(lines), encoding="utf-8")
                self.last_report = report_path
                self.update_report_preview()
                self.report_btn.setEnabled(True)
                self.folder_btn.setEnabled(True)
                self.log("")
                self.set_progress(100, "Report generated")
                self.log("Report generated from last probe.", "SUCCESS")
                self.log("Verdict:")
                self.log(verdict)
                self.log(f"Report saved to: {report_path}")
            except Exception as exc:
                LOGGER.write(f"Generate report failed: {exc}", "ERROR")
                self.log(f"Generate report failed: {exc}")

        def on_worker_done(self, payload: dict) -> None:
            self.set_running(False)
            if payload.get("ok"):
                if payload.get("kind") == "probe":
                    self.set_progress(100, "Probe complete")
                    self.last_report = Path(payload["report"])
                    self.update_report_preview()
                    self.last_probe_cache = payload.get("cache")
                    self.generate_btn.setEnabled(True)
                    self.report_btn.setEnabled(True)
                    self.folder_btn.setEnabled(True)
                    self.log("")
                    self.log("Probe complete.", "SUCCESS")
                    self.log("Verdict:")
                    self.log(payload["verdict"])
                    self.log(f"Report saved to: {self.last_report}")
                    self.log("Adjust sections and click Generate Report to re-render without probing again.")
                else:
                    self.set_progress(100, "Install complete")
                    self.log("Install complete.", "SUCCESS")
            else:
                self.set_progress(0, "Failed")
                self.log("")
                self.log(f"{payload.get('kind', 'Worker')} failed: {payload.get('error')}", "ERROR")
            self.log(f"Log file: {LOGGER.path}")

        def open_report(self) -> None:
            if self.last_report and self.last_report.is_file():
                open_path(self.last_report)

        def open_folder(self) -> None:
            if self.last_report and self.last_report.is_file():
                reveal_path(self.last_report)

        def open_log(self) -> None:
            open_path(LOGGER.path)

    LOGGER.write(f"{APP_NAME} Qt app started. Python: {sys.version}")
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    window = QtProbeApp()
    window.show()
    if "--smoke-test" in sys.argv:
        # Exercise the real packaged Qt startup without leaving a test window open.
        QTimer.singleShot(250, app.quit)
    return app.exec()


def build_arguments(source: Path) -> tuple[list[str], Path]:
    source = source.resolve()
    root = source.parent
    app_name = "MediaProber"
    args = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
            "--name", app_name, "--distpath", str(root / "dist"),
            "--workpath", str(root / "build"), "--specpath", str(root),
            "--paths", str(root)]
    if is_windows():
        args += ["--onefile", "--windowed"]
        artifact = root / "dist" / f"{app_name}.exe"
    elif is_macos():
        args += ["--onedir", "--windowed", "--osx-bundle-identifier", "com.local.mediaprober"]
        artifact = root / "dist" / f"{app_name}.app"
    else:
        args += ["--onedir"]
        artifact = root / "dist" / app_name
    return args + [str(source)], artifact


def validate_build_layout(artifact: Path) -> Path:
    """Check structural completeness; this does not certify native execution."""
    if is_macos():
        contents = artifact / "Contents"
        with (contents / "Info.plist").open("rb") as stream:
            info = plistlib.load(stream)
        name = info.get("CFBundleExecutable", "")
        if not name or "/" in name or "\\" in name or name in {".", ".."}:
            raise RuntimeError("Invalid executable name in macOS Info.plist")
        executable = contents / "MacOS" / name
        for folder in ("Frameworks", "Resources"):
            if not (contents / folder).is_dir():
                raise RuntimeError(f"Incomplete macOS bundle: Contents/{folder} is missing")
    elif is_windows():
        executable = artifact
    else:
        executable = artifact / "MediaProber"
        if not (artifact / "_internal").is_dir():
            raise RuntimeError("Incomplete Linux bundle: _internal is missing")
    if not executable.is_file() or (not is_windows() and not os.access(executable, os.X_OK)):
        raise RuntimeError(f"Packaged executable is missing or not executable: {executable}")
    if artifact.is_dir():
        for item in artifact.rglob("*"):
            if item.is_symlink() and not item.exists():
                raise RuntimeError(f"Broken bundle symlink: {item}")
    return executable


def run_build() -> int:
    if getattr(sys, "frozen", False):
        raise RuntimeError("Build from the source folder using Python, not from a packaged app.")
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("Installing PyInstaller...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--upgrade", "pyinstaller"], cwd=APP_ROOT)
    try:
        import PySide6  # noqa: F401
    except ImportError:
        print("Installing PySide6...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--upgrade", "PySide6"], cwd=APP_ROOT)

    args, artifact = build_arguments(Path(__file__))

    print(f"Building {APP_NAME} for {sys.platform}...")
    print("Build style: Windows .exe, macOS .app bundle, Linux app folder.")
    subprocess.check_call(args, cwd=APP_ROOT)
    validate_build_layout(artifact)
    print(f"Build complete: {artifact}")
    print("Run the build on each target OS to create that OS's native app.")
    return 0


if __name__ == "__main__":
    if "--build" in sys.argv:
        raise SystemExit(run_build())
    raise SystemExit(main())
