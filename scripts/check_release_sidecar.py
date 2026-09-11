#!/usr/bin/python3
# ruff: noqa: UP006, UP035, UP045
from __future__ import annotations

import bz2
import ctypes
import errno
import gzip
import hashlib
import io
import lzma
import math
import os
import re
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
import unicodedata
import zipfile
from dataclasses import dataclass, field
from typing import IO, BinaryIO, Dict, Iterable, List, Optional, Sequence, Tuple, cast

POLICY = "PDF2MD_BUNDLE_POLICY_M0_ADHOC_HARDENED_NOT_NOTARIZED"
CLEAN_ENV = {"PATH": "/usr/bin:/bin", "LC_ALL": "C", "LANG": "C"}
TOOLS = {
    "codesign": "/usr/bin/codesign",
    "lipo": "/usr/bin/lipo",
    "otool": "/usr/bin/otool",
}

MAX_TREE_ENTRIES = 200_000
MAX_XATTR_BYTES = 16 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 10_000
MAX_BUNDLE_ARCHIVE_MEMBERS = 50_000
MAX_ARCHIVE_DEPTH = 3
MAX_ARCHIVE_MEMBER_BYTES = 128 * 1024 * 1024
MAX_ARCHIVE_TOTAL_BYTES = 512 * 1024 * 1024
MAX_COMPRESSION_RATIO = 200
STREAM_OVERLAP = 8 * 1024
MAX_DMG_PRESENTATION_BYTES = 32 * 1024 * 1024
CONTAINER_PROBE_HEAD_BYTES = 16 * 2048 + 8
CONTAINER_PROBE_TAIL_BYTES = 512

UNSUPPORTED_CONTAINER_SUFFIXES = (
    ".zst",
    ".zstd",
    ".lz4",
    ".cpio",
    ".7z",
    ".rar",
    ".cab",
    ".z",
    ".lz",
    ".rpm",
    ".squashfs",
    ".dmg",
    ".udif",
    ".sparseimage",
    ".sparsebundle",
    ".hdi",
    ".iso",
    ".iso9660",
    ".udf",
    ".cdr",
    ".nrg",
    ".toast",
    ".img",
    ".ima",
    ".dsk",
    ".vhd",
    ".vhdx",
    ".avhd",
    ".avhdx",
    ".qcow",
    ".qcow2",
    ".qed",
    ".vmdk",
    ".vdi",
    ".hdd",
    ".xar",
    ".pkg",
    ".mpkg",
    ".wim",
    ".swm",
    ".esd",
    ".ova",
)
UNSUPPORTED_CONTAINER_PREFIX_MAGICS = (
    b"\x28\xb5\x2f\xfd",
    b"\x22\xb5\x2f\xfd",
    b"\x04\x22\x4d\x18",
    b"\x02\x21\x4c\x18",
    b"070701",
    b"070702",
    b"070707",
    b"\x71\xc7",
    b"\xc7\x71",
    b"7z\xbc\xaf'\x1c",
    b"Rar!\x1a\x07",
    b"MSCF",
    b"\x1f\x9d",
    b"LZIP",
    b"\xed\xab\xee\xdb",
    b"hsqs",
    b"sqsh",
    b"vhdxfile",
    b"QFI\xfb",
    b"QED\x00",
    b"xar!",
    b"KDMV",
    b"MSWIM\x00\x00\x00",
    b"sprs",
    b"<<< Oracle VM VirtualBox Disk Image >>>",
)
OPTICAL_IMAGE_MAGICS = (b"CD001", b"CDROM", b"BEA01", b"NSR02", b"NSR03", b"TEA01")

MACHO_MAGICS = {
    b"\xfe\xed\xfa\xce": (">", False),
    b"\xce\xfa\xed\xfe": ("<", False),
    b"\xfe\xed\xfa\xcf": (">", False),
    b"\xcf\xfa\xed\xfe": ("<", False),
}
FAT_MAGICS = {
    b"\xca\xfe\xba\xbe": (">", 20),
    b"\xbe\xba\xfe\xca": ("<", 20),
    b"\xca\xfe\xba\xbf": (">", 32),
    b"\xbf\xba\xfe\xca": ("<", 32),
}
CPU_ARCHITECTURES = {
    0x0100000C: "arm64",
    0x01000007: "x86_64",
    12: "arm",
    7: "i386",
}

PREFIX_SECRET_RE = re.compile(
    rb"(?:"
    rb"sk-[A-Za-z0-9_-]{32,}"
    rb"|AKID[A-Za-z0-9]{12,}"
    rb"|AKIA[0-9A-Z]{16}"
    rb"|AIza[0-9A-Za-z_-]{30,}"
    rb"|ghp_[0-9A-Za-z]{30,}"
    rb"|github_pat_[0-9A-Za-z_]{40,}"
    rb"|xox[baprs]-[0-9A-Za-z-]{20,}"
    rb")"
)
ASSIGNED_SECRET_RE = re.compile(
    rb"(?ix)"
    rb"(?:^|[\s{,;])"
    rb"[\"']?"
    rb"([A-Z0-9_.-]*(?:API[_-]?KEY|SECRET(?:[_-]?KEY)?|TOKEN|PASSWORD|PASSWD)"
    rb"[A-Z0-9_.-]*)"
    rb"[\"']?\s*(?::=|=>|=|:)\s*[\"']?"
    rb"([A-Za-z0-9+/_=-]{20,})"
)
PEM_PRIVATE_KEY_RE = re.compile(
    rb"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"
)
JWT_RE = re.compile(
    rb"(?<![A-Za-z0-9_-])"
    rb"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{20,}"
    rb"(?![A-Za-z0-9_-])"
)
DEVELOPMENT_PATH_RE = re.compile(
    rb"(?<![A-Za-z0-9_])/"
    rb"(?:"
    rb"Users/[A-Za-z0-9._-]+(?:/[A-Za-z0-9._@+ -]+)+"
    rb"|Volumes/[A-Za-z0-9._@+ -]+(?:/[A-Za-z0-9._@+ -]+)*"
    rb"|private/tmp/[A-Za-z0-9._@+ -]+(?:/[A-Za-z0-9._@+ -]+)*"
    rb"|private/var/folders/[A-Za-z0-9._@+ -]+(?:/[A-Za-z0-9._@+ -]+)*"
    rb"|var/folders/[A-Za-z0-9._@+ -]+(?:/[A-Za-z0-9._@+ -]+)*"
    rb"|tmp/[A-Za-z0-9._@+ -]+(?:/[A-Za-z0-9._@+ -]+)*"
    rb"|opt/homebrew(?:/[A-Za-z0-9._@+ -]+)+"
    rb"|usr/local(?:/[A-Za-z0-9._@+ -]+)+"
    rb"|usr/bin/python[0-9.]*"
    rb"|Library/Frameworks(?:/[A-Za-z0-9._@+ -]+)+"
    rb")"
)


class GateError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Entry:
    relative: str
    kind: str
    mode: int
    uid: int
    gid: int
    device: int
    inode: int
    links: int
    size: int
    mtime_ns: int
    ctime_ns: int
    digest: str = ""
    xattr_digest: str = ""
    symlink_target: str = ""
    macho_archs: Tuple[str, ...] = ()
    head: bytes = b""

    def fingerprint_bytes(self) -> bytes:
        values = (
            self.relative,
            self.kind,
            str(self.mode),
            str(self.uid),
            str(self.gid),
            str(self.device),
            str(self.inode),
            str(self.links),
            str(self.size),
            str(self.mtime_ns),
            str(self.ctime_ns),
            self.digest,
            self.xattr_digest,
            self.symlink_target,
            ",".join(self.macho_archs),
        )
        return "\0".join(values).encode("utf-8", "surrogateescape")


@dataclass(frozen=True)
class Snapshot:
    app: str
    contents: str
    entries: Dict[str, Entry]
    fingerprint: str

    def required(self, relative: str, kind: str) -> Entry:
        entry = self.entries.get(relative)
        if entry is None or entry.kind != kind:
            raise GateError("PDF2MD_BUNDLE_E_REQUIRED_PATH")
        return entry

    def absolute(self, entry: Entry) -> str:
        return os.path.join(self.contents, entry.relative) if entry.relative else self.contents

    def resolved_entry(self, absolute: str) -> Optional[Entry]:
        resolved = os.path.realpath(absolute)
        try:
            if os.path.commonpath((self.app, resolved)) != self.app:
                return None
        except ValueError:
            return None
        try:
            relative = os.path.relpath(resolved, self.contents)
        except ValueError:
            return None
        if relative == ".":
            relative = ""
        return self.entries.get(relative)


