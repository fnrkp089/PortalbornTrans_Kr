"""
Portalborn 한국어 패치 원클릭 적용기

게임의 resources/app.asar 원본을 백업하고, 중국어 자리에 한국어를 넣은 app.asar로 바꾼다.
게임 안 언어 버튼에서 한국어/English를 고를 수 있다 (패치 후 기본은 한국어).
새 번역은 GitHub 릴리즈에서 받는다 (인터넷이 안 되면 패처에 든 번역으로 적용).

    패처.cmd                        창으로 실행
    패처.cmd --status               상태만 출력
    패처.cmd --apply [--game 경로]   적용 (--offline: 새 번역 확인 안 함)
    패처.cmd --restore              원본으로 복원
"""

import argparse
import hashlib
import json
import logging
import os
import re
import shutil
import ssl
import string
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import asar  # noqa: E402
import kopatch  # noqa: E402

try:
    import winreg
except ImportError:
    winreg = None

PATCHER_VERSION = "0.2.0"
GITHUB_REPO = "fnrkp089/PortalbornTrans_Kr"
GITHUB_API_LATEST = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
USER_AGENT = f"Portalborn-KR-Patcher/{PATCHER_VERSION}"
TRANSLATION_ASSET_PREFIX = "Portalborn_KR_translation-"
CHECKSUM_ASSET = "SHA256SUMS"
CA_BUNDLE = "cacert.pem"

STEAM_APP_ID = "4815720"
GAME_FOLDER_NAME = "Portalborn"
GAME_EXE = "Portalborn MUD.exe"
ASAR_RELPATH = Path("resources") / "app.asar"
STATE_FILE = "kr_patch_state.json"
BACKUP_DIR_NAME = "kr_patch_backup"
# 배포 패키지: app/translation/. 작업 폴더에서 돌릴 때는 ../translation/. 시험용으로 PB_KR_TRANSLATION 지정 가능
BUNDLED_TRANSLATION = Path(os.environ.get("PB_KR_TRANSLATION") or (
    HERE / "translation" if (HERE / "translation").is_dir() else HERE.parent / "translation"))
TRANSLATION_FILES = ("ko_strings.json", "code_patches.json", "ko_runtime.js", "ko_fonts.css")

UI_FONT_FILE = "Maplestory Bold.ttf"
UI_FONT_FAMILY = "메이플스토리"
UI_FONT_FALLBACK = "맑은 고딕"
# 배율 100% 기준 창 크기와, 이보다 낮으면 좁은 화면용 배치로 바꾸는 높이 (UMPC 등)
WINDOW_SIZE = (780, 680)
COMPACT_BELOW = 600

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("patcher")


class PatchError(RuntimeError):
    pass


def parse_version(ver):
    cleaned = re.sub(r"^[a-zA-Z_-]*v?", "", (ver or "").strip())
    parts = []
    for p in cleaned.split("."):
        m = re.match(r"\d+", p)
        parts.append(int(m.group(0)) if m else 0)
    return tuple(parts) if parts else (0,)


def data_dir():
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    return Path(base) / "PortalbornKr"


# ━━━ 1. 게임 폴더 찾기 ━━━

