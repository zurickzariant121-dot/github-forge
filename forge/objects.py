"""Git object model and wire encoding.

Owns the bytes: object identity, commit/tree/tag serialisation,
pkt-line framing, and packfile read/write (including delta resolution).
No HTTP, no state, no business rules.
"""

from __future__ import annotations

import hashlib
import struct
import zlib

# --- identities ---------------------------------------------------------

AUTHOR = "A U Thor <author@example.com>"
COMMITTER = "C O Mitter <committer@example.com>"
FIXED_TS = 1700000000          # deterministic timestamps
FIXED_DATE = "2026-01-01"      # deterministic issue dates
ZERO_OID = "0" * 40


def object_id(kind: str, payload: bytes) -> str:
    """SHA-1 identity of a git object: ``<kind> <len>\\0<payload>``."""
    return hashlib.sha1(kind.encode() + b" " + str(len(payload)).encode()
                        + b"\0" + payload).hexdigest()


def raw_bytes(kind: str, payload: bytes) -> bytes:
    """Full on-disk form of an object, header included."""
    return kind.encode() + b" " + str(len(payload)).encode() + b"\0" + payload


def load_raw(kind: str, blob: bytes) -> bytes:
    """Strip the ``<kind> <len>\\0`` header, verifying it matches."""
    head, _, body = blob.partition(b"\0")
    want = kind.encode() + b" " + str(len(body)).encode()
    if head != want:
        raise ValueError("object header mismatch")
    return body


# --- commits ------------------------------------------------------------

def encode_commit(tree_oid: str, parent_oids: list[str], message: str) -> bytes:
    lines = ["tree " + tree_oid]
    lines += ["parent " + p for p in parent_oids]
    lines.append(f"author {AUTHOR} {FIXED_TS} +0000")
    lines.append(f"committer {COMMITTER} {FIXED_TS} +0000")
    lines.append("")
    lines.append(message)
    return "\n".join(lines).encode()


def decode_commit(payload: bytes) -> dict:
    head, _, body = payload.partition(b"\n\n")
    out = {"parents": [], "message": body.decode()}
    for line in head.decode().split("\n"):
        key, _, value = line.partition(" ")
        if key == "tree":
            out["tree"] = value
        elif key == "parent":
            out["parents"].append(value)
        elif key == "author":
            out["author"] = value
        elif key == "committer":
            out["committer"] = value
    return out


# --- trees --------------------------------------------------------------

def _tree_sort_key(entry: tuple[str, str, str]) -> tuple:
    """Git order: directories sort as if their name ended with '/'."""
    mode, name, _oid = entry
    return (name + "/" if mode == "40000" else name).encode()


def encode_tree(entries: list[tuple[str, str, str]]) -> bytes:
    blob = bytearray()
    for mode, name, oid in sorted(entries, key=_tree_sort_key):
        blob += mode.encode() + b" " + name.encode() + b"\0"
        blob += bytes.fromhex(oid)
    return bytes(blob)


def decode_tree(payload: bytes) -> list[tuple[str, str, str]]:
    entries: list[tuple[str, str, str]] = []
    pos = 0
    while pos < len(payload):
        sp = payload.index(b" ", pos)
        mode = payload[pos:sp].decode()
        nul = payload.index(b"\0", sp)
        name = payload[sp + 1:nul].decode()
        oid = payload[nul + 1:nul + 21].hex()
        entries.append((mode, name, oid))
        pos = nul + 21
    return entries


# --- tags ---------------------------------------------------------------

def encode_tag(target_oid: str, target_kind: str, name: str, message: str) -> bytes:
    lines = [
        "object " + target_oid,
        "type " + target_kind,
        "tag " + name,
        f"tagger {AUTHOR} {FIXED_TS} +0000",
        "",
        message,
    ]
    return "\n".join(lines).encode()


def decode_tag(payload: bytes) -> dict:
    head, _, body = payload.partition(b"\n\n")
    out = {"message": body.decode()}
    for line in head.decode().split("\n"):
        key, _, value = line.partition(" ")
        if key == "object":
            out["object"] = value
        elif key == "type":
            out["type"] = value
        elif key == "tag":
            out["tag"] = value
    return out


# --- pkt-line framing ---------------------------------------------------