@dataclass
class AggregateArchiveBudget:
    members: int = 0
    expanded_bytes: int = 0

    def account_member(self) -> None:
        self.members += 1
        self._check()

    def account_bytes(self, size: int) -> None:
        self.expanded_bytes += size
        self._check()

    def _check(self) -> None:
        if (
            self.members > MAX_BUNDLE_ARCHIVE_MEMBERS
            or self.expanded_bytes > MAX_ARCHIVE_TOTAL_BYTES
        ):
            raise GateError("PDF2MD_BUNDLE_E_ARCHIVE_LIMIT")


@dataclass
class ArchiveBudget:
    aggregate: AggregateArchiveBudget = field(default_factory=AggregateArchiveBudget)
    members: int = 0
    expanded_bytes: int = 0

    def account_member(self) -> None:
        self.members += 1
        self.aggregate.account_member()
        self._check()

    def account_bytes(self, size: int) -> None:
        self.expanded_bytes += size
        self.aggregate.account_bytes(size)
        self._check()

    def _check(self) -> None:
        if self.members > MAX_ARCHIVE_MEMBERS or self.expanded_bytes > MAX_ARCHIVE_TOTAL_BYTES:
            raise GateError("PDF2MD_BUNDLE_E_ARCHIVE_LIMIT")


