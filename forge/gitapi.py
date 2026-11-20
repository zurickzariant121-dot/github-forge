"""Git smart HTTP: upload-pack (v0/v2), receive-pack (v0), dumb HTTP.

A ``GitService`` owns one forge and a bounded pack cache. The wire
behaviour is the git protocol; the structure is this module's own.
"""

from __future__ import annotations

import threading

from . import model, objects as obj

AUTH_TOKEN = "test-token"

CAPS_UPLOAD_V0 = ("multi_ack thin-pack side-band side-band-64k ofs-delta "
                  "shallow deepen-since deepen-not deepen-relative "
                  "no-progress include-tag multi_ack_detailed "
                  "allow-tip-sha1-in-want allow-reachable-sha1-in-want "
                  "no-done filter agent=git/2.43.0")
CAPS_RECEIVE_V0 = ("report-status delete-refs "
                   "side-band-64k quiet atomic ofs-delta agent=git/2.43.0")
CAPS_UPLOAD_V2 = ["ls-refs=unborn", "fetch=shallow wait-for-done",
                  "server-option", "object-format=sha1"]


def authorised(headers: dict) -> bool:
    """The kit's token, as ``Bearer`` or ``token``."""
    value = headers.get("authorization", "")
    return value in ("Bearer " + AUTH_TOKEN, "token " + AUTH_TOKEN)


class PackCache:
    """Bounded memo of built packs, keyed by the wanted oid set."""

    def __init__(self, limit: int = 64):
        self.limit = limit
        self._entries: dict[tuple, bytes] = {}
        self._lock = threading.Lock()

    def lookup(self, key: tuple) -> bytes | None:
        return self._entries.get(key)

    def store(self, key: tuple, pack: bytes) -> None:
        with self._lock:
            if key in self._entries:
                return
            if len(self._entries) >= self.limit:
                self._entries.pop(next(iter(self._entries)))
            self._entries[key] = pack


def _advertisement_v2() -> bytes:
    """Protocol v2 capability advertisement (no refs)."""
    out = bytearray()
    out += obj.frame_line(b"# service=git-upload-pack\n")
    out += obj.frame_line(None)
    out += obj.frame_line(b"version 2\n")
    for cap in CAPS_UPLOAD_V2:
        out += obj.frame_line((cap + "\n").encode())
    out += obj.frame_line(None)
    return bytes(out)


def _advertisement(service: str, caps: str,
                   refs: list[tuple[str, str]]) -> bytes:
    """``# service=...`` + flush + ref lines (caps on the first).

    An empty repository advertises a single ``capabilities^{}`` line
    under the zero oid, exactly like git does for a repo with no refs.
    """
    out = bytearray()
    out += obj.frame_line(f"# service={service}\n".encode())
    out += obj.frame_line(None)
    if not refs:
        out += obj.frame_line(
            f"{obj.ZERO_OID} capabilities^{{}}\0{caps}\n".encode())
        out += obj.frame_line(None)
        return bytes(out)
    for index, (oid, ref) in enumerate(refs):
        if index == 0:
            out += obj.frame_line(f"{oid} {ref}\0{caps}\n".encode())
        else:
            out += obj.frame_line(f"{oid} {ref}\n".encode())
    out += obj.frame_line(None)
    return bytes(out)


def _sorted_refs(repo: model.Repository) -> list[tuple[str, str]]:
    return sorted(((oid, ref) for ref, oid in repo.refs.items()),
                  key=lambda pair: pair[1])


def _held_objects(repo: model.Repository, haves: list[str],
                  shallow: list[str]) -> set[str]:
    """Objects the client already holds.

    A ``have`` that is also a shallow boundary is held with its tree
    only; its parents are NOT held (that is what makes it shallow).
    """
    held: set[str] = set()
    shallow_set = set(shallow)
    for have in haves:
        if have in shallow_set:
            held.add(have)
            blob = repo.objects.get(have)
            if blob is not None and blob.split(b" ", 1)[0] == b"commit":
                tree = obj.decode_commit(
                    obj.load_raw("commit", blob))["tree"]
                held |= obj.reachable_oids(repo.objects, [tree])
        else:
            held |= obj.reachable_oids(repo.objects, [have])
    return held