def frame_line(payload: bytes | None) -> bytes:
    """One pkt-line. ``None`` is the flush packet (``0000``)."""
    if payload is None:
        return b"0000"
    return ("%04x" % (len(payload) + 4)).encode() + payload


def frame_delim() -> bytes:
    return b"0001"


class PacketReader:
    """Cursor over a pkt-line stream."""

    def __init__(self, stream: bytes):
        self.buf = stream
        self.pos = 0

    def next(self) -> bytes | None:
        """Return the next payload, or ``None`` on flush/end."""
        if self.pos + 4 > len(self.buf):
            return None
        size = int(self.buf[self.pos:self.pos + 4], 16)
        if size == 0:
            self.pos += 4
            return None
        if size == 1:
            self.pos += 4
            return b""
        out = self.buf[self.pos + 4:self.pos + size]
        self.pos += size
        return out

    def read_line(self) -> bytes:
        """One payload, raising on flush/end."""
        item = self.next()
        if item is None:
            raise ValueError("unexpected flush packet")
        return item

    def drain(self) -> list[bytes]:
        """All payloads until flush/end."""
        out = []
        while True:
            item = self.next()
            if item is None:
                return out
            out.append(item)


# --- packs --------------------------------------------------------------

OBJ_COMMIT = 1
OBJ_TREE = 2
OBJ_BLOB = 3
OBJ_TAG = 4
OBJ_OFS_DELTA = 6
OBJ_REF_DELTA = 7

_KIND_BY_NUM = {1: "commit", 2: "tree", 3: "blob", 4: "tag"}
_NUM_BY_KIND = {"commit": 1, "tree": 2, "blob": 3, "tag": 4}


def _type_and_size_header(kind_num: int, size: int) -> bytes:
    """Variable-length ``(type, size)`` header of a pack entry."""
    out = bytearray()
    first = (kind_num << 4) | (size & 0x0F)
    size >>= 4
    if size:
        first |= 0x80
    out.append(first)
    while size:
        chunk = size & 0x7F
        size >>= 7
        if size:
            chunk |= 0x80
        out.append(chunk)
    return bytes(out)


def _parse_type_and_size(buf: bytes, pos: int) -> tuple[int, int, int]:
    """Return ``(kind_num, size, next_pos)``."""
    first = buf[pos]
    kind_num = (first >> 4) & 0x07
    size = first & 0x0F
    shift = 4
    pos += 1
    while buf[pos - 1] & 0x80:
        size |= (buf[pos] & 0x7F) << shift
        shift += 7
        pos += 1
    return kind_num, size, pos


def _parse_ofs_offset(buf: bytes, pos: int) -> tuple[int, int]:
    """OFS_DELTA backward distance. Returns ``(distance, next_pos)``."""
    byte = buf[pos]
    pos += 1
    distance = byte & 0x7F
    while byte & 0x80:
        byte = buf[pos]
        pos += 1
        distance = ((distance + 1) << 7) | (byte & 0x7F)
    return distance, pos


def apply_delta(base: bytes, delta: bytes) -> bytes:
    """Apply a git delta stream to ``base``; result is the new object."""
    pos = 0

    def varint() -> int:
        nonlocal pos
        total = 0
        shift = 0
        while True:
            byte = delta[pos]
            pos += 1
            total |= (byte & 0x7F) << shift
            shift += 7
            if not byte & 0x80:
                return total

    src_len = varint()
    dst_len = varint()
    if src_len != len(base):
        raise ValueError("delta source length mismatch")
    out = bytearray()
    while pos < len(delta):
        op = delta[pos]
        pos += 1
        if op & 0x80:
            off = 0
            size = 0
            for i in range(4):
                if op & (1 << i):
                    off |= delta[pos] << (8 * i)
                    pos += 1
            for i in range(3):
                if op & (0x10 << i):
                    size |= delta[pos] << (8 * i)
                    pos += 1
            if size == 0:
                size = 0x10000
            out += base[off:off + size]
        elif op:
            out += delta[pos:pos + op]
            pos += op
        else:
            raise ValueError("delta opcode 0")
    if len(out) != dst_len:
        raise ValueError("delta result length mismatch")
    return bytes(out)