def _contains_control(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def _entropy(value: bytes) -> float:
    counts: Dict[int, int] = {}
    for byte in value:
        counts[byte] = counts.get(byte, 0) + 1
    length = len(value)
    return -sum((count / length) * math.log2(count / length) for count in counts.values())


def _looks_high_entropy(value: bytes) -> bool:
    if len(value) < 20:
        return False
    lowered = value.lower()
    if any(
        marker in lowered
        for marker in (b"placeholder", b"example", b"dummy", b"changeme", b"not-a-secret")
    ):
        return False
    if re.fullmatch(rb"[A-Z0-9]{24,}", value):
        return True
    if re.fullmatch(rb"[a-f0-9]{32,}", value):
        return True
    categories = sum(
        (
            bool(re.search(rb"[a-z]", value)),
            bool(re.search(rb"[A-Z]", value)),
            bool(re.search(rb"[0-9]", value)),
            bool(re.search(rb"[^A-Za-z0-9]", value)),
        )
    )
    return categories >= 3 and _entropy(value) >= 3.5


def _is_expected_digest_assignment(name: bytes, value: bytes) -> bool:
    normalized_name = name.upper().replace(b"-", b"_").replace(b".", b"_")
    expected_lengths = {
        b"_MD5": (32,),
        b"_SHA1": (40,),
        b"_SHA224": (56,),
        b"_SHA256": (64,),
        b"_SHA384": (96,),
        b"_SHA512": (128,),
        b"_DIGEST": (32, 40, 56, 64, 96, 128),
        b"_CHECKSUM": (32, 40, 56, 64, 96, 128),
    }
    for suffix, lengths in expected_lengths.items():
        if normalized_name.endswith(suffix):
            return len(value) in lengths and re.fullmatch(rb"[0-9A-Fa-f]+", value) is not None
    return False


def _allows_upstream_build_paths(relative: str) -> bool:
    runtime_prefix = "Resources/python-runtime/"
    project_prefix = f"{runtime_prefix}lib/python3.12/site-packages/parsing_core/"
    return relative.startswith(runtime_prefix) and not relative.startswith(project_prefix)


def _inspect_patterns(data: bytes, *, allow_upstream_build_paths: bool = False) -> None:
    if PREFIX_SECRET_RE.search(data) or PEM_PRIVATE_KEY_RE.search(data) or JWT_RE.search(data):
        raise GateError("PDF2MD_BUNDLE_E_CREDENTIAL")
    for match in ASSIGNED_SECRET_RE.finditer(data):
        name = match.group(1).upper()
        if name.endswith((b"_ENV", b"_NAME", b"_FIELD", b"_LABEL", b"_PATH", b"_PREFIX")):
            continue
        value = match.group(2)
        if _is_expected_digest_assignment(name, value):
            continue
        if _looks_high_entropy(value):
            raise GateError("PDF2MD_BUNDLE_E_CREDENTIAL")
    for _match in DEVELOPMENT_PATH_RE.finditer(data):
        if allow_upstream_build_paths:
            continue
        raise GateError("PDF2MD_BUNDLE_E_DEVELOPMENT_PATH")


class StreamInspector:
    def __init__(self, *, allow_upstream_build_paths: bool = False) -> None:
        self.tail = b""
        self.allow_upstream_build_paths = allow_upstream_build_paths

    def feed(self, data: bytes) -> None:
        combined = self.tail + data
        _inspect_patterns(
            combined,
            allow_upstream_build_paths=self.allow_upstream_build_paths,
        )
        self.tail = combined[-STREAM_OVERLAP:]


_LIBC = ctypes.CDLL(None, use_errno=True)
_FLISTXATTR = _LIBC.flistxattr
_FLISTXATTR.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
_FLISTXATTR.restype = ctypes.c_ssize_t
_FGETXATTR = _LIBC.fgetxattr
_FGETXATTR.argtypes = [
    ctypes.c_int,
    ctypes.c_char_p,
    ctypes.c_void_p,
    ctypes.c_size_t,
    ctypes.c_uint32,
    ctypes.c_int,
]
_FGETXATTR.restype = ctypes.c_ssize_t
_LISTXATTR = _LIBC.listxattr
_LISTXATTR.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
_LISTXATTR.restype = ctypes.c_ssize_t
_GETXATTR = _LIBC.getxattr
_GETXATTR.argtypes = [
    ctypes.c_char_p,
    ctypes.c_char_p,
    ctypes.c_void_p,
    ctypes.c_size_t,
    ctypes.c_uint32,
    ctypes.c_int,
]
_GETXATTR.restype = ctypes.c_ssize_t
XATTR_NOFOLLOW = 0x0001


def _xattr_call(function: object, *arguments: object) -> int:
    result = function(*arguments)  # type: ignore[operator]
    if result < 0:
        error = ctypes.get_errno()
        if error == errno.ENOTSUP:
            return 0
        raise OSError(error, os.strerror(error))
    return int(result)


def _fd_xattrs(fd: int, *, inspect_content: bool) -> Tuple[str, int]:
    try:
        size = _xattr_call(_FLISTXATTR, fd, None, 0, 0)
        if size == 0:
            return "", 0
        if size > MAX_XATTR_BYTES:
            raise GateError("PDF2MD_BUNDLE_E_SCAN")
        names_buffer = ctypes.create_string_buffer(size)
        actual = _xattr_call(_FLISTXATTR, fd, names_buffer, size, 0)
        names = sorted(name for name in names_buffer.raw[:actual].split(b"\0") if name)
        digest = hashlib.sha256()
        total = 0
        for name in names:
            value_size = _xattr_call(_FGETXATTR, fd, name, None, 0, 0, 0)
            total += len(name) + value_size
            if value_size > MAX_XATTR_BYTES or total > MAX_XATTR_BYTES:
                raise GateError("PDF2MD_BUNDLE_E_SCAN")
            value_buffer = ctypes.create_string_buffer(value_size or 1)
            value_actual = _xattr_call(
                _FGETXATTR,
                fd,
                name,
                value_buffer,
                value_size,
                0,
                0,
            )
            value = value_buffer.raw[:value_actual]
            if inspect_content:
                _inspect_patterns(name)
                _inspect_patterns(value)
            digest.update(struct.pack(">I", len(name)))
            digest.update(name)
            digest.update(struct.pack(">Q", len(value)))
            digest.update(value)
        return digest.hexdigest(), total
    except GateError:
        raise
    except (OSError, OverflowError, ValueError) as error:
        raise GateError("PDF2MD_BUNDLE_E_SCAN") from error


def _symlink_xattrs(path: str, *, inspect_content: bool) -> str:
    encoded_path = os.fsencode(path)
    try:
        size = _xattr_call(_LISTXATTR, encoded_path, None, 0, XATTR_NOFOLLOW)
        if size == 0:
            return ""
        if size > MAX_XATTR_BYTES:
            raise GateError("PDF2MD_BUNDLE_E_SCAN")
        names_buffer = ctypes.create_string_buffer(size)
        actual = _xattr_call(
            _LISTXATTR,
            encoded_path,
            names_buffer,
            size,
            XATTR_NOFOLLOW,
        )
        names = sorted(name for name in names_buffer.raw[:actual].split(b"\0") if name)
        digest = hashlib.sha256()
        total = 0
        for name in names:
            value_size = _xattr_call(
                _GETXATTR,
                encoded_path,
                name,
                None,
                0,
                0,
                XATTR_NOFOLLOW,
            )
            total += len(name) + value_size
            if value_size > MAX_XATTR_BYTES or total > MAX_XATTR_BYTES:
                raise GateError("PDF2MD_BUNDLE_E_SCAN")
            value_buffer = ctypes.create_string_buffer(value_size or 1)
            value_actual = _xattr_call(
                _GETXATTR,
                encoded_path,
                name,
                value_buffer,
                value_size,
                0,
                XATTR_NOFOLLOW,
            )
            value = value_buffer.raw[:value_actual]
            if inspect_content:
                _inspect_patterns(name)
                _inspect_patterns(value)
            digest.update(struct.pack(">I", len(name)))
            digest.update(name)
            digest.update(struct.pack(">Q", len(value)))
            digest.update(value)
        return digest.hexdigest()
    except GateError:
        raise
    except (OSError, OverflowError, ValueError) as error:
        raise GateError("PDF2MD_BUNDLE_E_SCAN") from error


def _architecture_name(cpu_type: int) -> str:
    return CPU_ARCHITECTURES.get(cpu_type & 0xFFFFFFFF, "unknown-%08x" % (cpu_type & 0xFFFFFFFF))


def _macho_architectures(head: bytes) -> Tuple[str, ...]:
    if len(head) < 8:
        return ()
    magic = head[:4]
    if magic in MACHO_MAGICS:
        endian, _ = MACHO_MAGICS[magic]
        cpu_type = struct.unpack(endian + "I", head[4:8])[0]
        return (_architecture_name(cpu_type),)
    if magic not in FAT_MAGICS:
        return ()
    endian, record_size = FAT_MAGICS[magic]
    count = struct.unpack(endian + "I", head[4:8])[0]
    if count == 0 or count > 64 or len(head) < 8 + count * record_size:
        raise GateError("PDF2MD_BUNDLE_E_MACHO_INSPECTION")
    architectures = []
    for index in range(count):
        offset = 8 + index * record_size
        cpu_type = struct.unpack(endian + "I", head[offset : offset + 4])[0]
        architectures.append(_architecture_name(cpu_type))
    return tuple(sorted(set(architectures)))


def _safe_archive_name(name: str, seen: set[str]) -> None:
    if _contains_control(name) or "\x00" in name:
        raise GateError("PDF2MD_BUNDLE_E_ARCHIVE_UNSAFE")
    normalized = name.replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:/", normalized):
        raise GateError("PDF2MD_BUNDLE_E_ARCHIVE_UNSAFE")
    components = [component for component in normalized.split("/") if component not in ("", ".")]
    if not components or any(component == ".." for component in components):
        raise GateError("PDF2MD_BUNDLE_E_ARCHIVE_UNSAFE")
    canonical = unicodedata.normalize("NFC", "/".join(components)).casefold()
    if canonical in seen:
        raise GateError("PDF2MD_BUNDLE_E_ARCHIVE_UNSAFE")
    seen.add(canonical)
    _inspect_patterns(normalized.encode("utf-8", "strict"))


def _archive_kind(name: str, head: bytes) -> Optional[str]:
    lowered = name.casefold()
    zstd_skippable = len(head) >= 4 and 0x50 <= head[0] <= 0x5F and head[1:4] == b"\x2a\x4d\x18"
    optical_magic = head[16 * 2048 + 1 : 16 * 2048 + 6]
    udif_footer = len(head) >= 512 and head[-512:-508] == b"koly"
    vhd_footer = len(head) >= 512 and head[-512:-504] == b"conectix"
    nrg_footer = (len(head) >= 12 and head[-12:-8] == b"NER5") or (
        len(head) >= 8 and head[-8:-4] == b"NERO"
    )
    vdi_signature = len(head) >= 68 and head[64:68] == b"\x7f\x10\xda\xbe"
    if (
        lowered.endswith(UNSUPPORTED_CONTAINER_SUFFIXES)
        or head.startswith(UNSUPPORTED_CONTAINER_PREFIX_MAGICS)
        or zstd_skippable
        or optical_magic in OPTICAL_IMAGE_MAGICS
        or udif_footer
        or head.startswith(b"conectix")
        or vhd_footer
        or nrg_footer
        or vdi_signature
    ):
        raise GateError("PDF2MD_BUNDLE_E_ARCHIVE_UNSUPPORTED")
    if lowered.endswith((".zip", ".whl", ".jar", ".egg")) or head.startswith(b"PK"):
        return "zip"
    if lowered.endswith((".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz")):
        return "tar"
    if head.startswith(b"!<arch>\n") or lowered.endswith(".a"):
        return "ar"
    if lowered.endswith(".gz") or head.startswith(b"\x1f\x8b"):
        return "gzip"
    if lowered.endswith(".bz2") or head.startswith(b"BZh"):
        return "bz2"
    if lowered.endswith(".xz") or head.startswith(b"\xfd7zXZ\x00"):
        return "xz"
    if len(head) >= 265 and head[257:262] == b"ustar":
        return "tar"
    return None


def _stream_member(
    source: IO[bytes],
    *,
    declared_size: int,
    name: str,
    depth: int,
    budget: ArchiveBudget,
    allow_upstream_build_paths: bool = False,
) -> None:
    if declared_size < 0 or declared_size > MAX_ARCHIVE_MEMBER_BYTES:
        raise GateError("PDF2MD_BUNDLE_E_ARCHIVE_LIMIT")
    budget.account_bytes(declared_size)
    inspector = StreamInspector(allow_upstream_build_paths=allow_upstream_build_paths)
    actual = 0
    with tempfile.TemporaryFile() as copy:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            actual += len(chunk)
            if actual > declared_size or actual > MAX_ARCHIVE_MEMBER_BYTES:
                raise GateError("PDF2MD_BUNDLE_E_ARCHIVE_LIMIT")
            inspector.feed(chunk)
            copy.write(chunk)
        if actual != declared_size:
            raise GateError("PDF2MD_BUNDLE_E_ARCHIVE")
        copy.seek(0)
        head = copy.read(CONTAINER_PROBE_HEAD_BYTES)
        if actual > len(head):
            copy.seek(max(0, actual - CONTAINER_PROBE_TAIL_BYTES))
            head += copy.read(CONTAINER_PROBE_TAIL_BYTES)
        copy.seek(0)
        kind = _archive_kind(name, head)
        if kind is not None:
            _inspect_archive(
                copy,
                kind=kind,
                name=name,
                depth=depth + 1,
                budget=budget,
                allow_upstream_build_paths=allow_upstream_build_paths,
            )


def _inspect_zip(
    stream: BinaryIO,
    *,
    depth: int,
    budget: ArchiveBudget,
    allow_upstream_build_paths: bool = False,
) -> None:
    seen: set[str] = set()
    try:
        with zipfile.ZipFile(stream) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_ARCHIVE_MEMBERS:
                raise GateError("PDF2MD_BUNDLE_E_ARCHIVE_LIMIT")
            for info in infos:
                budget.account_member()
                _safe_archive_name(info.filename, seen)
                if info.flag_bits & 0x1:
                    raise GateError("PDF2MD_BUNDLE_E_ARCHIVE_UNSAFE")
                mode = (info.external_attr >> 16) & 0xFFFF
                file_type = stat.S_IFMT(mode)
                if file_type and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                    raise GateError("PDF2MD_BUNDLE_E_ARCHIVE_UNSAFE")
                if info.is_dir():
                    continue
                if info.file_size > MAX_ARCHIVE_MEMBER_BYTES:
                    raise GateError("PDF2MD_BUNDLE_E_ARCHIVE_LIMIT")
                if (
                    info.file_size > 1024 * 1024
                    and info.file_size > max(1, info.compress_size) * MAX_COMPRESSION_RATIO
                ):
                    raise GateError("PDF2MD_BUNDLE_E_ARCHIVE_LIMIT")
                with archive.open(info, "r") as member:
                    _stream_member(
                        member,
                        declared_size=info.file_size,
                        name=info.filename,
                        depth=depth,
                        budget=budget,
                        allow_upstream_build_paths=allow_upstream_build_paths,
                    )
    except GateError:
        raise
    except (OSError, EOFError, RuntimeError, UnicodeError, zipfile.BadZipFile) as error:
        raise GateError("PDF2MD_BUNDLE_E_ARCHIVE") from error


def _inspect_tar(
    stream: BinaryIO,
    *,
    depth: int,
    budget: ArchiveBudget,
    allow_upstream_build_paths: bool = False,
) -> None:
    seen: set[str] = set()
    try:
        stream.seek(0, os.SEEK_END)
        container_size = stream.tell()
        stream.seek(0)
        expanded_before = budget.expanded_bytes
        with tarfile.open(fileobj=stream, mode="r:*") as archive:
            for member in archive:
                budget.account_member()
                _safe_archive_name(member.name, seen)
                if member.isdir():
                    continue
                if not member.isfile() or getattr(member, "sparse", None):
                    raise GateError("PDF2MD_BUNDLE_E_ARCHIVE_UNSAFE")
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise GateError("PDF2MD_BUNDLE_E_ARCHIVE")
                with extracted:
                    _stream_member(
                        extracted,
                        declared_size=member.size,
                        name=member.name,
                        depth=depth,
                        budget=budget,
                        allow_upstream_build_paths=allow_upstream_build_paths,
                    )
        expanded = budget.expanded_bytes - expanded_before
        if expanded > 1024 * 1024 and expanded > max(1, container_size) * MAX_COMPRESSION_RATIO:
            raise GateError("PDF2MD_BUNDLE_E_ARCHIVE_LIMIT")
    except GateError:
        raise
    except (OSError, EOFError, UnicodeError, tarfile.TarError) as error:
        raise GateError("PDF2MD_BUNDLE_E_ARCHIVE") from error


def _inspect_ar(
    stream: BinaryIO,
    *,
    depth: int,
    budget: ArchiveBudget,
    allow_upstream_build_paths: bool = False,
) -> None:
    seen: set[str] = set()
    try:
        if stream.read(8) != b"!<arch>\n":
            raise GateError("PDF2MD_BUNDLE_E_ARCHIVE")
        index = 0
        while True:
            header = stream.read(60)
            if not header:
                return
            if len(header) != 60 or header[58:60] != b"`\n":
                raise GateError("PDF2MD_BUNDLE_E_ARCHIVE")
            try:
                size = int(header[48:58].decode("ascii").strip())
            except (UnicodeError, ValueError) as error:
                raise GateError("PDF2MD_BUNDLE_E_ARCHIVE") from error
            budget.account_member()
            raw_name = header[:16].decode("ascii", "strict").strip()
            name = raw_name.rstrip("/") or f"metadata-{index}"
            name_prefix = 0
            if raw_name.startswith("#1/"):
                name_prefix = int(raw_name[3:])
                encoded_name = stream.read(name_prefix)
                if len(encoded_name) != name_prefix:
                    raise GateError("PDF2MD_BUNDLE_E_ARCHIVE")
                name = encoded_name.decode("utf-8", "strict").rstrip("\0")
            if raw_name in ("/", "//") or raw_name.startswith("/"):
                name = f"metadata-{index}"
            _safe_archive_name(name, seen)
            content_size = size - name_prefix
            if content_size < 0:
                raise GateError("PDF2MD_BUNDLE_E_ARCHIVE")
            if content_size > MAX_ARCHIVE_MEMBER_BYTES:
                raise GateError("PDF2MD_BUNDLE_E_ARCHIVE_LIMIT")
            member = io.BytesIO(stream.read(content_size))
            if member.getbuffer().nbytes != content_size:
                raise GateError("PDF2MD_BUNDLE_E_ARCHIVE")
            _stream_member(
                member,
                declared_size=content_size,
                name=name,
                depth=depth,
                budget=budget,
                allow_upstream_build_paths=allow_upstream_build_paths,
            )
            if size % 2 and len(stream.read(1)) != 1:
                raise GateError("PDF2MD_BUNDLE_E_ARCHIVE")
            index += 1
    except GateError:
        raise
    except (OSError, UnicodeError, ValueError) as error:
        raise GateError("PDF2MD_BUNDLE_E_ARCHIVE") from error


def _inspect_single_compressed(
    stream: BinaryIO,
    *,
    kind: str,
    name: str,
    depth: int,
    budget: ArchiveBudget,
    allow_upstream_build_paths: bool = False,
) -> None:
    try:
        stream.seek(0, os.SEEK_END)
        compressed_size = stream.tell()
        stream.seek(0)
        budget.account_member()
        reader: IO[bytes]
        if kind == "gzip":
            reader = cast(IO[bytes], gzip.GzipFile(fileobj=stream, mode="rb"))
        elif kind == "bz2":
            reader = bz2.BZ2File(stream, mode="rb")
        else:
            reader = lzma.LZMAFile(stream, mode="rb")
        with reader as member:
            with tempfile.TemporaryFile() as copy:
                inspector = StreamInspector(allow_upstream_build_paths=allow_upstream_build_paths)
                size = 0
                while True:
                    chunk = member.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_ARCHIVE_MEMBER_BYTES:
                        raise GateError("PDF2MD_BUNDLE_E_ARCHIVE_LIMIT")
                    if (
                        size > 1024 * 1024
                        and size > max(1, compressed_size) * MAX_COMPRESSION_RATIO
                    ):
                        raise GateError("PDF2MD_BUNDLE_E_ARCHIVE_LIMIT")
                    inspector.feed(chunk)
                    copy.write(chunk)
                budget.account_bytes(size)
                copy.seek(0)
                head = copy.read(CONTAINER_PROBE_HEAD_BYTES)
                if size > len(head):
                    copy.seek(max(0, size - CONTAINER_PROBE_TAIL_BYTES))
                    head += copy.read(CONTAINER_PROBE_TAIL_BYTES)
                copy.seek(0)
                nested_name = name.rsplit(".", 1)[0]
                nested_kind = _archive_kind(nested_name, head)
                if nested_kind is not None:
                    _inspect_archive(
                        copy,
                        kind=nested_kind,
                        name=nested_name,
                        depth=depth + 1,
                        budget=budget,
                        allow_upstream_build_paths=allow_upstream_build_paths,
                    )
    except GateError:
        raise
    except (OSError, EOFError, TypeError, ValueError, lzma.LZMAError) as error:
        raise GateError("PDF2MD_BUNDLE_E_ARCHIVE") from error


def _inspect_archive(
    stream: BinaryIO,
    *,
    kind: str,
    name: str,
    depth: int,
    budget: ArchiveBudget,
    allow_upstream_build_paths: bool = False,
) -> None:
    if depth >= MAX_ARCHIVE_DEPTH:
        raise GateError("PDF2MD_BUNDLE_E_ARCHIVE_LIMIT")
    stream.seek(0)
    if kind == "zip":
        _inspect_zip(
            stream,
            depth=depth,
            budget=budget,
            allow_upstream_build_paths=allow_upstream_build_paths,
        )
    elif kind == "tar":
        _inspect_tar(
            stream,
            depth=depth,
            budget=budget,
            allow_upstream_build_paths=allow_upstream_build_paths,
        )
    elif kind == "ar":
        _inspect_ar(
            stream,
            depth=depth,
            budget=budget,
            allow_upstream_build_paths=allow_upstream_build_paths,
        )
    elif kind in ("gzip", "bz2", "xz"):
        _inspect_single_compressed(
            stream,
            kind=kind,
            name=name,
            depth=depth,
            budget=budget,
            allow_upstream_build_paths=allow_upstream_build_paths,
        )
    else:
        raise GateError("PDF2MD_BUNDLE_E_ARCHIVE_UNSUPPORTED")


def _inspect_regular_container(
    fd: int,
    name: str,
    value: os.stat_result,
    probe: bytes,
    aggregate_budget: AggregateArchiveBudget,
    *,
    allow_upstream_build_paths: bool = False,
) -> None:
    kind = _archive_kind(name, probe)
    if kind is None:
        return
    if value.st_size > MAX_ARCHIVE_TOTAL_BYTES:
        raise GateError("PDF2MD_BUNDLE_E_ARCHIVE_LIMIT")
    try:
        duplicate = os.dup(fd)
        os.lseek(duplicate, 0, os.SEEK_SET)
        with os.fdopen(duplicate, "rb", closefd=True) as stream:
            _inspect_archive(
                stream,
                kind=kind,
                name=name,
                depth=0,
                budget=ArchiveBudget(aggregate_budget),
                allow_upstream_build_paths=allow_upstream_build_paths,
            )
    except GateError:
        raise
    except OSError as error:
        raise GateError("PDF2MD_BUNDLE_E_ARCHIVE") from error


def _same_object(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev,
        left.st_ino,
        stat.S_IFMT(left.st_mode),
    ) == (
        right.st_dev,
        right.st_ino,
        stat.S_IFMT(right.st_mode),
    )


def _entry_from_stat(
    relative: str,
    kind: str,
    value: os.stat_result,
    *,
    digest: str = "",
    xattr_digest: str = "",
    symlink_target: str = "",
    macho_archs: Tuple[str, ...] = (),
    head: bytes = b"",
) -> Entry:
    return Entry(
        relative=relative,
        kind=kind,
        mode=value.st_mode,
        uid=value.st_uid,
        gid=value.st_gid,
        device=value.st_dev,
        inode=value.st_ino,
        links=value.st_nlink,
        size=value.st_size,
        mtime_ns=value.st_mtime_ns,
        ctime_ns=value.st_ctime_ns,
        digest=digest,
        xattr_digest=xattr_digest,
        symlink_target=symlink_target,
        macho_archs=macho_archs,
        head=head,
    )


class TreeScanner:
    def __init__(self, app: str, *, inspect_content: bool):
        self.app = os.path.realpath(app)
        self.contents = os.path.join(self.app, "Contents")
        self.inspect_content = inspect_content
        self.entries: Dict[str, Entry] = {}
        self.archive_budget = AggregateArchiveBudget()

    def scan(self) -> Snapshot:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            expected_app = os.lstat(self.app)
        except OSError as error:
            raise GateError("PDF2MD_BUNDLE_E_REQUIRED_PATH") from error
        if not stat.S_ISDIR(expected_app.st_mode) or stat.S_ISLNK(expected_app.st_mode):
            raise GateError("PDF2MD_BUNDLE_E_REQUIRED_PATH")
        app_fd = -1
        try:
            app_fd = os.open(self.app, flags)
            actual_app = os.fstat(app_fd)
        except OSError as error:
            if app_fd >= 0:
                os.close(app_fd)
            raise GateError("PDF2MD_BUNDLE_E_REQUIRED_PATH") from error
        if not _same_object(expected_app, actual_app):
            os.close(app_fd)
            raise GateError("PDF2MD_BUNDLE_E_TREE_DRIFT")
        try:
            app_xattr_digest, _ = _fd_xattrs(app_fd, inspect_content=self.inspect_content)
            try:
                app_names = os.listdir(app_fd)
            except OSError as error:
                raise GateError("PDF2MD_BUNDLE_E_SCAN") from error
            app_names.sort(key=lambda value: os.fsencode(value))
            if app_names != ["Contents"]:
                raise GateError("PDF2MD_BUNDLE_E_SCAN")
            try:
                root_fd = os.open("Contents", flags, dir_fd=app_fd)
                root_stat = os.fstat(root_fd)
            except OSError as error:
                raise GateError("PDF2MD_BUNDLE_E_REQUIRED_PATH") from error
            try:
                self._walk(root_fd, "", root_stat)
            finally:
                os.close(root_fd)
            try:
                final_app_names = os.listdir(app_fd)
                final_app = os.fstat(app_fd)
                final_app_xattr_digest, _ = _fd_xattrs(app_fd, inspect_content=False)
            except OSError as error:
                raise GateError("PDF2MD_BUNDLE_E_SCAN") from error
            final_app_names.sort(key=lambda value: os.fsencode(value))
            if (
                app_names != final_app_names
                or not _same_object(actual_app, final_app)
                or (actual_app.st_mtime_ns, actual_app.st_ctime_ns)
                != (final_app.st_mtime_ns, final_app.st_ctime_ns)
                or app_xattr_digest != final_app_xattr_digest
            ):
                raise GateError("PDF2MD_BUNDLE_E_TREE_DRIFT")
        finally:
            os.close(app_fd)
        digest = hashlib.sha256()
        digest.update(
            _entry_from_stat(
                "@app-root",
                "directory",
                actual_app,
                xattr_digest=app_xattr_digest,
            ).fingerprint_bytes()
        )
        digest.update(b"\n")
        for relative in sorted(self.entries):
            digest.update(self.entries[relative].fingerprint_bytes())
            digest.update(b"\n")
        return Snapshot(self.app, self.contents, self.entries, digest.hexdigest())

    def _walk(self, directory_fd: int, relative: str, initial: os.stat_result) -> None:
        if len(self.entries) >= MAX_TREE_ENTRIES:
            raise GateError("PDF2MD_BUNDLE_E_SCAN")
        xattr_digest, _ = _fd_xattrs(directory_fd, inspect_content=self.inspect_content)
        self.entries[relative] = _entry_from_stat(
            relative, "directory", initial, xattr_digest=xattr_digest
        )
        try:
            names = os.listdir(directory_fd)
        except OSError as error:
            raise GateError("PDF2MD_BUNDLE_E_SCAN") from error
        names.sort(key=lambda value: os.fsencode(value))
        for name in names:
            try:
                name.encode("utf-8", "strict")
            except UnicodeError as error:
                raise GateError("PDF2MD_BUNDLE_E_SCAN") from error
            if _contains_control(name):
                raise GateError("PDF2MD_BUNDLE_E_ENTRY_CONTROL_CHAR")
            child_relative = os.path.join(relative, name) if relative else name
            _inspect_patterns(child_relative.encode("utf-8"))
            try:
                child_stat = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except OSError as error:
                raise GateError("PDF2MD_BUNDLE_E_SCAN") from error
            _archive_kind(child_relative, b"")
            if stat.S_ISLNK(child_stat.st_mode):
                self._scan_symlink(directory_fd, name, child_relative, child_stat)
            elif stat.S_ISDIR(child_stat.st_mode):
                self._scan_directory(directory_fd, name, child_relative, child_stat)
            elif stat.S_ISREG(child_stat.st_mode):
                self._scan_regular(directory_fd, name, child_relative, child_stat)
            else:
                raise GateError("PDF2MD_BUNDLE_E_SCAN")
            if len(self.entries) > MAX_TREE_ENTRIES:
                raise GateError("PDF2MD_BUNDLE_E_SCAN")
        try:
            final_names = os.listdir(directory_fd)
            final_stat = os.fstat(directory_fd)
        except OSError as error:
            raise GateError("PDF2MD_BUNDLE_E_SCAN") from error
        final_names.sort(key=lambda value: os.fsencode(value))
        if names != final_names or not _same_object(initial, final_stat):
            raise GateError("PDF2MD_BUNDLE_E_TREE_DRIFT")
        if (initial.st_mtime_ns, initial.st_ctime_ns) != (
            final_stat.st_mtime_ns,
            final_stat.st_ctime_ns,
        ):
            raise GateError("PDF2MD_BUNDLE_E_TREE_DRIFT")

    def _scan_directory(
        self,
        parent_fd: int,
        name: str,
        relative: str,
        expected: os.stat_result,
    ) -> None:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            child_fd = os.open(name, flags, dir_fd=parent_fd)
            actual = os.fstat(child_fd)
        except OSError as error:
            raise GateError("PDF2MD_BUNDLE_E_SCAN") from error
        try:
            if not _same_object(expected, actual):
                raise GateError("PDF2MD_BUNDLE_E_TREE_DRIFT")
            self._walk(child_fd, relative, actual)
        finally:
            os.close(child_fd)

    def _scan_symlink(
        self,
        parent_fd: int,
        name: str,
        relative: str,
        expected: os.stat_result,
    ) -> None:
        try:
            target = os.readlink(name, dir_fd=parent_fd)
            actual = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as error:
            raise GateError("PDF2MD_BUNDLE_E_SCAN") from error
        if not _same_object(expected, actual):
            raise GateError("PDF2MD_BUNDLE_E_TREE_DRIFT")
        if _contains_control(target):
            raise GateError("PDF2MD_BUNDLE_E_ENTRY_CONTROL_CHAR")
        encoded_target = target.encode("utf-8", "strict")
        if PREFIX_SECRET_RE.search(encoded_target):
            raise GateError("PDF2MD_BUNDLE_E_CREDENTIAL")
        if os.path.isabs(target):
            raise GateError("PDF2MD_BUNDLE_E_SYMLINK_ABSOLUTE")
        _inspect_patterns(encoded_target)
        absolute = os.path.join(self.contents, relative)
        if not os.path.exists(absolute):
            raise GateError("PDF2MD_BUNDLE_E_SYMLINK_BROKEN")
        resolved = os.path.realpath(absolute)
        try:
            inside = os.path.commonpath((self.app, resolved)) == self.app
        except ValueError:
            inside = False
        if not inside:
            raise GateError("PDF2MD_BUNDLE_E_SYMLINK_OUTSIDE")
        xattr_digest = _symlink_xattrs(
            absolute,
            inspect_content=self.inspect_content,
        )
        try:
            final = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as error:
            raise GateError("PDF2MD_BUNDLE_E_SCAN") from error
        if not _same_object(actual, final) or (
            actual.st_mtime_ns,
            actual.st_ctime_ns,
        ) != (final.st_mtime_ns, final.st_ctime_ns):
            raise GateError("PDF2MD_BUNDLE_E_TREE_DRIFT")
        self.entries[relative] = _entry_from_stat(
            relative,
            "symlink",
            final,
            xattr_digest=xattr_digest,
            symlink_target=target,
        )

    def _scan_regular(
        self,
        parent_fd: int,
        name: str,
        relative: str,
        expected: os.stat_result,
    ) -> None:
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd)
            actual = os.fstat(fd)
        except OSError as error:
            raise GateError("PDF2MD_BUNDLE_E_SCAN") from error
        try:
            if not _same_object(expected, actual) or not stat.S_ISREG(actual.st_mode):
                raise GateError("PDF2MD_BUNDLE_E_TREE_DRIFT")
            if actual.st_nlink != 1:
                raise GateError("PDF2MD_BUNDLE_E_SCAN")
            digest = hashlib.sha256()
            inspector = StreamInspector(
                allow_upstream_build_paths=_allows_upstream_build_paths(relative)
            )
            container_head = bytearray()
            container_tail = bytearray()
            while True:
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                if len(container_head) < CONTAINER_PROBE_HEAD_BYTES:
                    container_head.extend(chunk[: CONTAINER_PROBE_HEAD_BYTES - len(container_head)])
                container_tail.extend(chunk)
                if len(container_tail) > CONTAINER_PROBE_TAIL_BYTES:
                    del container_tail[:-CONTAINER_PROBE_TAIL_BYTES]
                if self.inspect_content:
                    inspector.feed(chunk)
            final = os.fstat(fd)
            if not _same_object(actual, final) or (
                actual.st_size,
                actual.st_mtime_ns,
                actual.st_ctime_ns,
            ) != (final.st_size, final.st_mtime_ns, final.st_ctime_ns):
                raise GateError("PDF2MD_BUNDLE_E_TREE_DRIFT")
            xattr_digest, _ = _fd_xattrs(fd, inspect_content=self.inspect_content)
            entry_head = bytes(container_head[:4096])
            container_probe = bytes(container_head)
            if actual.st_size > len(container_head):
                container_probe += bytes(container_tail)
            architectures = _macho_architectures(entry_head)
            if self.inspect_content:
                self._inspect_regular_policy(fd, relative, actual, container_probe)
            post_policy = os.fstat(fd)
            if not _same_object(actual, post_policy) or (
                actual.st_size,
                actual.st_mtime_ns,
                actual.st_ctime_ns,
            ) != (
                post_policy.st_size,
                post_policy.st_mtime_ns,
                post_policy.st_ctime_ns,
            ):
                raise GateError("PDF2MD_BUNDLE_E_TREE_DRIFT")
            self.entries[relative] = _entry_from_stat(
                relative,
                "file",
                post_policy,
                digest=digest.hexdigest(),
                xattr_digest=xattr_digest,
                macho_archs=architectures,
                head=entry_head,
            )
        finally:
            os.close(fd)

    def _inspect_regular_policy(
        self, fd: int, relative: str, value: os.stat_result, head: bytes
    ) -> None:
        if relative.startswith("Resources/") and relative.casefold().endswith((".pyc", ".pyo")):
            raise GateError("PDF2MD_BUNDLE_E_BYTECODE")
        runtime_prefix = "Resources/python-runtime/"
        if relative.startswith(runtime_prefix) and value.st_mode & 0o111 and head.startswith(b"#!"):
            first_line = head.splitlines()[0]
            if first_line not in (b"#!/bin/bash", b"#!/bin/sh"):
                raise GateError("PDF2MD_BUNDLE_E_SHEBANG")
        _inspect_regular_container(
            fd,
            relative,
            value,
            head,
            self.archive_budget,
            allow_upstream_build_paths=_allows_upstream_build_paths(relative),
        )


