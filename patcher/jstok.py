"""
JS 소스에서 문자열·템플릿·정규식 리터럴의 위치를 찾는 토크나이저.

게임 JS를 실행하거나 고치지 않고, 번역할 문자열이 소스의 어디에 있는지만 찾는다.
완전한 파서는 아니고 렉서 수준이며, 정규식과 나눗셈(/)은 앞 토큰으로 구분한다.
작업 폴더의 tests/test_jstok.py가 게임 파일 전체를 acorn(정식 JS 파서) 결과와 대조한다.

토큰 종류
  StrTok   '...' / "..."           value = 실제 문자열 값 (이스케이프 해석)
  TplTok   `...${expr}...`         quasis = 조각들의 실제 값, exprs = 식 구간 (안쪽 토큰 포함)
  RegexTok /.../flags              (번역하지 않음, 위치만)
"""

import re

__all__ = ["StrTok", "TplTok", "RegexTok", "tokenize", "cook_string", "template_key", "has_cjk",
           "encode_string", "encode_template_text", "JsSyntaxError"]


class JsSyntaxError(ValueError):
    pass


class StrTok:
    __slots__ = ("start", "end", "quote", "value")

    def __init__(self, start, end, quote, value):
        self.start, self.end, self.quote, self.value = start, end, quote, value


class TplTok:
    """quasi_ranges[i] = (start, end) 원문 조각 구간, exprs[i] = Expr. 조각 수 = 식 수 + 1"""
    __slots__ = ("start", "end", "quasi_ranges", "quasis", "exprs", "tagged")

    def __init__(self, start):
        self.start, self.end = start, None
        self.quasi_ranges, self.quasis, self.exprs = [], [], []
        self.tagged = False


class Expr:
    """템플릿 안 ${ } 구간. children = 그 안에서 바로 찾은 토큰들"""
    __slots__ = ("start", "end", "children")

    def __init__(self, start):
        self.start, self.end, self.children = start, None, []


class RegexTok:
    __slots__ = ("start", "end", "pattern", "flags")

    def __init__(self, start, end, pattern, flags):
        self.start, self.end, self.pattern, self.flags = start, end, pattern, flags


# 이 키워드 뒤의 / 는 정규식
_REGEX_AFTER_KEYWORDS = frozenset(
    "return typeof instanceof in of new delete void throw case do else yield await".split())
_ID_START = re.compile(r"[A-Za-z_$\u0080-\U0010ffff\\#]")
_ID_PART = re.compile(r"[\w$\u0080-\U0010ffff‌‍]*")
_NUM = re.compile(r"0[xX][0-9a-fA-F_]+n?|0[oO][0-7_]+n?|0[bB][01_]+n?|(?:\d[\d_]*\.?[\d_]*|\.\d[\d_]*)(?:[eE][+-]?\d[\d_]*)?n?")
_PUNCT = re.compile(r">>>=|\.\.\.|===|!==|\*\*=|<<=|>>=|>>>|&&=|\|\|=|\?\?=|=>|==|!=|<=|>=|&&|\|\||\?\?|\?\.(?!\d)|\+\+|--|\+=|-=|\*=|%=|&=|\|=|\^=|/=|<<|>>|\*\*|[{}()\[\];,<>+\-*%&|^!~?:=.@/]")
_LINE_TERM = "\n\r  "
_SIMPLE_ESC = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}
_CJK = re.compile(r"[　-〿㐀-鿿豈-﫿＀-￯]")


def has_cjk(text):
    return bool(_CJK.search(text or ""))