def _depth_limited(store: dict[str, bytes], tips: list[str],
                   depth: int) -> tuple[set[str], set[str]]:
    """Objects within ``depth`` commit generations of ``tips``.

    Returns ``(objects, boundary)`` where ``boundary`` holds the commits
    whose parents were cut off (the shallow boundary). A commit's tree
    and blobs are always included whole.
    """
    included: set[str] = set()
    boundary: set[str] = set()
    queue: list[tuple[str, int]] = [(oid, 1) for oid in tips]
    visited: set[str] = set()
    while queue:
        oid, generation = queue.pop(0)
        if oid in visited or oid not in store:
            continue
        blob = store[oid]
        if blob.split(b" ", 1)[0] != b"commit":
            continue
        visited.add(oid)
        parsed = obj.decode_commit(obj.load_raw("commit", blob))
        included.add(oid)
        included |= obj.reachable_oids(store, [parsed["tree"]])
        if generation >= depth:
            boundary.add(oid)
        else:
            for parent in parsed["parents"]:
                queue.append((parent, generation + 1))
    return included, boundary


def _sideband_pack(pack: bytes) -> bytes:
    """Pack bytes as sideband channel-1 frames.

    A pkt-line is at most 65520 bytes including its 4-byte length, so
    one band byte leaves 65515 data bytes per frame.
    """
    out = bytearray()
    pos = 0
    while pos < len(pack):
        chunk = pack[pos:pos + 65515]
        out += obj.frame_line(b"\x01" + chunk)
        pos += len(chunk)
    out += obj.frame_line(None)
    return bytes(out)


