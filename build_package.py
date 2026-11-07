"""
패처 배포 패키지 생성 (PyInstaller 미사용, 라스트에포크 패처와 같은 방식)

python.org가 배포하는 Windows용 Python을 받아 해시를 확인하고, 패처 소스(.py)와 번역 파일을 그대로 옆에 둔 폴더를 만듦.
실행 파일(exe/dll/pyd)은 모두 Python Software Foundation·Microsoft 서명본이고 직접 만든 실행 파일은 없음.

    python build_package.py            # dist/Portalborn_KR_Patcher/ 와 zip 생성
    python build_package.py --out 폴더
"""

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent

PYTHON_VERSION = "3.14.7"
PYTHON_URL = f"https://www.python.org/ftp/python/{PYTHON_VERSION}/python-{PYTHON_VERSION}-amd64.zip"
PYTHON_SHA256 = "ac1a727a71738e11de80b76e975f9b8a258aea6412bfc31696b929d59c6aafd0"
PYTHON_TAG = "python" + "".join(PYTHON_VERSION.split(".")[:2])

# 루트 인증서 묶음 (Mozilla 목록, certifi 배포본에서 cacert.pem만 꺼냄) — 새 번역을 GitHub에서 받을 때 씀
CERTIFI_URL = ("https://files.pythonhosted.org/packages/0b/a7/71ac2cff56fec219ed242bb11b8efb69fcc4bec75db06fb7bfe35de520e6/"
               "certifi-2026.7.22-py3-none-any.whl")
CERTIFI_SHA256 = "62f22742b58a1a33014a2b6b706588a8d7e2a88ae7bd1a6ebe8c992928483775"
CA_BUNDLE = "cacert.pem"

PACKAGE_NAME = "Portalborn_KR_Patcher"
TRANSLATION_ZIP_PREFIX = "Portalborn_KR_translation-"
APP_FILES = ["patcher.py", "kopatch.py", "jstok.py", "asar.py", "Maplestory Bold.ttf", "Maplestory-LICENSE.txt"]
TRANSLATION_FILES = ["ko_strings.json", "code_patches.json", "ko_runtime.js", "ko_fonts.css"]

# 패처가 쓰지 않는 런타임 구성요소
PRUNE_ROOT = ["Doc", "include", "libs", "Scripts", "__install__.json"]
PRUNE_LIB = ["test", "idlelib", "turtledemo", "ensurepip", "site-packages", "venv", "pydoc_data", "__phello__"]
ZIP_DATE = (2020, 1, 1, 0, 0, 0)

LAUNCHER = r"""@echo off
if not exist "%~dp0runtime\pythonw.exe" goto missing
if not "%~1"=="" goto cli
start "" "%~dp0runtime\pythonw.exe" -B "%~dp0app\patcher.py"
exit /b
:cli
"%~dp0runtime\python.exe" -B "%~dp0app\patcher.py" %*
exit /b %errorlevel%
:missing
chcp 65001 >nul
echo 압축을 먼저 풀고, 푼 폴더 안에서 실행해주세요.
pause
"""

README = """Portalborn 한국어 패치 {patch} (패처 {version})

사용법
  1. 게임을 끈다.
  2. Portalborn_KR_Patcher.cmd 더블클릭 (압축을 푼 폴더 안에서)
  3. [한국어 패치 적용] → 끝. 게임을 켜면 한국어로 나옵니다.
     화면의 🌐 언어 버튼으로 English와 바꿀 수 있습니다.
  * 스팀이 게임을 업데이트하면 패치가 풀립니다. 그때 다시 적용하세요.
  * 되돌리기: 패처의 [원본 복원], 또는 스팀 > 속성 > 설치된 파일 > 게임 파일 무결성 검사

번역 기준 게임 버전: {game_version} (빌드 {game_build})

구성
  Portalborn_KR_Patcher.cmd  실행용 배치 파일 (메모장으로 열어볼 수 있음)
  app\\                       패처 소스 (.py, 메모장으로 열어볼 수 있음), 번역 파일(translation\\),
                             루트 인증서 목록 (cacert.pem, Mozilla — 새 번역을 GitHub에서 받을 때),
                             패처 창 글꼴 (메이플스토리 서체, 저작권 안내는 Maplestory-LICENSE.txt)
  runtime\\                   Python {python} 공식 배포본 (python.org, Python Software Foundation 서명)

직접 만든 실행 파일(exe)은 들어 있지 않습니다.
게임 파일은 resources\\app.asar 하나만 바꾸며, 원본은 게임 폴더의 kr_patch_backup\\ 에 보관합니다.
이 패처 창에는 메이플스토리가 제공한 메이플스토리 서체가 적용되어 있습니다.
"""


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def patcher_version():
    m = re.search(r'^PATCHER_VERSION = "([^"]+)"', (HERE / "patcher" / "patcher.py").read_text(encoding="utf-8"), re.M)
    if not m:
        raise RuntimeError("patcher.py에서 PATCHER_VERSION을 찾을 수 없음")
    return m.group(1)


def fetch(cache, url, sha256):
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / Path(url).name
    if not (archive.is_file() and sha256_file(archive) == sha256):
        print(f"다운로드: {url}")
        with urllib.request.urlopen(url, timeout=120) as resp, open(archive, "wb") as f:
            shutil.copyfileobj(resp, f)
        if sha256_file(archive) != sha256:
            archive.unlink()
            raise RuntimeError(f"해시 불일치: {archive.name}")
    return archive