def cook_string(raw, template=False):
    """따옴표 안쪽 원문 → 실제 값. JS 이스케이프 규칙"""
    if "\\" not in raw and "\r" not in raw:
        return raw
    out, i, n = [], 0, len(raw)
    while i < n:
        c = raw[i]
        if c == "\r":
            # 템플릿의 CRLF/CR은 LF로 정규화
            out.append("\n" if template else "\r")
            i += 2 if (template and raw.startswith("\r\n", i)) else 1
            continue
        if c != "\\":
            out.append(c)
            i += 1
            continue
        i += 1
        if i >= n:
            raise JsSyntaxError("문자열 끝의 백슬래시")
        e = raw[i]
        if e in _SIMPLE_ESC and not (e == "0" and i + 1 < n and raw[i + 1].isdigit()):
            out.append(_SIMPLE_ESC[e])
            i += 1
        elif e == "x":
            out.append(chr(int(raw[i + 1:i + 3], 16)))
            i += 3
        elif e == "u":
            if raw.startswith("{", i + 1):
                j = raw.index("}", i)
                out.append(chr(int(raw[i + 2:j], 16)))
                i = j + 1
            else:
                out.append(chr(int(raw[i + 1:i + 5], 16)))
                i += 5
        elif e == "\r":
            i += 2 if raw.startswith("\r\n", i) else 1      # 줄 이어붙이기
        elif e in "\n  ":
            i += 1
        elif e.isdigit():
            # 레거시 8진 이스케이프 (엄격 모드·모듈에서는 쓰이지 않음)
            m = re.match(r"[0-7]{1,3}", raw[i:])
            if not m:
                out.append(e)
                i += 1
            else:
                out.append(chr(int(m.group(0), 8)))
                i += len(m.group(0))
        else:
            out.append(e)
            i += 1
    # 😀 같은 서로게이트 쌍을 한 글자로 합침 (JS 문자열은 UTF-16)
    s = "".join(out)
    if re.search("[\ud800-\udbff][\udc00-\udfff]", s):
        s = s.encode("utf-16", "surrogatepass").decode("utf-16")
    return s


def template_key(quasis):
    """템플릿을 번역 사전의 키로: 조각 사이에 ${0}, ${1} ... 표시"""
    parts = [quasis[0]]
    for i, q in enumerate(quasis[1:]):
        parts.append("${%d}" % i)
        parts.append(q)
    return "".join(parts)


def encode_string(value, quote):
    """실제 값 → 같은 따옴표를 쓰는 JS 문자열 리터럴"""
    out = [quote]
    for c in value:
        if c == "\\":
            out.append("\\\\")
        elif c == quote:
            out.append("\\" + quote)
        elif c == "\n":
            out.append("\\n")
        elif c == "\r":
            out.append("\\r")
        elif c == " ":
            out.append("\\u2028")
        elif c == " ":
            out.append("\\u2029")
        else:
            out.append(c)
    out.append(quote)
    return "".join(out)


def encode_template_text(value):
    """템플릿 조각의 실제 값 → 원문 (백틱 안에 들어갈 글)"""
    return value.replace("\\", "\\\\").replace("`", "\\`").replace("${", "\\${")