def find_steam_install_path():
    if winreg is None:
        return None
    for hive, subkey in [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam"),
        (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
    ]:
        try:
            with winreg.OpenKey(hive, subkey) as key:
                val, _ = winreg.QueryValueEx(key, "InstallPath" if hive == winreg.HKEY_LOCAL_MACHINE else "SteamPath")
                if val and Path(val).exists():
                    return str(Path(val))
        except OSError:
            continue
    return None


def parse_vdf_library_folders(steam_path):
    folders = [steam_path]
    for vdf in [Path(steam_path) / "steamapps" / "libraryfolders.vdf", Path(steam_path) / "config" / "libraryfolders.vdf"]:
        if not vdf.exists():
            continue
        try:
            for m in re.finditer(r'"path"\s+"([^"]+)"', vdf.read_text(encoding="utf-8", errors="replace")):
                p = m.group(1).replace("\\\\", "\\")
                if Path(p).exists() and p not in folders:
                    folders.append(p)
        except OSError:
            pass
        break
    return folders


def read_acf_value(acf_path, key):
    try:
        m = re.search(rf'"{key}"\s+"([^"]+)"', acf_path.read_text(encoding="utf-8", errors="replace"))
        return m.group(1) if m else None
    except OSError:
        return None


def is_game_folder(game):
    return bool(game) and (Path(game) / ASAR_RELPATH).is_file()


def _guess_library_roots():
    """레지스트리가 없을 때: 드라이브마다 흔한 스팀 라이브러리 위치"""
    roots = []
    if sys.platform != "win32":
        return roots
    for letter in string.ascii_uppercase[2:]:
        drive = Path(f"{letter}:\\")
        try:
            if not drive.exists():
                continue
        except OSError:
            continue
        for sub in ("SteamLibrary", "Steam", "Games\\Steam", "Program Files (x86)\\Steam", "Program Files\\Steam",
                    "Games\\SteamLibrary"):
            roots.append(drive / sub)
    return roots


def find_game_path():
    libraries = []
    steam = find_steam_install_path()
    if steam:
        libraries = parse_vdf_library_folders(steam)
        for lib in libraries:
            steamapps = Path(lib) / "steamapps"
            manifest = steamapps / f"appmanifest_{STEAM_APP_ID}.acf"
            if manifest.exists():
                game = steamapps / "common" / (read_acf_value(manifest, "installdir") or GAME_FOLDER_NAME)
                if is_game_folder(game):
                    return str(game)
    for root in [Path(p) for p in libraries] + _guess_library_roots():
        game = root / "steamapps" / "common" / GAME_FOLDER_NAME
        try:
            if is_game_folder(game):
                return str(game)
        except OSError:
            continue
    return None


def steam_buildid(game):
    manifest = Path(game).parent.parent / f"appmanifest_{STEAM_APP_ID}.acf"
    return read_acf_value(manifest, "buildid") if manifest.exists() else None


def game_running():
    if sys.platform != "win32":
        return False
    try:
        out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {GAME_EXE}", "/NH", "/FO", "CSV"], capture_output=True,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=15).stdout or b""
        return GAME_EXE.lower().encode() in out.lower()
    except (OSError, subprocess.SubprocessError):
        return False


# ━━━ 2. 번역 패키지 (패처에 든 것 / GitHub에서 받은 것) ━━━

def make_ssl_context():
    """Windows 인증서 저장소 + 패키지에 넣은 루트 인증서 묶음(cacert.pem, Mozilla)"""
    ctx = ssl.create_default_context()
    bundle = HERE / CA_BUNDLE
    if bundle.is_file():
        try:
            ctx.load_verify_locations(cafile=str(bundle))
        except (OSError, ssl.SSLError):
            pass
    return ctx


def _http_get(url, timeout=20, tries=3):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"})
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=make_ssl_context()) as resp:
                return resp.read()
        except urllib.error.HTTPError:
            raise                                   # 404 등은 다시 해도 같음
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            if attempt == tries - 1:
                raise
            time.sleep(1 + attempt)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def package_version(folder):
    try:
        return json.loads((Path(folder) / "ko_strings.json").read_text(encoding="utf-8")).get("meta", {}).get("version")
    except (OSError, ValueError):
        return None


def downloaded_packages():
    root = data_dir() / "translation"
    found = []
    if root.is_dir():
        for d in root.iterdir():
            if d.is_dir() and all((d / n).is_file() for n in TRANSLATION_FILES[:2]):
                found.append((package_version(d) or d.name, d))
    return found


def best_local_package():
    """(버전, 폴더): 패처에 든 것과 받아 둔 것 중 가장 새것"""
    cands = [(package_version(BUNDLED_TRANSLATION) or "v0", BUNDLED_TRANSLATION)] + downloaded_packages()
    return max(cands, key=lambda x: parse_version(x[0]))


def latest_release_info():
    """GitHub 최신 릴리즈의 번역 패키지 정보. 없거나 인터넷이 안 되면 None"""
    rel = json.loads(_http_get(GITHUB_API_LATEST).decode("utf-8"))
    assets = {a["name"]: a for a in rel.get("assets", [])}
    zip_asset = next((a for n, a in assets.items() if n.startswith(TRANSLATION_ASSET_PREFIX) and n.endswith(".zip")), None)
    if not zip_asset:
        return None
    return {"tag": rel.get("tag_name"), "name": rel.get("name") or rel.get("tag_name"), "body": rel.get("body") or "",
            "zip": zip_asset, "sums": assets.get(CHECKSUM_ASSET)}