def _inspect_dmg_regular(parent_fd: int, name: str, archive_budget: AggregateArchiveBudget) -> None:
    try:
        expected = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd)
    except OSError as error:
        raise GateError("PDF2MD_BUNDLE_E_DMG_LAYOUT") from error
    try:
        actual = os.fstat(fd)
        if (
            not _same_object(expected, actual)
            or not stat.S_ISREG(actual.st_mode)
            or actual.st_nlink != 1
            or actual.st_mode & 0o111
            or actual.st_size > MAX_DMG_PRESENTATION_BYTES
        ):
            raise GateError("PDF2MD_BUNDLE_E_DMG_LAYOUT")
        inspector = StreamInspector()
        container_head = bytearray()
        container_tail = bytearray()
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            if len(container_head) < CONTAINER_PROBE_HEAD_BYTES:
                container_head.extend(chunk[: CONTAINER_PROBE_HEAD_BYTES - len(container_head)])
            container_tail.extend(chunk)
            if len(container_tail) > CONTAINER_PROBE_TAIL_BYTES:
                del container_tail[:-CONTAINER_PROBE_TAIL_BYTES]
            inspector.feed(chunk)
        _fd_xattrs(fd, inspect_content=True)
        container_probe = bytes(container_head)
        if actual.st_size > len(container_head):
            container_probe += bytes(container_tail)
        _inspect_regular_container(fd, name, actual, container_probe, archive_budget)
        final = os.fstat(fd)
        if not _same_object(actual, final) or (
            actual.st_size,
            actual.st_mtime_ns,
            actual.st_ctime_ns,
        ) != (final.st_size, final.st_mtime_ns, final.st_ctime_ns):
            raise GateError("PDF2MD_BUNDLE_E_TREE_DRIFT")
    finally:
        os.close(fd)


