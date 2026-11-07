"""
Electron asar 묶음 파일 읽기·쓰기.

구조: [8바이트: 헤더 크기] [헤더: 파일 목록 JSON] [파일 내용들을 이어 붙인 것]
헤더의 각 파일 항목에는 크기·위치(offset)·SHA256(integrity)이 있다.
바꾼 파일만 새 내용으로 넣고 나머지는 원본 바이트를 그대로 복사한다.
"""

import hashlib
import json
import os
import shutil
import struct

BLOCK_SIZE = 4 * 1024 * 1024


class AsarError(ValueError):
    pass


def _align4(n):
    return (n + 3) & ~3


class Asar:
    def __init__(self, path):
        self.path = str(path)
        with open(self.path, "rb") as f:
            head = f.read(16)
            if len(head) < 16:
                raise AsarError("asar 파일이 너무 작음")
            size_payload, header_pickle_size, header_payload_size, json_len = struct.unpack("<IIII", head)
            if size_payload != 4 or json_len > header_payload_size or header_payload_size + 4 != header_pickle_size:
                raise AsarError("asar 헤더 형식이 아님")
            raw = f.read(json_len)
        self.header = json.loads(raw.decode("utf-8"))
        self.base = 8 + header_pickle_size

    def iter_files(self, node=None, prefix=""):
        """(경로, 항목) — 경로는 '/'로 구분, 맨 앞 '/' 없음"""
        node = node or self.header
        for name, entry in node.get("files", {}).items():
            path = f"{prefix}{name}"
            if "files" in entry:
                yield from self.iter_files(entry, path + "/")
            else:
                yield path, entry

    def entry(self, path):
        node = self.header
        for part in path.split("/"):
            files = node.get("files")
            if not files or part not in files:
                raise KeyError(path)
            node = files[part]
        return node

    def read(self, path):
        e = self.entry(path)
        if e.get("unpacked") or "link" in e:
            raise AsarError(f"asar 밖에 있는 파일: {path}")
        with open(self.path, "rb") as f:
            f.seek(self.base + int(e["offset"]))
            data = f.read(e["size"])
        if len(data) != e["size"]:
            raise AsarError(f"파일이 잘림: {path}")
        return data

    def write(self, target, replacements):
        """replacements: {경로: bytes}. 나머지는 원본 그대로 복사해 target에 새 asar를 씀"""
        header = json.loads(json.dumps(self.header))      # 깊은 복사
        missing = [p for p in replacements if not _exists(header, p)]
        if missing:
            raise AsarError(f"asar에 없는 파일: {missing[:5]}")

        # 원본의 저장 순서(offset 순)대로 다시 배치
        packed = []
        for path, e in Asar._iter(header):
            if e.get("unpacked") or "link" in e or "offset" not in e:
                continue
            packed.append((int(e["offset"]), path, e))
        packed.sort(key=lambda x: x[0])

        offset = 0
        plan = []
        for orig_offset, path, e in packed:
            if path in replacements:
                data = replacements[path]
                e["size"] = len(data)
                e["integrity"] = integrity(data)
                plan.append((path, None, data))
            else:
                plan.append((path, orig_offset, None))
            e["offset"] = str(offset)
            offset += e["size"]

        hjson = json.dumps(header, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        header_payload = struct.pack("<I", len(hjson)) + hjson
        header_payload += b"\0" * (_align4(len(header_payload)) - len(header_payload))
        header_pickle = struct.pack("<I", len(header_payload)) + header_payload
        size_pickle = struct.pack("<II", 4, len(header_pickle))

        tmp = str(target) + ".tmp"
        try:
            with open(self.path, "rb") as src, open(tmp, "wb") as out:
                out.write(size_pickle)
                out.write(header_pickle)
                for path, orig_offset, data in plan:
                    if data is not None:
                        out.write(data)
                    else:
                        size = Asar._lookup(header, path)["size"]
                        src.seek(self.base + orig_offset)
                        _copy_exact(src, out, size, path)
                out.flush()
                os.fsync(out.fileno())
            os.replace(tmp, target)
        except BaseException:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise

    @staticmethod
    def _iter(node, prefix=""):
        for name, entry in node.get("files", {}).items():
            path = f"{prefix}{name}"
            if "files" in entry:
                yield from Asar._iter(entry, path + "/")
            else:
                yield path, entry

    @staticmethod
    def _lookup(header, path):
        node = header
        for part in path.split("/"):
            node = node["files"][part]
        return node


def _exists(header, path):
    try:
        Asar._lookup(header, path)
        return True
    except KeyError:
        return False


def _copy_exact(src, out, size, path):
    left = size
    while left:
        chunk = src.read(min(left, 1 << 20))
        if not chunk:
            raise AsarError(f"원본 asar가 잘림: {path}")
        out.write(chunk)
        left -= len(chunk)


def integrity(data):
    blocks = [hashlib.sha256(data[i:i + BLOCK_SIZE]).hexdigest() for i in range(0, max(len(data), 1), BLOCK_SIZE)]
    return {"algorithm": "SHA256", "hash": hashlib.sha256(data).hexdigest(), "blockSize": BLOCK_SIZE, "blocks": blocks}


def extract_all(asar_path, dest, skip=("node_modules/",)):
    """개발용: asar 안 파일을 dest에 풂 (skip으로 시작하는 경로 제외)"""
    a = Asar(asar_path)
    count = 0
    for path, e in a.iter_files():
        if any(path.startswith(s) for s in skip) or e.get("unpacked") or "link" in e:
            continue
        out = os.path.join(dest, *path.split("/"))
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "wb") as f:
            f.write(a.read(path))
        count += 1
    return count


def copy_file(src, dst):
    shutil.copyfile(src, dst)
