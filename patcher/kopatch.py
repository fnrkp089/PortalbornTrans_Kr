"""
번역 적용 엔진.

게임의 app.asar 원본을 읽어서
  1) translation/code_patches.json 의 코드 수정을 적용하고
  2) JS 안의 중국어 문자열을 translation/ko_strings.json 의 한국어로 바꾼 뒤
  3) 새 app.asar를 쓴다.
중국어 언어 자리에 한국어가 들어가므로, 게임 안에서 언어를 '중국어'로 두면 한국어가 나온다.
"""

import json
import re
from pathlib import Path

import asar
import jstok

TEXT_EXTS = (".js", ".html", ".css")
SKIP_PREFIX = ("node_modules/",)
SKIP_FILES = {"tests.js", "tests.html"}
_PLACEHOLDER = re.compile(r"\$\{(\d+)\}")
_ASSET_REF = re.compile(r"\{\{([\w.\-]+)\}\}")


class Report:
    def __init__(self):
        self.translated = 0          # 바꾼 출현 수
        self.missing = {}            # 파일 → 번역 없는 중국어 문자열 목록
        self.errors = []             # 번역문 형식 오류 (원문 유지)
        self.patched = []            # 적용된 코드 수정 id
        self.skipped = []            # 적용 못 한 코드 수정 (id, 이유)
        self.files_changed = []

    @property
    def missing_count(self):
        return sum(len(v) for v in self.missing.values())

    def summary(self):
        return {
            "translated": self.translated,
            "missing": self.missing_count,
            "errors": len(self.errors),
            "code_patches": len(self.patched),
            "code_patches_skipped": [f"{i}: {why}" for i, why in self.skipped],
            "files_changed": len(self.files_changed),
        }


class Package:
    """번역 패키지 폴더 (ko_strings.json, code_patches.json, 기타 넣을 파일)"""

    def __init__(self, folder):
        self.folder = Path(folder)
        data = json.loads((self.folder / "ko_strings.json").read_text(encoding="utf-8"))
        self.meta = data.get("meta", {})
        self.strings = data["strings"]
        # 파일별 예외 번역 {파일 경로: {중국어: 한국어}} — 같은 중국어가 파일마다 다른 뜻일 때
        self.file_strings = data.get("files", {})
        cp = self.folder / "code_patches.json"
        self.code_patches = json.loads(cp.read_text(encoding="utf-8"))["patches"] if cp.exists() else []

    def asset(self, name):
        p = self.folder / name
        if p.parent != self.folder or not p.is_file():
            raise FileNotFoundError(f"번역 패키지에 {name} 없음")
        return p.read_text(encoding="utf-8")

    def expand(self, text):
        return _ASSET_REF.sub(lambda m: self.asset(m.group(1)), text)


# ------------------------------------------------------------------ JS 문자열 번역

def _render_template(src, tok, exprs_text, ko):
    """번역문(${n} 표시 포함) → 새 템플릿 리터럴. 형식이 안 맞으면 None"""
    parts = _PLACEHOLDER.split(ko)
    texts, idx = parts[0::2], [int(x) for x in parts[1::2]]
    if sorted(idx) != list(range(len(exprs_text))):
        return None
    out = ["`", jstok.encode_template_text(texts[0])]
    for i, t in zip(idx, texts[1:]):
        out.append("${" + exprs_text[i] + "}")
        out.append(jstok.encode_template_text(t))
    out.append("`")
    return "".join(out)


class _Lookup:
    """파일별 예외 번역을 먼저 보고, 없으면 전체 사전"""

    def __init__(self, table, overrides=None):
        self.table, self.overrides = table, overrides or {}

    def get(self, key):
        v = self.overrides.get(key)
        return v if v is not None else self.table.get(key)


def translate_js(src, table, report=None, filename="", overrides=None):
    """JS 소스의 중국어 문자열을 table(중국어 → 한국어)로 바꾼 새 소스"""
    report = report or Report()
    table = _Lookup(table, overrides)
    tops, _ = jstok.tokenize(src)
    missing = []

    def render_range(start, end, tokens):
        out, pos = [], start
        for t in tokens:
            out.append(src[pos:t.start])
            out.append(render_token(t))
            pos = t.end
        out.append(src[pos:end])
        return "".join(out)

    def render_token(t):
        if isinstance(t, jstok.StrTok):
            if jstok.has_cjk(t.value):
                ko = table.get(t.value)
                if ko is not None:
                    report.translated += 1
                    return jstok.encode_string(ko, t.quote)
                missing.append(t.value)
            return src[t.start:t.end]
        if isinstance(t, jstok.TplTok):
            exprs_text = [render_range(e.start, e.end, e.children) for e in t.exprs]
            if not t.tagged and any(jstok.has_cjk(q) for q in t.quasis):
                key = jstok.template_key(t.quasis)
                ko = table.get(key)
                if ko is not None:
                    new = _render_template(src, t, exprs_text, ko)
                    if new is not None:
                        report.translated += 1
                        return new
                    report.errors.append((filename, key[:80], "번역문의 ${n} 자리가 원문과 다름"))
                else:
                    missing.append(key)
            # 번역 안 함: 원문 그대로, 식 안쪽만 반영
            out, pos = [], t.start
            for e, et in zip(t.exprs, exprs_text):
                out.append(src[pos:e.start])
                out.append(et)
                pos = e.end
            out.append(src[pos:t.end])
            return "".join(out)
        return src[t.start:t.end]

    result = render_range(0, len(src), tops)
    if missing:
        report.missing.setdefault(filename, []).extend(missing)
    return result


# ------------------------------------------------------------------ 코드 수정

def apply_code_patches(files, package, report):
    """files: {경로: 문자열}. 제자리에서 고침"""
    for p in package.code_patches:
        pid, path = p["id"], p["file"]
        if path not in files:
            report.skipped.append((pid, f"{path} 없음"))
            continue
        text = files[path]
        if "append" in p:
            add = package.expand(p["append"])
            if add.strip() in text:
                report.skipped.append((pid, "이미 들어 있음"))
                continue
            files[path] = text + ("" if text.endswith("\n") else "\n") + add
            report.patched.append(pid)
            continue
        find = p["find"]
        n = text.count(find)
        want = p.get("count")
        if n == 0 or (want is not None and n != want):
            report.skipped.append((pid, f"기준 문자열 {n}번 발견 (예상 {want})"))
            continue
        files[path] = text.replace(find, package.expand(p["replace"]))
        report.patched.append(pid)


# ------------------------------------------------------------------ asar 전체

def build_patched_asar(original_asar, package, out_path, progress=None):
    """원본 asar → 한국어 asar(out_path). Report 반환"""
    report = Report()
    a = asar.Asar(original_asar)
    texts, raw = {}, {}
    for path, e in a.iter_files():
        if e.get("unpacked") or "link" in e or path.startswith(SKIP_PREFIX):
            continue
        if not path.endswith(TEXT_EXTS) or path.rsplit("/", 1)[-1] in SKIP_FILES:
            continue
        data = a.read(path)
        raw[path] = data
        texts[path] = data.decode("utf-8")

    apply_code_patches(texts, package, report)

    replacements = {}
    paths = sorted(texts)
    for i, path in enumerate(paths):
        if progress:
            progress(i, len(paths), path)
        text = texts[path]
        if path.endswith(".js") and jstok.has_cjk(text):
            text = translate_js(text, package.strings, report, path, package.file_strings.get(path))
        data = text.encode("utf-8")
        if data != raw[path]:
            replacements[path] = data
            report.files_changed.append(path)
    a.write(out_path, replacements)
    return report