def _inspect_dmg_background(root_fd: int, archive_budget: AggregateArchiveBudget) -> None:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        expected = os.stat(".background", dir_fd=root_fd, follow_symlinks=False)
        background_fd = os.open(".background", flags, dir_fd=root_fd)
    except OSError as error:
        raise GateError("PDF2MD_BUNDLE_E_DMG_LAYOUT") from error
    try:
        actual = os.fstat(background_fd)
        if not _same_object(expected, actual) or not stat.S_ISDIR(actual.st_mode):
            raise GateError("PDF2MD_BUNDLE_E_DMG_LAYOUT")
        xattrs, _ = _fd_xattrs(background_fd, inspect_content=True)
        names = os.listdir(background_fd)
        names.sort(key=os.fsencode)
        if names != ["background.png"]:
            raise GateError("PDF2MD_BUNDLE_E_DMG_LAYOUT")
        _inspect_dmg_regular(background_fd, "background.png", archive_budget)
        final_names = os.listdir(background_fd)
        final_names.sort(key=os.fsencode)
        final = os.fstat(background_fd)
        final_xattrs, _ = _fd_xattrs(background_fd, inspect_content=False)
        if (
            names != final_names
            or not _same_object(actual, final)
            or (actual.st_mtime_ns, actual.st_ctime_ns) != (final.st_mtime_ns, final.st_ctime_ns)
            or xattrs != final_xattrs
        ):
            raise GateError("PDF2MD_BUNDLE_E_TREE_DRIFT")
    except GateError:
        raise
    except OSError as error:
        raise GateError("PDF2MD_BUNDLE_E_DMG_LAYOUT") from error
    finally:
        os.close(background_fd)