class GitService:
    """Handles every git-protocol request for one forge."""

    def __init__(self, forge: model.ForgeState):
        self.forge = forge
        self.packs = PackCache()

    # -- advertisement --------------------------------------------------

    def info_refs(self, repo: model.Repository, service: str,
                  protocol: int = 0) -> bytes:
        refs = _sorted_refs(repo)
        if service == "git-upload-pack":
            if protocol == 2:
                return _advertisement_v2()
            caps = CAPS_UPLOAD_V0
            head_oid = repo.refs.get("refs/heads/" + repo.default_branch)
            if head_oid:
                caps += (" symref=HEAD:refs/heads/"
                         + repo.default_branch)
                refs = [(head_oid, "HEAD")] + refs
            peeled: list[tuple[str, str]] = []
            for oid, ref in refs:
                if not ref.startswith("refs/tags/"):
                    continue
                blob = repo.objects.get(oid)
                if blob is not None and blob.split(b" ", 1)[0] == b"tag":
                    target = obj.decode_tag(
                        obj.load_raw("tag", blob))["object"]
                    peeled.append((target, ref + "^{}"))
            return _advertisement(service, caps, refs + peeled)
        if service == "git-receive-pack":
            return _advertisement(service, CAPS_RECEIVE_V0, refs)
        raise ValueError("unknown service")

    def dumb_refs(self, repo: model.Repository) -> bytes:
        out = bytearray()
        for oid, ref in _sorted_refs(repo):
            out += f"{oid}\t{ref}\n".encode()
        return bytes(out)

    # -- upload-pack v0 -------------------------------------------------

    def upload_v0(self, repo: model.Repository, body: bytes) -> bytes:
        reader = obj.PacketReader(body)
        wants: list[str] = []
        haves: list[str] = []
        shallow: list[str] = []
        deepen: int | None = None
        # The request has two phases: wants (then a flush) and haves.
        # A flush packet is not the end of the stream.
        while reader.pos < len(body):
            line = reader.next()
            if line is None:
                continue  # flush between the want and have phases
            text = line.decode()
            if text.startswith("want "):
                wants.append(text.split()[1])
            elif text.startswith("have "):
                haves.append(text.split()[1])
            elif text.startswith("shallow "):
                shallow.append(text.split()[1])
            elif text.startswith("deepen "):
                deepen = int(text.split()[1])
            elif text == "done":
                break
        if deepen is not None:
            wanted, boundary = _depth_limited(repo.objects, wants, deepen)
        else:
            wanted = obj.reachable_oids(repo.objects, wants)
            boundary = set()
        already = _held_objects(repo, haves, shallow)
        missing = sorted(wanted - already)
        pack = self._pack_for(repo, tuple(missing))
        out = bytearray()
        out += obj.frame_line(b"NAK\n")
        for oid in sorted(boundary):
            out += obj.frame_line(f"shallow {oid}\n".encode())
        out += _sideband_pack(pack)
        return bytes(out)

    # -- upload-pack v2 -------------------------------------------------

    def upload_v2(self, repo: model.Repository, body: bytes) -> bytes:
        reader = obj.PacketReader(body)
        responses = bytearray()
        while True:
            command = self._read_command(reader)
            if command is None:
                break
            name, args = command
            if name == "ls-refs":
                responses += self._ls_refs(repo, args)
            elif name == "fetch":
                responses += self._fetch(repo, args)
            else:
                raise ValueError("unknown v2 command: " + name)
        return bytes(responses)

    @staticmethod
    def _read_command(reader: obj.PacketReader) -> tuple[str, list[str]] | None:
        """One ``command=...`` block.

        A v2 command is ``command=<name>``, capability arguments, a
        delim, command arguments, then a flush. Both argument groups
        are returned together.
        """
        first = reader.next()
        if first is None:
            return None
        if not first.startswith(b"command="):
            raise ValueError("expected command= line")
        name = first[len(b"command="):].decode().strip()
        args: list[str] = []
        while True:
            item = reader.next()
            if item is None:
                return name, args
            if item == b"":
                continue  # delim: command arguments follow
            args.append(item.decode().strip())
        # unreachable

    def _ls_refs(self, repo: model.Repository,
                 args: list[str]) -> bytes:
        peel = "peel" in args
        unborn = "unborn" in args
        prefixes = [a.split(" ", 1)[1] for a in args
                    if a.startswith("ref-prefix ")]
        out = bytearray()
        head_oid = repo.refs.get("refs/heads/" + repo.default_branch)
        show_head = (not prefixes
                     or any("HEAD".startswith(p) for p in prefixes))
        if show_head:
            if head_oid:
                out += obj.frame_line(
                    f"{head_oid} HEAD symref-target:refs/heads/"
                    f"{repo.default_branch}\n".encode())
            elif unborn:
                out += obj.frame_line(
                    b"unborn HEAD symref-target:refs/heads/"
                    + repo.default_branch.encode() + b"\n")
        for oid, ref in _sorted_refs(repo):
            if prefixes and not any(ref.startswith(p) for p in prefixes):
                continue
            line = f"{oid} {ref}"
            if peel:
                blob = repo.objects.get(oid)
                if blob is not None and blob.split(b" ", 1)[0] == b"tag":
                    parsed = obj.decode_tag(obj.load_raw("tag", blob))
                    # v2 reports the peel as an attribute on the tag's
                    # own line, not as a separate ^{} line.
                    line += f" peeled:{parsed['object']}"
            out += obj.frame_line((line + "\n").encode())
        out += obj.frame_line(None)
        return bytes(out)

    def _fetch(self, repo: model.Repository, args: list[str]) -> bytes:
        wants: list[str] = []
        haves: list[str] = []
        shallow: list[str] = []
        deepen: int | None = None
        for text in args:
            text = text.strip()
            if text.startswith("want "):
                wants.append(text.split()[1])
            elif text.startswith("have "):
                haves.append(text.split()[1])
            elif text.startswith("shallow "):
                shallow.append(text.split()[1])
            elif text.startswith("deepen "):
                deepen = int(text.split()[1])
            elif text == "done":
                break
        if deepen is not None:
            wanted, boundary = _depth_limited(repo.objects, wants, deepen)
        else:
            wanted = obj.reachable_oids(repo.objects, wants)
            boundary = set()
        already = _held_objects(repo, haves, shallow)
        missing = sorted(wanted - already)
        pack = self._pack_for(repo, tuple(missing))
        out = bytearray()
        # acknowledgments section
        out += obj.frame_line(b"acknowledgments\n")
        out += obj.frame_line(b"NAK\n")
        out += obj.frame_delim()
        # shallow-info section, only when a depth was requested
        if deepen is not None:
            out += obj.frame_line(b"shallow-info\n")
            for oid in sorted(boundary):
                out += obj.frame_line(f"shallow {oid}\n".encode())
            out += obj.frame_delim()
        # packfile section, sideband band-1 frames
        out += obj.frame_line(b"packfile\n")
        out += _sideband_pack(pack)
        return bytes(out)

    # -- receive-pack v0 ------------------------------------------------

    def receive(self, repo: model.Repository, body: bytes) -> bytes:
        reader = obj.PacketReader(body)
        updates: list[tuple[str, str, str]] = []
        caps = ""
        first = True
        while True:
            line = reader.next()
            if line is None:
                break
            text = line.decode()
            # Capabilities ride after a NUL on the first line.
            if first and "\0" in text:
                text, caps = text.split("\0", 1)
                first = False
            parts = text.split()
            if len(parts) == 3:
                updates.append((parts[0], parts[1], parts[2]))
        # Everything after the flush is the packfile.
        pack_bytes = body[reader.pos:]
        if pack_bytes:
            incoming = obj.PackReader(pack_bytes).objects()
            for oid, (kind, payload) in incoming.items():
                repo.objects[oid] = obj.raw_bytes(kind, payload)
        report = bytearray()
        report += b"unpack ok\n"
        for old, new, ref in updates:
            problem = self._validate_update(repo, old, new, ref)
            if problem:
                report += f"ng {ref} {problem}\n".encode()
                continue
            if new == obj.ZERO_OID:
                repo.delete_ref(ref)
            else:
                repo.update_ref(ref, new)
            report += f"ok {ref}\n".encode()
        # When the client asked for side-band, the whole status report
        # travels as sideband band-1 (data) frames; band 2 is progress
        # text and band 3 is an error. A bare pkt-line would be read
        # as a band byte and rejected.
        if "side-band" in caps:
            return _sideband_text(bytes(report), band=1)
        out = bytearray()
        for line in bytes(report).splitlines(keepends=True):
            out += obj.frame_line(line)
        out += obj.frame_line(None)
        return bytes(out)


    @staticmethod
    def _validate_update(repo: model.Repository, old: str, new: str,
                         ref: str) -> str | None:
        """Return an error string, or ``None`` when the update is fine."""
        current = repo.refs.get(ref)
        if old != obj.ZERO_OID and current != old:
            return "non-fast-forward"
        if new == obj.ZERO_OID:
            return None  # delete
        if old == obj.ZERO_OID:
            return None  # new branch
        if not obj.is_ancestor(repo.objects, old, new):
            return "non-fast-forward"
        return None

    # -- pack building --------------------------------------------------

    def _pack_for(self, repo: model.Repository,
                  oids: tuple[str, ...]) -> bytes:
        key = (repo.owner, repo.name, oids)
        cached = self.packs.lookup(key)
        if cached is not None:
            return cached
        pack = obj.PackWriter(repo.objects).build(list(oids))
        self.packs.store(key, pack)
        return pack


def _sideband_text(data: bytes, band: int = 2) -> bytes:
    """Progress/error text as sideband frames (max 65515 data bytes)."""
    out = bytearray()
    pos = 0
    while pos < len(data):
        chunk = data[pos:pos + 65515]
        out += obj.frame_line(bytes([band]) + chunk)
        pos += len(chunk)
    out += obj.frame_line(None)
    return bytes(out)
