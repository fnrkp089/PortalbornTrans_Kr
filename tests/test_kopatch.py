"""patcher/kopatch.py: 문자열·템플릿 번역 적용, 코드 보정"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "patcher"))

import kopatch  # noqa: E402


class TranslateJs(unittest.TestCase):
    def test_string_keeps_quote_style(self):
        src = "const a = '你好', b = \"再见\";"
        out = kopatch.translate_js(src, {"你好": "안녕 'x'", "再见": "잘 가 \"y\""})
        self.assertEqual(out, "const a = '안녕 \\'x\\'', b = \"잘 가 \\\"y\\\"\";")

    def test_template_reorder(self):
        src = "msg = `你对${mob}造成${dmg}点伤害`;"
        out = kopatch.translate_js(src, {"你对${0}造成${1}点伤害": "${1}의 피해를 ${0}에게 입혔다"})
        self.assertEqual(out, "msg = `${dmg}의 피해를 ${mob}에게 입혔다`;")

    def test_template_bad_placeholders_keeps_original(self):
        src = "msg = `你对${mob}造成${dmg}点伤害`;"
        rep = kopatch.Report()
        out = kopatch.translate_js(src, {"你对${0}造成${1}点伤害": "${0}에게 피해"}, rep, "x.js")
        self.assertEqual(out, src)
        self.assertEqual(len(rep.errors), 1)

    def test_nested_inside_template_expression(self):
        src = "s = `${zh ? \"中文\" : \"EN\"} 和 ${n}`;"
        out = kopatch.translate_js(src, {"中文": "한국어", "${0} 和 ${1}": "${0} 및 ${1}"})
        self.assertEqual(out, "s = `${zh ? \"한국어\" : \"EN\"} 및 ${n}`;")

    def test_template_escaping(self):
        src = "s = `价格${p}`;"
        out = kopatch.translate_js(src, {"价格${0}": "값 `${0}` \\ 끝"})
        self.assertEqual(out, "s = `값 \\`${p}\\` \\\\ 끝`;")

    def test_missing_reported(self):
        rep = kopatch.Report()
        kopatch.translate_js("x = '未知';", {}, rep, "f.js")
        self.assertEqual(rep.missing, {"f.js": ["未知"]})

    def test_regex_untouched(self):
        src = "if (/[㐀-鿿]/.test(q)) a = '中';"
        self.assertEqual(kopatch.translate_js(src, {"中": "한"}), "if (/[㐀-鿿]/.test(q)) a = '한';")


class CodePatches(unittest.TestCase):
    def test_count_mismatch_skips(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "ko_strings.json").write_text(json.dumps({"strings": {}}), encoding="utf-8")
            Path(d, "code_patches.json").write_text(json.dumps({"patches": [
                {"id": "a", "file": "x.js", "find": "foo", "replace": "bar", "count": 1},
                {"id": "b", "file": "x.js", "find": "baz", "replace": "qux", "count": 1},
                {"id": "c", "file": "y.css", "append": "{{add.css}}"}]}), encoding="utf-8")
            Path(d, "add.css").write_text("p{}", encoding="utf-8")
            pkg = kopatch.Package(d)
            files = {"x.js": "foo foo baz", "y.css": "a{}\n"}
            rep = kopatch.Report()
            kopatch.apply_code_patches(files, pkg, rep)
            self.assertEqual(files["x.js"], "foo foo qux")
            self.assertEqual(files["y.css"], "a{}\np{}")
            self.assertEqual(rep.patched, ["b", "c"])
            self.assertEqual([i for i, _ in rep.skipped], ["a"])

    def test_real_patch_file_is_valid(self):
        data = json.loads((ROOT / "translation" / "code_patches.json").read_text(encoding="utf-8"))
        ids = [p["id"] for p in data["patches"]]
        self.assertEqual(len(ids), len(set(ids)))
        for p in data["patches"]:
            self.assertTrue(("find" in p and "replace" in p) or "append" in p, p["id"])


if __name__ == "__main__":
    unittest.main()