def verify_dmg_volume(volume: str) -> str:
    if _contains_control(volume):
        raise GateError("PDF2MD_BUNDLE_E_ENTRY_CONTROL_CHAR")
    root = os.path.abspath(volume)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        expected_root = os.lstat(root)
        root_fd = os.open(root, flags)
    except OSError as error:
        raise GateError("PDF2MD_BUNDLE_E_DMG_LAYOUT") from error
    try:
        actual_root = os.fstat(root_fd)
        if not _same_object(expected_root, actual_root) or not stat.S_ISDIR(actual_root.st_mode):
            raise GateError("PDF2MD_BUNDLE_E_DMG_LAYOUT")
        root_xattrs, _ = _fd_xattrs(root_fd, inspect_content=True)
        names = os.listdir(root_fd)
        names.sort(key=os.fsencode)
        required_names = [
            ".VolumeIcon.icns",
            "Applications",
            "PDF2MD.app",
        ]
        allowed_names = set(required_names) | {".background", ".DS_Store"}
        if not set(required_names) <= set(names) or not set(names) <= allowed_names:
            raise GateError("PDF2MD_BUNDLE_E_DMG_LAYOUT")

        app_expected = os.stat("PDF2MD.app", dir_fd=root_fd, follow_symlinks=False)
        if not stat.S_ISDIR(app_expected.st_mode) or stat.S_ISLNK(app_expected.st_mode):
            raise GateError("PDF2MD_BUNDLE_E_DMG_LAYOUT")
        app_fd = os.open("PDF2MD.app", flags, dir_fd=root_fd)
        try:
            app_actual = os.fstat(app_fd)
            if not _same_object(app_expected, app_actual):
                raise GateError("PDF2MD_BUNDLE_E_TREE_DRIFT")
            _fd_xattrs(app_fd, inspect_content=True)
            app_final = os.fstat(app_fd)
            if not _same_object(app_actual, app_final) or (
                app_actual.st_mtime_ns,
                app_actual.st_ctime_ns,
            ) != (app_final.st_mtime_ns, app_final.st_ctime_ns):
                raise GateError("PDF2MD_BUNDLE_E_TREE_DRIFT")
        finally:
            os.close(app_fd)

        applications_expected = os.stat("Applications", dir_fd=root_fd, follow_symlinks=False)
        if not stat.S_ISLNK(applications_expected.st_mode):
            raise GateError("PDF2MD_BUNDLE_E_DMG_TARGET")
        applications_target = os.readlink("Applications", dir_fd=root_fd)
        if applications_target != "/Applications":
            raise GateError("PDF2MD_BUNDLE_E_DMG_TARGET")
        _symlink_xattrs(os.path.join(root, "Applications"), inspect_content=True)
        applications_final = os.stat("Applications", dir_fd=root_fd, follow_symlinks=False)
        if (
            not _same_object(applications_expected, applications_final)
            or os.readlink("Applications", dir_fd=root_fd) != applications_target
        ):
            raise GateError("PDF2MD_BUNDLE_E_TREE_DRIFT")

        archive_budget = AggregateArchiveBudget()
        if ".background" in names:
            _inspect_dmg_background(root_fd, archive_budget)
        _inspect_dmg_regular(root_fd, ".VolumeIcon.icns", archive_budget)
        if ".DS_Store" in names:
            _inspect_dmg_regular(root_fd, ".DS_Store", archive_budget)

        final_names = os.listdir(root_fd)
        final_names.sort(key=os.fsencode)
        final_root = os.fstat(root_fd)
        final_root_xattrs, _ = _fd_xattrs(root_fd, inspect_content=False)
        if (
            names != final_names
            or not _same_object(actual_root, final_root)
            or (actual_root.st_mtime_ns, actual_root.st_ctime_ns)
            != (final_root.st_mtime_ns, final_root.st_ctime_ns)
            or root_xattrs != final_root_xattrs
        ):
            raise GateError("PDF2MD_BUNDLE_E_TREE_DRIFT")
    except GateError:
        raise
    except (OSError, UnicodeError, ValueError) as error:
        raise GateError("PDF2MD_BUNDLE_E_DMG_LAYOUT") from error
    finally:
        os.close(root_fd)
    return "PDF2MD_DMG_VOLUME_POLICY_TAURI_V1"