class _Lexer:
    def __init__(self, src):
        self.src = src
        self.n = len(src)
        self.i = 0
        # 정규식 판정용: 직전 의미 토큰 ('punct', 값) / ('name', 값) / ('value', None)
        self.prev = ("punct", None)
        self.tokens = []           # 맨 바깥 토큰
        self.flat = []             # 모든 토큰 (안쪽 포함), 등장 순
        self.brace_stack = []      # '{' 또는 템플릿 식(Expr)

    def error(self, msg):
        line = self.src.count("\n", 0, self.i) + 1
        raise JsSyntaxError(f"{msg} (줄 {line})")

    def add(self, tok):
        self.flat.append(tok)
        for entry in reversed(self.brace_stack):
            if isinstance(entry, Expr):
                entry.children.append(tok)
                return
        self.tokens.append(tok)

    def regex_allowed(self):
        kind, val = self.prev
        if kind == "value":
            return False
        if kind == "name":
            return val in _REGEX_AFTER_KEYWORDS
        return val not in (")", "]", "}") if val is not None else True

    def skip_space_and_comments(self):
        src, n = self.src, self.n
        while self.i < n:
            c = src[self.i]
            if c in " \t\n\r\v\f ﻿  " or (c > "\u007f" and c.isspace()):
                self.i += 1
            elif src.startswith("//", self.i):
                j = self.i + 2
                while j < n and src[j] not in _LINE_TERM:
                    j += 1
                self.i = j
            elif src.startswith("/*", self.i):
                j = src.find("*/", self.i + 2)
                if j < 0:
                    self.error("닫히지 않은 주석")
                self.i = j + 2
            else:
                return

    def read_string(self):
        src, start, q = self.src, self.i, self.src[self.i]
        j = start + 1
        while True:
            if j >= self.n:
                self.error("닫히지 않은 문자열")
            c = src[j]
            if c == "\\":
                j += 2
                if src.startswith("\r\n", j - 1):
                    j += 1
            elif c == q:
                break
            elif c in "\n\r":
                self.error("문자열 안의 줄바꿈")
            else:
                j += 1
        raw = src[start + 1:j]
        self.i = j + 1
        tok = StrTok(start, self.i, q, cook_string(raw))
        self.add(tok)
        self.prev = ("value", None)

    def read_template_part(self, tok):
        """백틱 다음 또는 식 끝 '}' 다음부터 조각 하나를 읽음. True = 템플릿 끝"""
        src, j = self.src, self.i
        qstart = j
        while True:
            if j >= self.n:
                self.error("닫히지 않은 템플릿")
            c = src[j]
            if c == "\\":
                j += 2
            elif c == "`":
                tok.quasi_ranges.append((qstart, j))
                tok.quasis.append(cook_string(src[qstart:j], template=True))
                self.i = j + 1
                tok.end = self.i
                return True
            elif c == "$" and src.startswith("${", j):
                tok.quasi_ranges.append((qstart, j))
                tok.quasis.append(cook_string(src[qstart:j], template=True))
                self.i = j + 2
                expr = Expr(self.i)
                tok.exprs.append(expr)
                self.brace_stack.append(expr)
                return False
            else:
                j += 1

    def read_template(self):
        tok = TplTok(self.i)
        # foo`...` / a.b`...` / f()`...` 는 태그 템플릿 (태그 함수가 원문을 받으므로 번역하지 않음)
        tok.tagged = (self.prev[0] == "value" and self.prev[1] is not None) or self.prev in (("punct", ")"), ("punct", "]"))
        self.add(tok)
        self.i += 1
        self._open_templates.append(tok)
        if self.read_template_part(tok):
            self._open_templates.pop()
            self.prev = ("value", None)
        else:
            self.prev = ("punct", "${")

    def read_regex(self):
        src, start = self.src, self.i
        j = start + 1
        in_class = False
        while True:
            if j >= self.n or src[j] in _LINE_TERM:
                self.error("닫히지 않은 정규식")
            c = src[j]
            if c == "\\":
                j += 2
                continue
            if in_class:
                if c == "]":
                    in_class = False
            elif c == "[":
                in_class = True
            elif c == "/":
                break
            j += 1
        pattern = src[start + 1:j]
        j += 1
        m = _ID_PART.match(src, j)
        flags = m.group(0)
        self.i = m.end()
        self.add(RegexTok(start, self.i, pattern, flags))
        self.prev = ("value", None)

    def run(self):
        src = self.src
        self._open_templates = []
        if src.startswith("#!"):
            j = 0
            while j < self.n and src[j] not in _LINE_TERM:
                j += 1
            self.i = j
        while True:
            self.skip_space_and_comments()
            if self.i >= self.n:
                break
            c = src[self.i]
            if c in "'\"":
                self.read_string()
            elif c == "`":
                self.read_template()
            elif c == "/" and self.regex_allowed():
                self.read_regex()
            elif c == "}" and self.brace_stack and isinstance(self.brace_stack[-1], Expr):
                expr = self.brace_stack.pop()
                expr.end = self.i
                tok = self._open_templates[-1]
                self.i += 1
                if self.read_template_part(tok):
                    self._open_templates.pop()
                    self.prev = ("value", None)
                else:
                    self.prev = ("punct", "${")
            elif c.isdigit() or (c == "." and self.i + 1 < self.n and src[self.i + 1].isdigit()):
                m = _NUM.match(src, self.i)
                self.i = m.end()
                self.prev = ("value", None)
            elif _ID_START.match(c):
                j = self.i + 1
                if c == "\\":                       # \u 이스케이프 식별자
                    j = self.i + 6
                m = _ID_PART.match(src, j)
                word = src[self.i:m.end()]
                self.i = m.end()
                kind = "name" if word in _REGEX_AFTER_KEYWORDS else "value"
                if word in ("this", "super", "null", "true", "false"):
                    kind = "value"
                self.prev = (kind, word) if kind == "name" else ("value", word)
            else:
                m = _PUNCT.match(src, self.i)
                if not m:
                    self.error(f"알 수 없는 문자 {c!r}")
                p = m.group(0)
                self.i = m.end()
                if p == "{":
                    self.brace_stack.append("{")
                elif p == "}":
                    if not self.brace_stack:
                        self.error("짝 없는 }")
                    self.brace_stack.pop()
                self.prev = ("punct", p)
        if self.brace_stack or self._open_templates:
            self.error("파일 끝에서 괄호·템플릿이 닫히지 않음")
        return self.tokens, self.flat


def tokenize(src):
    """(맨 바깥 토큰 목록, 전체 토큰 목록) 반환"""
    return _Lexer(src).run()
