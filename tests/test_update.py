"""patcher.py: GitHub 릴리즈에서 새 번역 받기, 화면 맞춤 계산

내려받기(_http_get)는 가짜로 바꿔서 시험한다 (이 PC는 로컬 HTTP 서버 연결이 가끔 끊겨 테스트가 흔들림).
"""

import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "patcher"))

import patcher  # noqa: E402

FILES = ("ko_strings.json", "code_patches.json", "ko_runtime.js", "ko_fonts.css")


def make_release(folder, tag, corrupt=False):
    """가짜 릴리즈 파일들을 folder에 만들고, URL → 파일 내용 사전을 돌려줌"""
    folder = Path(folder)
    pkg = folder / "pkg"
    pkg.mkdir(parents=True)
    data = {"meta": {"version": tag}, "strings": {"你好": "안녕"}}
    (pkg / "ko_strings.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    (pkg / "code_patches.json").write_text('{"patches": []}', encoding="utf-8")
    (pkg / "ko_runtime.js").write_text("//", encoding="utf-8")
    (pkg / "ko_fonts.css").write_text("/**/", encoding="utf-8")
    zip_name = f"{patcher.TRANSLATION_ASSET_PREFIX}{tag}.zip"
    with zipfile.ZipFile(folder / zip_name, "w") as z:
        for n in FILES:
            z.write(pkg / n, "translation/" + n)       # 폴더 안에 있어도 이름으로 찾아야 함
        z.writestr("../evil.txt", "x")                 # 엉뚱한 경로는 무시돼야 함
    zbytes = (folder / zip_name).read_bytes()
    digest = "0" * 64 if corrupt else hashlib.sha256(zbytes).hexdigest()
    base = "https://example.invalid/dl"
    rel = {"tag_name": tag, "name": f"{tag} 번역", "body": "## 📖 주요 작업\n- 새 문장 번역\n\n## 사용법\n- 몰라도 됨",
           "assets": [{"name": zip_name, "browser_download_url": f"{base}/{zip_name}"},
                      {"name": "SHA256SUMS", "browser_download_url": f"{base}/SHA256SUMS"}]}
    return {patcher.GITHUB_API_LATEST: json.dumps(rel, ensure_ascii=False).encode("utf-8"),
            f"{base}/{zip_name}": zbytes,
            f"{base}/SHA256SUMS": f"{digest}  {zip_name}\n".encode("ascii")}


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old_env = os.environ.get("LOCALAPPDATA")
        os.environ["LOCALAPPDATA"] = str(Path(self.tmp) / "appdata")
        self.old_get = patcher._http_get

    def tearDown(self):
        patcher._http_get = self.old_get
        if self.old_env is None:
            os.environ.pop("LOCALAPPDATA", None)
        else:
            os.environ["LOCALAPPDATA"] = self.old_env
        shutil.rmtree(self.tmp, ignore_errors=True)

    def serve(self, files):
        patcher._http_get = lambda url, **k: files[url]

    def test_download_and_pick_newest(self):
        self.serve(make_release(Path(self.tmp) / "srv", "v9.9.9"))
        info = patcher.latest_release_info()
        self.assertEqual(info["tag"], "v9.9.9")
        self.assertEqual(patcher.release_highlights(info["body"]), ["• 새 문장 번역"])
        folder = patcher.download_translation(info, say=lambda m: None)
        self.assertEqual(patcher.package_version(folder), "v9.9.9")
        self.assertEqual(sorted(p.name for p in folder.iterdir()), sorted(FILES))
        self.assertEqual(patcher.best_local_package(), ("v9.9.9", folder))

    def test_bad_hash_rejected(self):
        self.serve(make_release(Path(self.tmp) / "srv", "v9.9.9", corrupt=True))
        with self.assertRaises(patcher.PatchError):
            patcher.download_translation(patcher.latest_release_info(), say=lambda m: None)
        self.assertEqual(patcher.downloaded_packages(), [])

    def test_older_release_not_preferred(self):
        self.serve(make_release(Path(self.tmp) / "srv", "v0.0.1"))
        patcher.download_translation(patcher.latest_release_info(), say=lambda m: None)
        ver, folder = patcher.best_local_package()
        self.assertEqual(folder, patcher.BUNDLED_TRANSLATION)


class ScreenFit(unittest.TestCase):
    def test_umpc_compact(self):
        w, h, compact = patcher.fit_window(1920, 1152, 2.0)      # 7~8인치 1920x1200, 배율 200%
        self.assertLessEqual(h, 1152 - 80)
        self.assertTrue(compact)

    def test_desktop_full(self):
        self.assertEqual(patcher.fit_window(2560, 1400, 1.0), (780, 680, False))

    def test_versions(self):
        self.assertGreater(patcher.parse_version("v0.1.10"), patcher.parse_version("v0.1.9"))


if __name__ == "__main__":
    unittest.main()