def _validate_tool(path: str) -> None:
    current = path
    while True:
        try:
            value = os.lstat(current)
        except OSError as error:
            raise GateError("PDF2MD_BUNDLE_E_INSPECTION") from error
        if stat.S_ISLNK(value.st_mode) or value.st_uid != 0 or value.st_mode & 0o022:
            raise GateError("PDF2MD_BUNDLE_E_INSPECTION")
        if current == path:
            if not stat.S_ISREG(value.st_mode) or not value.st_mode & 0o111:
                raise GateError("PDF2MD_BUNDLE_E_INSPECTION")
        elif not stat.S_ISDIR(value.st_mode):
            raise GateError("PDF2MD_BUNDLE_E_INSPECTION")
        parent = os.path.dirname(current)
        if parent == current:
            return
        current = parent


def _run_tool(
    tool: str,
    arguments: Sequence[str],
    *,
    error_code: str,
    allow_failure: bool = False,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            [tool, *arguments],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            env=CLEAN_ENV,
            cwd="/",
            close_fds=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError) as error:
        raise GateError(error_code) from error
    if result.returncode != 0 and not allow_failure:
        raise GateError(error_code)
    return result


def _parse_dependencies(report: str) -> List[str]:
    lines = report.splitlines()
    if not lines:
        raise GateError("PDF2MD_BUNDLE_E_MACHO_PARSE")
    dependencies = []
    for line in lines[1:]:
        match = re.match(r"^\s*(.+?)\s+\([^)]*version[^)]*\)\s*$", line)
        if not match:
            if line.strip():
                raise GateError("PDF2MD_BUNDLE_E_MACHO_PARSE")
            continue
        dependencies.append(match.group(1))
    return dependencies


def _parse_rpaths(report: str) -> List[str]:
    lines = report.splitlines()
    values = []
    for index, line in enumerate(lines):
        if line.strip() != "cmd LC_RPATH":
            continue
        value = None
        for following in lines[index + 1 :]:
            stripped = following.strip()
            if stripped.startswith("Load command "):
                break
            match = re.match(r"^path (.*?) \(offset \d+\)$", stripped)
            if match:
                value = match.group(1)
                break
        if not value:
            raise GateError("PDF2MD_BUNDLE_E_MACHO_PARSE")
        values.append(value)
    return values


def _is_system_library(path: str) -> bool:
    normalized = os.path.normpath(path)
    return (
        normalized == "/usr/lib"
        or normalized.startswith("/usr/lib/")
        or (normalized == "/System/Library" or normalized.startswith("/System/Library/"))
    )


def _inside(root: str, path: str) -> bool:
    try:
        return os.path.commonpath((root, path)) == root
    except ValueError:
        return False