def release_highlights(body, limit=12):
    """릴리즈 노트의 첫 '## ' 섹션(주요 작업) 줄만"""
    lines = (body or "").replace("\r\n", "\n").split("\n")
    starts = [i for i, line in enumerate(lines) if line.startswith("## ")]
    if starts:
        lines = lines[starts[0] + 1:(starts[1] if len(starts) > 1 else len(lines))]
    out = []
    for line in lines:
        t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", line.strip()).replace("**", "").replace("`", "")
        if t and t != "---":
            out.append("• " + t[2:] if t.startswith(("- ", "* ")) else t)
    return out[:limit]


def download_translation(info, say=print):
    """릴리즈의 번역 zip을 받아 해시 확인 후 풀어 둠. 폴더 반환"""
    if not info.get("sums"):
        raise PatchError("릴리즈에 해시 목록(SHA256SUMS)이 없어 새 번역을 쓰지 않습니다.")
    sums = {}
    for line in _http_get(info["sums"]["browser_download_url"]).decode("utf-8").splitlines():
        parts = line.split()
        if len(parts) >= 2:
            sums[parts[-1].lstrip("*")] = parts[0].lower()
    name = info["zip"]["name"]
    if name not in sums:
        raise PatchError("해시 목록에 번역 파일이 없습니다.")
    dest_root = data_dir() / "translation"
    dest_root.mkdir(parents=True, exist_ok=True)
    say(f"새 번역 {info['tag']} 받는 중...")
    with tempfile.TemporaryDirectory(dir=dest_root) as tmp:
        zpath = Path(tmp) / name
        zpath.write_bytes(_http_get(info["zip"]["browser_download_url"], timeout=120))
        if sha256_file(zpath) != sums[name]:
            raise PatchError("받은 번역 파일이 손상되었습니다 (해시 불일치). 잠시 후 다시 시도해 주세요.")
        out = Path(tmp) / "pkg"
        with zipfile.ZipFile(zpath) as z:
            for m in z.infolist():
                n = m.filename.replace("\\", "/")
                if n.endswith("/"):
                    continue
                base = n.rsplit("/", 1)[-1]
                if base not in TRANSLATION_FILES:
                    continue
                out.mkdir(exist_ok=True)
                (out / base).write_bytes(z.read(m))
        if not all((out / n).is_file() for n in TRANSLATION_FILES[:2]):
            raise PatchError("번역 파일 구성이 올바르지 않습니다.")
        kopatch.Package(out)          # 읽히는지 확인
        final = dest_root / re.sub(r"[^\w.\-]", "_", info["tag"] or "latest")
        if final.exists():
            shutil.rmtree(final)
        shutil.move(str(out), str(final))
    return final


# ━━━ 3. 상태·백업 ━━━

def asar_game_info(path):
    """asar 안 build-info.json의 게임 버전·빌드"""
    try:
        info = json.loads(asar.Asar(path).read("build-info.json").decode("utf-8"))
        return {"version": info.get("version"), "build": info.get("buildId")}
    except (KeyError, ValueError, OSError, asar.AsarError):
        return {"version": None, "build": None}


class PatchState:
    def __init__(self, game):
        self.game = Path(game)
        self.path = self.game / BACKUP_DIR_NAME / STATE_FILE
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {}

    @property
    def backup(self):
        return self.game / BACKUP_DIR_NAME / "app.asar"

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def clear(self):
        self.data = {}
        try:
            self.path.unlink()
        except OSError:
            pass


def inspect(game):
    """화면에 보여 줄 현재 상태"""
    game = Path(game)
    cur = game / ASAR_RELPATH
    st = PatchState(game)
    res = {"game": str(game), "exists": cur.is_file(), "patched": False, "updated_since": False, "state": st.data}
    if not cur.is_file():
        return res
    size = cur.stat().st_size
    # 크기가 기록과 같을 때만 해시 계산 (150MB라 1초쯤 걸림)
    if st.data.get("patched_size") == size and sha256_file(cur) == st.data.get("patched_hash"):
        res["patched"] = True
        res["game_info"] = {"version": st.data.get("game_version"), "build": st.data.get("game_build")}
    else:
        res["game_info"] = asar_game_info(cur)
        res["updated_since"] = bool(st.data)      # 기록은 있는데 파일이 다름 = 스팀이 게임을 업데이트함
    return res


def _copy_with_check(src, dst, expected_hash=None):
    tmp = Path(str(dst) + ".tmp")
    shutil.copyfile(src, tmp)
    if expected_hash and sha256_file(tmp) != expected_hash:
        tmp.unlink()
        raise PatchError(f"복사본이 원본과 다름: {dst}")
    os.replace(tmp, dst)


def apply_patch(game, package, progress=None, say=print):
    """한국어 패치 적용. 보고서 반환"""
    game = Path(game)
    cur = game / ASAR_RELPATH
    if not cur.is_file():
        raise PatchError(f"게임 파일이 없습니다: {cur}")
    if game_running():
        raise PatchError("게임이 실행 중입니다. 게임을 끄고 다시 눌러 주세요.")
    st = PatchState(game)
    backup = st.backup
    try:
        backup.parent.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        raise PatchError("게임 폴더에 쓸 수 없습니다. 패처를 마우스 오른쪽 버튼 → 관리자 권한으로 실행해 주세요.")

    say("현재 게임 파일 확인 중...")
    cur_hash = sha256_file(cur)
    if st.data.get("patched_hash") == cur_hash and backup.is_file():
        say("이미 패치된 상태 → 백업해 둔 원본에서 다시 만듭니다.")
        if sha256_file(backup) != st.data.get("original_hash"):
            raise PatchError("백업한 원본이 손상되었습니다. 스팀에서 '게임 파일 무결성 검사'를 한 뒤 다시 적용해 주세요.")
        original_hash = st.data["original_hash"]
    else:
        info = asar_game_info(cur)
        if info["version"] is None:
            raise PatchError("게임 파일을 읽을 수 없습니다. 스팀에서 '게임 파일 무결성 검사'를 해 주세요.")
        say(f"원본 백업 중... (게임 {info['version']})")
        _copy_with_check(cur, backup, cur_hash)
        original_hash = cur_hash
        st.data = {"original_hash": cur_hash, "original_size": cur.stat().st_size,
                   "game_version": info["version"], "game_build": info["build"]}
        st.save()

    say("한국어 패치 만드는 중...")
    tmp = game / "resources" / "app.asar.kr_tmp"
    report = kopatch.build_patched_asar(backup, package, tmp, progress=progress)
    say("게임 파일 교체 중...")
    try:
        os.replace(tmp, cur)
    except PermissionError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise PatchError("게임 파일을 바꿀 수 없습니다. 게임이 꺼져 있는지 확인하고 다시 눌러 주세요.")
    st.data.update({
        "patched_hash": sha256_file(cur), "patched_size": cur.stat().st_size, "original_hash": original_hash,
        "patch_version": package.meta.get("version"), "patch_game_build": package.meta.get("game_build"),
        "patcher_version": PATCHER_VERSION, "steam_buildid": steam_buildid(game),
        "date": datetime.now().strftime("%Y-%m-%d %H:%M"), "report": report.summary(),
    })
    st.save()
    return report


def restore(game, say=print):
    game = Path(game)
    cur = game / ASAR_RELPATH
    if game_running():
        raise PatchError("게임이 실행 중입니다. 게임을 끄고 다시 눌러 주세요.")
    st = PatchState(game)
    if not st.data:
        return "패치 기록이 없습니다 (이미 원본)."
    cur_hash = sha256_file(cur) if cur.is_file() else None
    if cur_hash == st.data.get("patched_hash"):
        if not st.backup.is_file() or sha256_file(st.backup) != st.data.get("original_hash"):
            raise PatchError("백업한 원본이 없거나 손상되었습니다. 스팀에서 '게임 파일 무결성 검사'를 하면 원래대로 돌아갑니다.")
        say("원본으로 되돌리는 중...")
        _copy_with_check(st.backup, cur, st.data["original_hash"])
        msg = "원본으로 복원했습니다."
    else:
        msg = "게임이 이미 원본 상태입니다 (스팀 업데이트로 바뀜). 기록만 지웠습니다."
    st.clear()
    try:
        st.backup.unlink()
        st.backup.parent.rmdir()
    except OSError:
        pass
    return msg


def describe_report(report, version=None):
    s = report.summary() if hasattr(report, "summary") else report
    lines = [f"번역 {version} 적용: 문장 {s['translated']:,}곳을 한국어로 바꿨습니다." if version
             else f"문장 {s['translated']:,}곳을 한국어로 바꿨습니다."]
    if s["missing"]:
        lines.append(f"번역이 없는 문장 {s['missing']:,}개는 중국어로 나올 수 있습니다 (게임이 번역보다 새 버전이면 생김).")
    if s["code_patches_skipped"]:
        lines.append(f"게임 코드가 바뀌어 적용 못 한 보정 {len(s['code_patches_skipped'])}개: "
                     + ", ".join(x.split(':')[0] for x in s["code_patches_skipped"]))
    return "\n".join(lines)


# ━━━ 4. 창 ━━━

def enable_dpi_awareness():
    """Windows 화면 배율을 패처가 직접 따름 (안 하면 글씨가 흐리고 화면 크기를 작게 알려 줌)"""
    if sys.platform != "win32":
        return
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


def load_ui_font():
    """패키지의 창 글꼴을 이 프로세스에서만 쓰게 등록 (PC에 설치하지 않음)"""
    source = HERE / UI_FONT_FILE
    if sys.platform != "win32" or not source.is_file():
        return False
    import ctypes
    try:
        target = data_dir() / "ui" / UI_FONT_FILE
        if not (target.is_file() and target.stat().st_size == source.stat().st_size):
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        return bool(ctypes.windll.gdi32.AddFontResourceExW(str(target), 0x10, 0))  # FR_PRIVATE
    except OSError:
        return False


def screen_work_area(root):
    """창을 둘 수 있는 화면 영역 (왼쪽, 위, 너비, 높이). 작업 표시줄 제외. 시험용: PB_KR_FAKE_AREA=1280x720"""
    fake = os.environ.get("PB_KR_FAKE_AREA")
    if fake:
        w, h = (int(v) for v in fake.lower().split("x"))
        return 0, 0, w, h
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes
            rect = wintypes.RECT()
            if ctypes.windll.user32.SystemParametersInfoW(0x30, 0, ctypes.byref(rect), 0):  # SPI_GETWORKAREA
                return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top
        except (AttributeError, OSError):
            pass
    return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight()


def ui_scale(root):
    fake = os.environ.get("PB_KR_FAKE_SCALE")
    if fake:
        return float(fake)
    return max(1.0, root.winfo_fpixels("1i") / 96)


def fit_window(area_w, area_h, scale):
    """화면 안에 다 들어오는 창 (너비, 높이, 좁은 화면 배치 여부). 제목 표시줄·테두리 몫을 뺌"""
    frame_w, frame_h = int(16 * scale), int(40 * scale)
    width = min(int(WINDOW_SIZE[0] * scale), area_w - frame_w)
    height = min(int(WINDOW_SIZE[1] * scale), area_h - frame_h)
    return width, height, height < int(COMPACT_BELOW * scale)


def run_gui():
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
    from tkinter import font as tkfont

    class App:
        BG, BG2, FG, ACCENT, WARN, OK, ENTRY = "#0b0f14", "#121a22", "#d8e6e6", "#00e5ff", "#ffb000", "#33ff99", "#16222c"

        def __init__(self, root, family):
            self.root, self.family = root, family
            self.scale = ui_scale(root)
            left, top, aw, ah = screen_work_area(root)
            w, h, self.compact = fit_window(aw, ah, self.scale)
            fw, fh = int(16 * self.scale), int(40 * self.scale)
            root.geometry(f"{w}x{h}+{left + max(0, (aw - w - fw) // 2)}+{top + max(0, (ah - h - fh) // 2)}")
            root.minsize(min(w, self.px(560)), min(h, self.px(420)))
            root.title(f"Portalborn 한국어 패치 v{PATCHER_VERSION}")
            root.configure(bg=self.BG)
            root.protocol("WM_DELETE_WINDOW", self.on_close)
            self.game = tk.StringVar()
            self.status = tk.StringVar(value="대기 중")
            self.busy = False
            self.remote = None          # 최신 릴리즈 정보 (None: 확인 전/실패)
            self.local_ver, self.local_dir = best_local_package()
            self.build(w)
            self.detect()
            self.check_update()

        def px(self, v):
            return int(round(v * self.scale))

        def f(self, size, bold=False):
            size += 0 if self.compact else 1
            return (self.family, size, "bold" if bold or self.family == UI_FONT_FAMILY else "normal")

        def build(self, width):
            px, f, compact = self.px, self.f, self.compact
            pad, gap = px(14 if compact else 22), px(3 if compact else 7)
            s = ttk.Style()
            s.theme_use("clam")
            s.configure("T.TLabel", background=self.BG, foreground=self.ACCENT, font=f(13 if compact else 17, True))
            s.configure("S.TLabel", background=self.BG, foreground="#7f9aa0", font=f(9))
            s.configure("I.TLabel", background=self.BG2, foreground=self.FG, font=f(10))
            s.configure("W.TLabel", background=self.BG2, foreground=self.WARN, font=f(10))
            s.configure("G.TLabel", background=self.BG2, foreground=self.OK, font=f(10))
            s.configure("St.TLabel", background=self.BG, foreground="#9fb4b8", font=f(9))
            s.configure("TButton", font=f(10))
            s.configure("Big.TButton", font=f(11 if compact else 12, True))
            s.configure("TProgressbar", troughcolor=self.ENTRY, background=self.ACCENT, thickness=px(10 if compact else 14))

            top = tk.Frame(self.root, bg=self.BG)
            top.pack(fill="x", padx=pad, pady=(px(10 if compact else 18), gap))
            title = ttk.Label(top, text="🌀  Portalborn 한국어 패치", style="T.TLabel")
            self.lbl_ver = ttk.Label(top, text="", style="S.TLabel")
            if compact:
                title.pack(side="left")
                self.lbl_ver.pack(side="left", padx=(px(10), 0), anchor="s")
            else:
                title.pack(anchor="w")
                self.lbl_ver.pack(anchor="w")
            self.show_versions()

            fp = tk.Frame(self.root, bg=self.BG)
            fp.pack(fill="x", padx=pad, pady=gap)
            if not compact:
                ttk.Label(fp, text="게임 폴더", style="S.TLabel").pack(anchor="w")
            row = tk.Frame(fp, bg=self.BG)
            row.pack(fill="x", pady=(px(2), 0))
            tk.Entry(row, textvariable=self.game, font=f(10), bg=self.ENTRY, fg=self.FG, insertbackground=self.FG,
                     relief="flat", bd=4).pack(side="left", fill="x", expand=True)
            ttk.Button(row, text="게임 폴더 찾기" if compact else "찾기", command=self.browse).pack(side="right", padx=(px(6), 0))

            box = tk.Frame(self.root, bg=self.BG2, bd=1, relief="solid")
            box.pack(fill="x", padx=pad, pady=gap)
            inner = px(3 if compact else 7)
            self.lbl_state = ttk.Label(box, text="📦 상태 확인 중...", style="I.TLabel")
            self.lbl_state.pack(anchor="w", padx=px(10), pady=(inner, 0))
            self.lbl_game = ttk.Label(box, text="", style="I.TLabel")
            self.lbl_game.pack(anchor="w", padx=px(10))
            self.lbl_note = ttk.Label(box, text="", style="W.TLabel", wraplength=width - 2 * pad - px(24), justify="left")
            self.lbl_note.pack(anchor="w", padx=px(10), pady=(0, inner))

            # 적용·복원 버튼은 맨 아래에 먼저 자리를 잡음: 창이 작으면 버튼이 아니라 안내 칸이 줄어듦
            fb = tk.Frame(self.root, bg=self.BG)
            fb.pack(side="bottom", fill="x", padx=pad, pady=(0, px(10 if compact else 18)))
            self.btn_apply = ttk.Button(fb, text="🚀  한국어 패치 적용", style="Big.TButton", command=self.on_apply)
            self.btn_apply.pack(side="left", fill="x", expand=True, ipady=px(4 if compact else 8))
            self.btn_restore = ttk.Button(fb, text="↩  원본 복원", command=self.on_restore)
            self.btn_restore.pack(side="right", padx=(px(8), 0), ipady=px(4 if compact else 8))

            fpr = tk.Frame(self.root, bg=self.BG)
            fpr.pack(side="bottom", fill="x", padx=pad, pady=(0, gap))
            self.progress = ttk.Progressbar(fpr, mode="determinate")
            self.progress.pack(fill="x")
            ttk.Label(fpr, textvariable=self.status, style="St.TLabel").pack(anchor="w", pady=(px(2), 0))

            fl = tk.Frame(self.root, bg=self.BG)
            fl.pack(fill="both", expand=True, padx=pad, pady=(gap, gap))
            self.log_text = tk.Text(fl, height=3, bg=self.ENTRY, fg="#8fb0b5", font=f(9), relief="flat", bd=6,
                                    wrap="word", state="disabled", cursor="arrow", spacing1=1, spacing3=1)
            scroll = ttk.Scrollbar(fl, orient="vertical", command=self.log_text.yview)
            self.log_text.configure(yscrollcommand=scroll.set)
            scroll.pack(side="right", fill="y")
            self.log_text.pack(fill="both", expand=True)
            self.log_text.tag_configure("head", foreground=self.ACCENT, font=f(10, True))
            self.log("• 패치 후 게임을 켜면 한국어로 나옵니다. 화면 위쪽 🌐 언어 버튼으로 English와 바꿀 수 있습니다.")
            self.log("• 스팀이 게임을 업데이트하면 패치가 풀립니다. 그때 이 패처로 다시 적용하세요.")

        def show_versions(self):
            remote = self.remote["tag"] if self.remote else None
            text = f"패처 v{PATCHER_VERSION}  ·  번역 {self.local_ver}"
            if remote and parse_version(remote) > parse_version(self.local_ver):
                text += f"  →  새 번역 {remote}"
            self.lbl_ver.configure(text=text)

        def log(self, msg, tag=None):
            self.log_text.configure(state="normal")
            self.log_text.insert("end", msg + "\n", tag)
            self.log_text.see("end")
            self.log_text.configure(state="disabled")

        def set_status(self, msg):
            self.root.after(0, self.status.set, msg)

        def set_progress(self, cur, tot, _name=""):
            self.root.after(0, lambda: self.progress.configure(value=cur / max(tot, 1) * 100))

        def detect(self):
            g = find_game_path()
            if g:
                self.game.set(g)
                self.status.set("스팀에서 게임 폴더를 찾았습니다.")
            else:
                self.status.set("게임 폴더를 찾지 못했습니다. [찾기]로 Portalborn 폴더를 골라 주세요.")
            self.refresh()

        def check_update(self):
            def work():
                try:
                    info = latest_release_info()
                except (urllib.error.URLError, OSError, ValueError, ssl.SSLError) as e:
                    log.info("새 번역 확인 실패: %s", e)
                    info = None
                self.root.after(0, self.on_remote, info)
            threading.Thread(target=work, daemon=True).start()

        def on_remote(self, info):
            self.remote = info
            self.show_versions()
            if info and parse_version(info["tag"]) > parse_version(self.local_ver):
                self.log(f"📢 새 번역 {info['name']} — 적용을 누르면 받아서 적용합니다.", "head")
                for line in release_highlights(info["body"], 8 if self.compact else 12):
                    self.log("   " + line)
            self.refresh()

        def browse(self):
            d = filedialog.askdirectory(title="Portalborn 게임 폴더 선택 (Portalborn MUD.exe가 있는 곳)")
            if d:
                self.game.set(d)
                self.refresh()

        def refresh(self):
            g = self.game.get().strip()
            if not is_game_folder(g):
                self.lbl_state.configure(text="📦 게임 폴더가 아닙니다", style="W.TLabel")
                self.lbl_game.configure(text="Portalborn MUD.exe 와 resources\\app.asar 가 있는 폴더를 골라 주세요.")
                self.lbl_note.configure(text="")
                return
            self.lbl_state.configure(text="📦 상태 확인 중...", style="I.TLabel")

            def work():
                info = inspect(g)
                self.root.after(0, self.show_state, info)
            threading.Thread(target=work, daemon=True).start()

        def show_state(self, info):
            gi = info.get("game_info") or {}
            st = info["state"]
            newest = self.remote["tag"] if self.remote and parse_version(self.remote["tag"]) > parse_version(self.local_ver) else self.local_ver
            note = ""
            if info["patched"]:
                applied = st.get("patch_version") or "?"
                self.lbl_state.configure(text=f"✅ 한국어 패치 적용됨 (번역 {applied}, {st.get('date', '')})", style="G.TLabel")
                if parse_version(newest) > parse_version(applied):
                    note = f"새 번역 {newest}이 있습니다. [한국어 패치 적용]을 다시 누르면 바뀝니다."
            elif info["updated_since"]:
                self.lbl_state.configure(text="⚠ 게임이 업데이트되어 패치가 풀렸습니다 — 다시 적용하세요", style="W.TLabel")
            else:
                self.lbl_state.configure(text="⬜ 패치 안 됨 (원본)", style="I.TLabel")
            self.lbl_game.configure(text=f"게임 버전 {gi.get('version') or '?'}  (빌드 {gi.get('build') or '?'})", style="I.TLabel")
            if not note:
                try:
                    pkg_build = kopatch.Package(self.local_dir).meta.get("game_build")
                except Exception:  # noqa: BLE001
                    pkg_build = None
                if pkg_build and gi.get("build") and str(gi["build"]) > str(pkg_build) and newest == self.local_ver:
                    note = "게임이 번역보다 새 버전입니다. 새로 생긴 문장은 번역이 나올 때까지 중국어로 나올 수 있습니다."
            self.lbl_note.configure(text=note)

        def lock(self, on):
            self.busy = on
            for b in (self.btn_apply, self.btn_restore):
                b.configure(state="disabled" if on else "normal")

        def run_task(self, fn, done):
            self.lock(True)
            self.progress.configure(value=0)

            def work():
                try:
                    res = fn()
                    self.root.after(0, done, res, None)
                except Exception as e:  # noqa: BLE001
                    log.exception("작업 실패")
                    self.root.after(0, done, None, e)
            threading.Thread(target=work, daemon=True).start()

        def on_apply(self):
            g = self.game.get().strip()
            if not is_game_folder(g):
                messagebox.showwarning("게임 폴더", "Portalborn 게임 폴더를 먼저 골라 주세요.")
                return
            started = time.monotonic()
            remote = self.remote

            def fn():
                ver, folder = self.local_ver, self.local_dir
                if remote and parse_version(remote["tag"]) > parse_version(ver):
                    try:
                        folder, ver = download_translation(remote, say=self.set_status), remote["tag"]
                    except (PatchError, urllib.error.URLError, OSError, ValueError, ssl.SSLError) as e:
                        self.root.after(0, self.log, f"⚠ 새 번역을 받지 못해 지금 번역({ver})으로 적용합니다: {e}")
                package = kopatch.Package(folder)
                report = apply_patch(g, package, progress=self.set_progress, say=self.set_status)
                return report, ver, folder

            def done(res, err):
                self.lock(False)
                if err:
                    self.status.set("실패")
                    self.log(f"❌ {err}")
                    messagebox.showerror("패치 실패", str(err))
                else:
                    report, ver, folder = res
                    self.local_ver, self.local_dir = ver, folder
                    self.show_versions()
                    self.progress.configure(value=100)
                    self.status.set(f"완료 ({time.monotonic() - started:.0f}초)")
                    text = describe_report(report, ver)
                    self.log("✅ " + text.replace("\n", "\n   "))
                    messagebox.showinfo("패치 완료", text + "\n\n게임을 켜 보세요!")
                self.refresh()
            self.run_task(fn, done)

        def on_restore(self):
            g = self.game.get().strip()
            if not is_game_folder(g):
                return
            if not messagebox.askyesno("원본 복원", "한국어 패치를 지우고 원본으로 되돌릴까요?"):
                return

            def done(msg, err):
                self.lock(False)
                if err:
                    self.log(f"❌ {err}")
                    messagebox.showerror("복원 실패", str(err))
                else:
                    self.status.set(msg)
                    self.log("↩ " + msg)
                self.refresh()
            self.run_task(lambda: restore(g, say=self.set_status), done)

        def on_close(self):
            if self.busy:
                messagebox.showwarning("작업 중", "게임 파일을 바꾸는 중입니다. 끝날 때까지 기다려 주세요.")
                return
            self.root.destroy()

    enable_dpi_awareness()
    has_font = load_ui_font()
    root = tk.Tk()
    if os.environ.get("PB_KR_FAKE_SCALE"):        # 시험용: 화면 배율 흉내 (글꼴 크기도 같이)
        root.tk.call("tk", "scaling", float(os.environ["PB_KR_FAKE_SCALE"]) * 96 / 72)
    family = UI_FONT_FAMILY if has_font and UI_FONT_FAMILY in tkfont.families(root) else UI_FONT_FALLBACK
    App(root, family)
    root.mainloop()


# ━━━ 5. 명령줄 ━━━

def run_cli(args):
    game = args.game or find_game_path()
    if not is_game_folder(game):
        print("게임 폴더를 찾지 못했습니다. --game 으로 지정하세요.")
        return 2
    try:
        if args.status:
            info = inspect(game)
            ver, folder = best_local_package()
            info["translation"] = {"version": ver, "folder": str(folder)}
            print(json.dumps(info, ensure_ascii=False, indent=2))
        elif args.apply:
            ver, folder = best_local_package()
            if not args.offline:
                try:
                    remote = latest_release_info()
                    if remote and parse_version(remote["tag"]) > parse_version(ver):
                        folder, ver = download_translation(remote), remote["tag"]
                except (PatchError, urllib.error.URLError, OSError, ValueError, ssl.SSLError) as e:
                    print(f"새 번역 확인 실패 (지금 번역으로 적용): {e}")
            rep = apply_patch(game, kopatch.Package(folder), progress=None, say=print)
            print(describe_report(rep, ver))
            print(json.dumps(rep.summary(), ensure_ascii=False, indent=1))
        elif args.restore:
            print(restore(game))
    except PatchError as e:
        print(f"실패: {e}")
        return 1
    return 0


def main():
    ap = argparse.ArgumentParser(description="Portalborn 한국어 패치")
    ap.add_argument("--game", help="게임 폴더")
    ap.add_argument("--offline", action="store_true", help="새 번역을 확인하지 않음")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--apply", action="store_true")
    g.add_argument("--restore", action="store_true")
    g.add_argument("--status", action="store_true")
    args = ap.parse_args()
    if args.apply or args.restore or args.status:
        sys.exit(run_cli(args))
    run_gui()


if __name__ == "__main__":
    main()