class PackReader:
    """Reads a packfile into ``{oid: (kind, payload)}``."""

    def __init__(self, pack: bytes):
        self.pack = pack
        if pack[:4] != b"PACK":
            raise ValueError("not a packfile")
        version, count = struct.unpack(">II", pack[4:12])
        if version != 2:
            raise ValueError("unsupported pack version")
        self.count = count

    def objects(self) -> dict[str, tuple[str, bytes]]:
        """All objects, delta chains resolved, in pack order."""
        pack = self.pack
        pos = 12
        found: dict[str, tuple[str, bytes]] = {}
        by_offset: dict[int, tuple[str, bytes]] = {}
        for _ in range(self.count):
            entry_at = pos
            kind_num, size, pos = _parse_type_and_size(pack, pos)
            if kind_num == OBJ_OFS_DELTA:
                distance, pos = _parse_ofs_offset(pack, pos)
                base_at = entry_at - distance
                if base_at not in by_offset:
                    raise ValueError("OFS delta base not yet read")
                base_kind, base_payload = by_offset[base_at]
                dobj = zlib.decompressobj()
                raw = dobj.decompress(pack[pos:])
                pos += len(pack[pos:]) - len(dobj.unused_data)
                payload = apply_delta(base_payload, raw)
                kind = base_kind
            elif kind_num == OBJ_REF_DELTA:
                base_oid = pack[pos:pos + 20].hex()
                pos += 20
                if base_oid not in found:
                    raise ValueError("REF delta base missing")
                base_kind, base_payload = found[base_oid]
                dobj = zlib.decompressobj()
                raw = dobj.decompress(pack[pos:])
                pos += len(pack[pos:]) - len(dobj.unused_data)
                payload = apply_delta(base_payload, raw)
                kind = base_kind
            else:
                kind = _KIND_BY_NUM[kind_num]
                dobj = zlib.decompressobj()
                payload = dobj.decompress(pack[pos:])
                pos += len(pack[pos:]) - len(dobj.unused_data)
                if len(payload) != size:
                    raise ValueError("pack object size mismatch")
            oid = object_id(kind, payload)
            found[oid] = (kind, payload)
            by_offset[entry_at] = (kind, payload)
        return found


class PackWriter:
    """Builds a packfile from a store of raw objects."""

    def __init__(self, store: dict[str, bytes]):
        self.store = store

    def build(self, oids: list[str]) -> bytes:
        """Pack exactly ``oids`` (in the given order), no deltas."""
        out = bytearray()
        out += b"PACK" + struct.pack(">II", 2, len(oids))
        for oid in oids:
            blob = self.store[oid]
            head, _, payload = blob.partition(b"\0")
            kind = head.split(b" ")[0].decode()
            num = _NUM_BY_KIND[kind]
            out += _type_and_size_header(num, len(payload))
            out += zlib.compress(payload)
        trailer = hashlib.sha1(bytes(out)).digest()
        out += trailer
        return bytes(out)


# --- reachability --------------------------------------------------------

def reachable_oids(store: dict[str, bytes], tips: list[str]) -> set[str]:
    """All objects reachable from ``tips`` (commit graph walk)."""
    seen: set[str] = set()
    stack = list(tips)
    while stack:
        oid = stack.pop()
        if oid in seen or oid not in store:
            continue
        seen.add(oid)
        blob = store[oid]
        kind = blob.split(b" ", 1)[0].decode()
        payload = load_raw(kind, blob)
        if kind == "commit":
            parsed = decode_commit(payload)
            stack.append(parsed["tree"])
            stack.extend(parsed["parents"])
        elif kind == "tree":
            for _mode, _name, child in decode_tree(payload):
                stack.append(child)
        elif kind == "tag":
            stack.append(decode_tag(payload)["object"])
    return seen


def is_ancestor(store: dict[str, bytes], maybe_old: str, maybe_new: str) -> bool:
    """True when ``maybe_old`` is ``maybe_new`` or one of its ancestors."""
    if maybe_old == maybe_new:
        return True
    seen: set[str] = set()
    stack = [maybe_new]
    while stack:
        oid = stack.pop()
        if oid in seen or oid not in store:
            continue
        seen.add(oid)
        blob = store[oid]
        if blob.split(b" ", 1)[0] != b"commit":
            continue
        parsed = decode_commit(load_raw("commit", blob))
        if maybe_old in parsed["parents"]:
            return True
        stack.extend(parsed["parents"])
    return False