class MachOVerifier:
    def __init__(self, snapshot: Snapshot):
        self.snapshot = snapshot
        self.executable_base = os.path.join(snapshot.app, "Contents", "MacOS")

    def verify_all(self) -> None:
        macho_entries = [entry for entry in self.snapshot.entries.values() if entry.macho_archs]
        for entry in macho_entries:
            self._verify_architecture(entry)
        for entry in macho_entries:
            self._verify_dependencies(entry)

    def _verify_architecture(self, entry: Entry) -> None:
        if entry.macho_archs != ("arm64",):
            raise GateError("PDF2MD_BUNDLE_E_MACHO_ARCH")
        result = _run_tool(
            TOOLS["lipo"],
            ["-archs", self.snapshot.absolute(entry)],
            error_code="PDF2MD_BUNDLE_E_MACHO_INSPECTION",
        )
        architectures = tuple(sorted(set(result.stdout.split())))
        if architectures != ("arm64",):
            raise GateError("PDF2MD_BUNDLE_E_MACHO_ARCH")

    def _verify_dependencies(self, entry: Entry) -> None:
        binary = self.snapshot.absolute(entry)
        dependencies = _parse_dependencies(
            _run_tool(
                TOOLS["otool"],
                ["-L", binary],
                error_code="PDF2MD_BUNDLE_E_MACHO_INSPECTION",
            ).stdout
        )
        install_name_report = _run_tool(
            TOOLS["otool"],
            ["-D", binary],
            error_code="PDF2MD_BUNDLE_E_MACHO_INSPECTION",
            allow_failure=True,
        )
        if install_name_report.returncode == 0:
            own_names = [
                line.strip() for line in install_name_report.stdout.splitlines()[1:] if line.strip()
            ]
            for own_name in own_names:
                if own_name in dependencies:
                    dependencies.remove(own_name)
        rpaths = _parse_rpaths(
            _run_tool(
                TOOLS["otool"],
                ["-l", binary],
                error_code="PDF2MD_BUNDLE_E_MACHO_INSPECTION",
            ).stdout
        )
        resolved_rpaths = [self._resolve_rpath(value, binary) for value in rpaths]
        for dependency in dependencies:
            self._resolve_dependency(dependency, binary, resolved_rpaths)

    def _expand_tokens(self, value: str, binary: str) -> tuple[str, ...]:
        if _contains_control(value):
            return ()
        candidates: list[str] = []
        for token, bases in (
            ("@loader_path", (os.path.dirname(binary),)),
            ("@executable_path", (self.executable_base, os.path.dirname(binary))),
        ):
            if value == token:
                suffix = ""
            elif value.startswith(token + "/"):
                suffix = value[len(token) + 1 :]
            else:
                continue
            for base in bases:
                candidate = os.path.normpath(os.path.join(base, suffix))
                if candidate not in candidates:
                    candidates.append(candidate)
            break
        return tuple(candidates)

    def _resolve_rpath(self, value: str, binary: str) -> str:
        if value.startswith("/"):
            normalized = os.path.normpath(value)
            if _is_system_library(normalized):
                return normalized
            raise GateError("PDF2MD_BUNDLE_E_MACHO_RPATH")
        for candidate in self._expand_tokens(value, binary):
            if not _inside(self.snapshot.app, candidate):
                continue
            entry = self.snapshot.resolved_entry(candidate)
            if entry is not None and entry.kind == "directory":
                return os.path.realpath(candidate)
        raise GateError("PDF2MD_BUNDLE_E_MACHO_RPATH")

    def _resolve_dependency(
        self, dependency: str, binary: str, resolved_rpaths: Sequence[str]
    ) -> None:
        if _contains_control(dependency):
            raise GateError("PDF2MD_BUNDLE_E_MACHO_DEPENDENCY")
        candidates: Iterable[str]
        if dependency == "@rpath":
            raise GateError("PDF2MD_BUNDLE_E_MACHO_DEPENDENCY")
        if dependency.startswith("@rpath/"):
            suffix = dependency[len("@rpath/") :]
            if not suffix or not resolved_rpaths:
                raise GateError("PDF2MD_BUNDLE_E_MACHO_DEPENDENCY")
            candidates = [os.path.normpath(os.path.join(base, suffix)) for base in resolved_rpaths]
        elif dependency.startswith("/"):
            normalized = os.path.normpath(dependency)
            if _is_system_library(normalized):
                return
            candidates = [normalized]
        else:
            candidates = self._expand_tokens(dependency, binary)
            if not candidates:
                raise GateError("PDF2MD_BUNDLE_E_MACHO_DEPENDENCY")
        for candidate in candidates:
            if _is_system_library(candidate):
                return
            if not _inside(self.snapshot.app, candidate):
                continue
            entry = self.snapshot.resolved_entry(candidate)
            if entry is not None and entry.kind == "file" and entry.macho_archs == ("arm64",):
                return
        raise GateError("PDF2MD_BUNDLE_E_MACHO_DEPENDENCY")


def _validate_required_layout(snapshot: Snapshot) -> None:
    launcher = snapshot.required("MacOS/python3", "file")
    runtime = snapshot.required("Resources/python-runtime/bin/python3.12", "file")
    if not launcher.mode & 0o111:
        raise GateError("PDF2MD_BUNDLE_E_LAUNCHER")
    if not runtime.mode & 0o111:
        raise GateError("PDF2MD_BUNDLE_E_RUNTIME")
    if runtime.links != 1:
        raise GateError("PDF2MD_BUNDLE_E_RUNTIME")
    for relative in (
        "Resources/python-runtime/bin/python",
        "Resources/python-runtime/bin/python3",
    ):
        interpreter = snapshot.entries.get(relative)
        if interpreter is None:
            raise GateError("PDF2MD_BUNDLE_E_RUNTIME_LINK")
        if interpreter.kind == "symlink":
            if interpreter.symlink_target != "python3.12":
                raise GateError("PDF2MD_BUNDLE_E_RUNTIME_LINK")
            continue
        if (
            interpreter.kind != "file"
            or not interpreter.mode & 0o111
            or interpreter.macho_archs != ("arm64",)
        ):
            raise GateError("PDF2MD_BUNDLE_E_RUNTIME_LINK")
    if runtime.macho_archs != ("arm64",):
        raise GateError("PDF2MD_BUNDLE_E_RUNTIME")


def _verify_signature_details(target: str) -> None:
    details = _run_tool(
        TOOLS["codesign"],
        ["-d", "--verbose=4", target],
        error_code="PDF2MD_BUNDLE_E_SIGNATURE_MODE",
    )
    report = details.stdout + "\n" + details.stderr
    lines = {line.strip() for line in report.splitlines()}
    if "Signature=adhoc" not in lines or "TeamIdentifier=not set" not in lines:
        raise GateError("PDF2MD_BUNDLE_E_SIGNATURE_MODE")
    if not any("flags=" in line and "runtime" in line for line in lines):
        raise GateError("PDF2MD_BUNDLE_E_HARDENED_RUNTIME")


def _verify_signature_policy(snapshot: Snapshot) -> None:
    _run_tool(
        TOOLS["codesign"],
        ["--verify", "--deep", "--strict", "--verbose=2", snapshot.app],
        error_code="PDF2MD_BUNDLE_E_CODESIGN",
    )
    _verify_signature_details(snapshot.app)
    macho_entries = sorted(
        (entry for entry in snapshot.entries.values() if entry.macho_archs),
        key=lambda entry: entry.relative,
    )
    for entry in macho_entries:
        target = snapshot.absolute(entry)
        _run_tool(
            TOOLS["codesign"],
            ["--verify", "--strict", "--verbose=2", target],
            error_code="PDF2MD_BUNDLE_E_CODESIGN",
        )
        _verify_signature_details(target)


def verify_bundle(app: str) -> str:
    if _contains_control(app):
        raise GateError("PDF2MD_BUNDLE_E_ENTRY_CONTROL_CHAR")
    try:
        value = os.lstat(app)
    except OSError as error:
        raise GateError("PDF2MD_BUNDLE_E_REQUIRED_PATH") from error
    if not stat.S_ISDIR(value.st_mode) or stat.S_ISLNK(value.st_mode):
        raise GateError("PDF2MD_BUNDLE_E_REQUIRED_PATH")
    for path in TOOLS.values():
        _validate_tool(path)
    before = TreeScanner(app, inspect_content=True).scan()
    _validate_required_layout(before)
    MachOVerifier(before).verify_all()
    _verify_signature_policy(before)
    after = TreeScanner(app, inspect_content=False).scan()
    if before.fingerprint != after.fingerprint:
        raise GateError("PDF2MD_BUNDLE_E_TREE_DRIFT")
    return POLICY


def main(arguments: Sequence[str]) -> int:
    if len(arguments) == 2 and arguments[0] == "--dmg-volume":
        operation = verify_dmg_volume
        target = arguments[1]
    elif len(arguments) == 1:
        operation = verify_bundle
        target = arguments[0]
    else:
        print("PDF2MD_BUNDLE_E_USAGE", file=sys.stderr)
        return 1
    try:
        policy = operation(target)
    except GateError as error:
        print(error.code, file=sys.stderr)
        return 1
    except Exception:
        print("PDF2MD_BUNDLE_E_INSPECTION", file=sys.stderr)
        return 1
    print(policy)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