def write_zip(target, root, files):
    """경로순·고정 시각으로 기록 (같은 입력이면 같은 zip)"""
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for path in sorted(files, key=lambda p: p.relative_to(root).as_posix()):
            info = zipfile.ZipInfo(path.relative_to(root).as_posix(), ZIP_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            z.writestr(info, path.read_bytes())


def build_runtime(archive, runtime):
    with zipfile.ZipFile(archive) as z:
        z.extractall(runtime)
    for name in PRUNE_ROOT:
        target = runtime / name
        shutil.rmtree(target) if target.is_dir() else target.unlink(missing_ok=True)
    lib = runtime / "Lib"
    for name in PRUNE_LIB:
        shutil.rmtree(lib / name, ignore_errors=True)
    for cache in list(lib.rglob("__pycache__")):
        shutil.rmtree(cache, ignore_errors=True)
    subprocess.run([str(runtime / "python.exe"), "-I", "-m", "compileall", "-b", "-q",
                    "--invalidation-mode", "unchecked-hash", str(lib)], check=True)
    write_zip(runtime / f"{PYTHON_TAG}.zip", lib, [p for p in lib.rglob("*") if p.is_file() and p.suffix != ".py"])
    shutil.rmtree(lib)
    # ._pth: 사용자 PC의 PYTHONPATH·레지스트리·site-packages와 분리
    (runtime / f"{PYTHON_TAG}._pth").write_text(f"{PYTHON_TAG}.zip\nDLLs\n..\\app\n", encoding="ascii", newline="\r\n")


def verify_signatures(package):
    """exe/dll/pyd가 전부 유효한 서명본인지 확인. 하나라도 아니면 빌드 실패"""
    script = ("$bin = @(Get-ChildItem -LiteralPath $args[0] -Recurse -File | "
              "Where-Object { '.exe','.dll','.pyd' -contains $_.Extension.ToLower() }); "
              "if ($bin.Count -lt 10) { 'binaries: ' + $bin.Count; exit 1 }; "
              "$bad = @($bin | Where-Object { (Get-AuthenticodeSignature -LiteralPath $_.FullName).Status -ne 'Valid' }); "
              "$bad | ForEach-Object { $_.FullName }; if ($bad.Count) { exit 1 }")
    result = subprocess.run(["powershell", "-NoProfile", "-Command", f"& {{ {script} }}", str(package)],
                            capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"서명 없는 실행 파일: {result.stdout.strip() or result.stderr.strip()}")


def build(out):
    if sys.platform != "win32":
        raise RuntimeError("Windows에서만 빌드 가능")
    version = patcher_version()
    meta = json.loads((HERE / "translation" / "ko_strings.json").read_text(encoding="utf-8"))["meta"]
    package = out / PACKAGE_NAME
    if package.exists():
        shutil.rmtree(package)
    package.mkdir(parents=True)

    build_runtime(fetch(out / "cache", PYTHON_URL, PYTHON_SHA256), package / "runtime")
    app = package / "app"
    (app / "translation").mkdir(parents=True)
    for name in APP_FILES:
        shutil.copyfile(HERE / "patcher" / name, app / name)
    for name in TRANSLATION_FILES:
        shutil.copyfile(HERE / "translation" / name, app / "translation" / name)
    with zipfile.ZipFile(fetch(out / "cache", CERTIFI_URL, CERTIFI_SHA256)) as z:
        (app / CA_BUNDLE).write_bytes(z.read(f"certifi/{CA_BUNDLE}"))
    (app / "package.json").write_text(json.dumps({"patcher": version, "python": PYTHON_VERSION, "translation": meta.get("version")},
                                                 indent=2) + "\n", encoding="utf-8", newline="\n")
    (package / f"{PACKAGE_NAME}.cmd").write_text(LAUNCHER, encoding="utf-8", newline="\r\n")
    (package / "README.txt").write_text(README.format(version=version, python=PYTHON_VERSION, patch=meta.get("version", ""),
                                                      game_version=meta.get("game_version"), game_build=meta.get("game_build")),
                                        encoding="utf-8-sig", newline="\r\n")
    verify_signatures(package)

    tag = meta.get("version") or version
    target = out / f"{PACKAGE_NAME}-{tag}.zip"
    write_zip(target, out, [p for p in package.rglob("*") if p.is_file()])
    # 번역만 든 zip: 패처가 GitHub 릴리즈에서 새 번역을 받을 때 씀
    tzip = out / f"{TRANSLATION_ZIP_PREFIX}{tag}.zip"
    write_zip(tzip, app / "translation", [app / "translation" / n for n in TRANSLATION_FILES])
    sums = "".join(f"{sha256_file(p)}  {p.name}\n" for p in (target, tzip))
    (out / "SHA256SUMS").write_text(sums, encoding="ascii", newline="\n")
    print(f"{target}  {target.stat().st_size / 1e6:.1f} MB\n{tzip}  {tzip.stat().st_size / 1e3:.0f} KB\n{sums}", end="")
    return target


def main():
    parser = argparse.ArgumentParser(description="패처 배포 패키지 생성")
    parser.add_argument("--out", default=str(HERE / "dist"), help="출력 폴더 (기본: dist)")
    build(Path(parser.parse_args().out).resolve())


if __name__ == "__main__":
    main()
