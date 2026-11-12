"""Reproducible patcher for the pinned Antigravity CLI 1.4.2 Linux binary.

Usage:
    python -m agy_proxy_core.bridge.patcher --source /path/to/agy --dest /path/to/agy.bridge

The source must be the exact 1.4.2 build below.  The patcher keeps the real
CLI login/bootstrap path and the original updater; auto-update must be disabled
with AGY_CLI_DISABLE_AUTO_UPDATE=true at runtime.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import struct
import tempfile

try:  # package import (`python -m app.bridge.patcher`)
    from . import PATCHED_SHA256, SOURCE_SHA256, emitter as p
except ImportError:  # direct script import without executing app/__init__.py
    import emitter as p  # type: ignore[no-redef]

    PATCHED_SHA256 = "636570d016f462b68382781917146ec8756dce416895bdfecfdd151f0a060649"
    SOURCE_SHA256 = "085f43e0d0628a8a8dabe1e547067818fa2c0adc86cfac98a3c4492918f455ef"

RW = 0x10108100
RX = 0x10110000
RX_FILE = 0xD30B000
RW_FILE = 0xD309100
DONOR = 0x90EF9A0
DONOR_REC = 0x4127BC0
DONOR_STUB = 0x90F0917
EMPTY_ARGS = DONOR + 0xE00
BOOT = DONOR + 0x280
FUNCVAL = RW + 0x600
MUX1 = RW + 0x608
MUX2 = RW + 0x610


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _configure_emitter() -> None:
    """Point the tested Day 3C emitter at the 1.4.2 addresses."""
    for name in [
        "PCHEADER", "FUNCNAMES", "PCTAB", "PCLNTABLE", "FTAB", "FINDFUNCTAB",
        "GOFUNC_NEW", "NEW_MD", "SERVER_GLOBAL", "BRIDGE_STATE", "INIT_COUNT",
        "SECOND_RUN", "MSG_ITAB_NEW", "TEST_CONFLICT", "RESP_ITAB_NEW",
        "STREAM_ITAB_NEW", "MOCK_RESP", "MOCK_STATE", "TEST_MODE", "SEQ_COUNTER",
    ]:
        setattr(p, name, RW + getattr(p, name) - p.RW_VA)
    for name, value in {
        "CODE_VA": RX,
        "CODE_LIMIT": RX + 0x2000,
        "FIRST_MD": 0xBA1D1E0,
        "MALLOCGC": 0x6263480,
        "RUNTIME_GC": 0x62068C0,
        "MORESTACK": 0x626C480,
        "HANDLEFUNC": 0x66A95E0,
        "NEWOBJECT": 0x61FA520,
        "PROTO_REFLECT": 0x6F66340,
        "PROTOJSON_UNMARSHAL_OPTS": 0x6774640,
        "CONTEXT_BACKGROUND": 0x635CA00,
        "REQUEST_CONTEXT": 0x6695120,
        "GETCHAT": 0x7E96D80,
        "MIME_SET": 0x6627AC0,
        "PROTO_MARSHAL_OPTS": 0x673B240,
        "PROTOJSON_MARSHAL": 0x6779660,
        "PROTOJSON_UNMARSHAL": 0x67745A0,
        "BASE64_ENC": 0x63843E0,
        "STDENC_GLOBAL": 0xB7B9958,
        "STRCONV_ITOA": 0x6298EC0,
        "RESP_TYPE": 0xB0C2FF0,
        "PTR_RESP_TYPE": 0xA066FB8,
        "RESP_PROTO_REFLECT": 0x6F66780,
        "REQ_TYPE": 0xB0C2D68,
        "PTR_REQ_TYPE": 0xA066DF0,
    }.items():
        setattr(p, name, value)


def _patch_handler_emitter() -> None:
    """Apply the tested 1.4.2 morestack fix and use the real proto itabs."""
    original_call = p.Program.call

    def preserve_morestack_args(program, target):
        morestack_stub = target == p.MORESTACK and program.labels.get("stub") == len(program.b)
        if morestack_stub:
            program.emit(bytes.fromhex("488944240848895c241048894c2418"))
        original_call(program, target)
        if morestack_stub:
            program.emit(bytes.fromhex("488b442408488b5c2410488b4c2418"))

    p.Program.call = preserve_morestack_args


def apply(source: bytes, *, expected_sha: str | None = PATCHED_SHA256) -> tuple[bytes, dict]:
    """Return (patched_bytes, plan).  Raises ValueError on unsupported input."""
    if _sha256(source) != SOURCE_SHA256:
        raise ValueError("source binary is not the pinned 1.4.2 build")
    _configure_emitter()
    _patch_handler_emitter()

    handler, stub, labels = p.build_handler()
    handler = bytearray(handler)

    # Keep byte offsets and PC tables unchanged; replace the old hand-built
    # proto.Message itabs with the real runtime itabs from this ELF.
    canonical = {p.MSG_ITAB_NEW: 0xB144CF0, p.RESP_ITAB_NEW: 0xB159770}
    replaced = 0
    for i in range(len(handler) - 65):
        if handler[i:i + 3] != bytes.fromhex("488d05"):
            continue
        target = RX + i + 7 + struct.unpack_from("<i", handler, i + 3)[0]
        if target not in canonical:
            continue
        assert handler[i + 7:i + 11] == bytes.fromhex("48898424")
        assert handler[i + 15:i + 22] == bytes.fromhex("48c70000000000")
        assert handler[i + 58:i + 62] != bytes.fromhex("48895018")
        struct.pack_into("<i", handler, i + 3, canonical[target] - (RX + i + 7))
        assert handler[i + 54:i + 58] == bytes.fromhex("48895018")
        handler[i + 15:i + 58] = b"\x90" * 43
        replaced += 1
    assert replaced == 3, replaced
    assert len(handler) <= 0x2000

    pcsp = p.pctab_table([(0, p.PROLOGUE_LEN), (p.PCSP, stub - p.PROLOGUE_LEN),
                          (0, 0x2000 - stub)])
    unsafe = p.pctab_table([(-2, 0x2000)])
    stack = p.pctab_table([(0, p.PROLOGUE_LEN), (1, stub - p.PROLOGUE_LEN),
                           (0, 0x2000 - stub)])
    pctab = b"\0" + pcsp + unsafe + stack
    args_map = struct.pack("<ii", 2, 3) + b"\x07\0"
    bits = bytearray((p.NBIT + 7) // 8)
    for i in p.POINTER_BITS:
        bits[i // 8] |= 1 << (i % 8)
    gofunc = args_map + struct.pack("<ii", 2, p.NBIT) + bytes(len(bits)) + bits
    func = bytearray(64)
    for off, value in [(0, 0), (4, 1), (8, 24), (16, 1), (28, 2),
                       (44, 1 + len(pcsp)), (48, 1 + len(pcsp) + len(unsafe)),
                       (52, 0), (56, len(args_map)), (60, 0xffffffff)]:
        struct.pack_into("<I", func, off, value)
    func[43] = 3

    hdr = bytearray(source[0x1512750:0x1512798])
    for off, value in [(8, 1), (16, 0), (24, 0), (32, p.FUNCNAMES - p.PCHEADER),
                       (40, 0), (48, 0), (56, p.PCTAB - p.PCHEADER),
                       (64, p.PCLNTABLE - p.PCHEADER)]:
        struct.pack_into("<Q", hdr, off, value)

    b = p.Program(BOOT)
    # setup.rsp == donor.rsp+0x560; no setup arguments are moved.
    p.mov_reg_rsp(b, 0, 0x950)  # setup saved *Server at +0x3F0
    b.lea_abs(2, p.SERVER_GLOBAL)
    b.emit(bytes.fromhex("488902"))
    b.lea_abs(0, p.FIRST_MD)
    b.emit(bytes.fromhex("488b9030020000"))
    b.emit(bytes.fromhex("4885d2"))
    b.jcc32(bytes.fromhex("0f84"), "init")
    b.lea_abs(1, p.NEW_MD)
    b.emit(bytes.fromhex("4839ca"))
    b.jcc32(bytes.fromhex("0f84"), "already")
    b.lea_abs(2, p.BRIDGE_STATE)
    b.emit(bytes.fromhex("48c70202000000"))
    b.jmp("done")
    b.label("init")
    b.lea_abs(3, p.NEW_MD)
    for off, va in [(0, p.PCHEADER), (8, p.FUNCNAMES), (0x50, p.PCTAB),
                    (0x68, p.PCLNTABLE), (0x80, p.FTAB), (0x98, p.FINDFUNCTAB),
                    (0xa0, RX), (0xa8, RX + 0x2000), (0xb0, RX),
                    (0xb8, RX + 0x2000), (0x158, p.GOFUNC_NEW)]:
        p.mov_store_ptr(b, off, va)
    for off, n in [(0x10, 16), (0x18, 16), (0x58, len(pctab)), (0x60, len(pctab)),
                   (0x70, 64), (0x78, 64), (0x88, 2), (0x90, 2)]:
        p.mov_store_imm(b, off, n)
    b.lea_abs(0, p.FIRST_MD)
    b.lea_abs(2, p.NEW_MD)
    b.emit(bytes.fromhex("48899030020000"))
    b.lea_abs(2, p.INIT_COUNT)
    b.emit(bytes.fromhex("48ff02"))
    b.jmp("ready")
    b.label("already")
    b.lea_abs(2, p.SECOND_RUN)
    b.emit(bytes.fromhex("48c70201000000"))
    b.label("ready")
    b.lea_abs(2, p.BRIDGE_STATE)
    b.emit(bytes.fromhex("48c70201000000"))
    b.lea_abs(0, RX)
    b.lea_abs(2, FUNCVAL)
    b.emit(bytes.fromhex("488902"))

    # Genuine stream in normal mode; fake stream only in the explicit mock copy.
    b.lea_abs(0, p.STREAM_ITAB_NEW)
    for slot, name in [(0x18, "mock_close"), (0x20, "mock_err"),
                       (0x28, "mock_msg"), (0x30, "mock_receive")]:
        b.lea_abs(2, RX + labels[name])
        b.emit(bytes.fromhex("488950") + bytes([slot]))
    p.mov_reg_rsp(b, 0, 0x720)  # setup saved *ServeMux at +0x1C0
    for cell in [MUX1, MUX2]:
        b.lea_abs(2, cell)
        b.emit(bytes.fromhex("483902"))
        b.jcc32(bytes.fromhex("0f84"), "done")
    # Exercise the original caller and donor stack maps before registration.
    b.call(p.RUNTIME_GC)
    p.mov_reg_rsp(b, 0, 0x720)
    b.lea_abs(3, RW + 0x618)
    b.emit(bytes.fromhex("b908000000"))
    b.lea_abs(7, FUNCVAL)
    b.call(p.HANDLEFUNC)
    p.mov_reg_rsp(b, 0, 0x720)
    b.lea_abs(2, MUX1)
    b.emit(bytes.fromhex("48833a00"))
    b.jcc32(bytes.fromhex("0f84"), "save_mux")
    b.lea_abs(2, MUX2)
    b.label("save_mux")
    b.emit(bytes.fromhex("488902"))
    b.label("done")
    p.mov_reg_rsp(b, 0, 0x720)
    b.emit(bytes.fromhex("4881c4500500005dc3"))
    boot = b.finish()
    assert BOOT + len(boot) < EMPTY_ARGS

    patched = bytearray(source)
    # Keep the real CLI bootstrap/login path and the original updater.  Runtime
    # auto-update is disabled by AGY_CLI_DISABLE_AUTO_UPDATE=true.
    assert patched[0x8BD113A:0x8BD1142] == bytes.fromhex("488b8424c0010000")
    patched[0x8BD113A:0x8BD1142] = (b"\xe8" + struct.pack("<i", DONOR - (0x8BD113A + 5))
                                    + b"\x90" * 3)
    assert patched[DONOR:DONOR + 0x1d] == bytes.fromhex(
        "4c8da42428fbffff4d3b66100f86650f0000554889e54881ec50050000")
    z = p.Program(DONOR + 0x1d)
    z.emit(bytes.fromhex("488d3c24b9aa00000031c0fcf348ab"))
    z.jmp_abs(BOOT)
    zblob = z.finish()
    patched[DONOR + 0x1d:DONOR + 0x1d + len(zblob)] = zblob
    patched[BOOT:BOOT + len(boot)] = boot
    patched[DONOR_STUB:DONOR_STUB + 10] = (
        b"\xe8" + struct.pack("<i", p.MORESTACK - (DONOR_STUB + 5))
        + b"\xe9" + struct.pack("<i", DONOR - (DONOR_STUB + 10)))
    patched[EMPTY_ARGS:EMPTY_ARGS + 8] = struct.pack("<ii", 26, 0)
    struct.pack_into("<ii", patched, DONOR_REC + 8, 0, 0)  # no args/deferreturn
    npc = p.u32(patched, DONOR_REC + 28)
    struct.pack_into("<I", patched, DONOR_REC + 44 + 4 * npc, EMPTY_ARGS - 0x416FC68)
    for j in (2, 5, 6):
        struct.pack_into("<I", patched, DONOR_REC + 44 + 4 * npc + 4 * j, 0xffffffff)
    patched += bytes(0x1000)

    def put(va, blob):
        off = RW_FILE + va - RW
        patched[off:off + len(blob)] = blob

    put(p.PCHEADER, hdr)
    put(p.FUNCNAMES, b"\0bridge.handler\0")
    put(p.PCTAB, pctab)
    put(p.PCLNTABLE, func)
    put(p.FTAB, struct.pack("<IIII", 0, 0, 0x2000, 0))
    put(p.FINDFUNCTAB, bytes(40))
    put(p.GOFUNC_NEW, gofunc)
    put(p.NEW_MD, bytes(0x238))
    put(RW + 0x618, b"/_bridge")

    patched += bytes(RX_FILE - len(patched))
    patched += handler + bytes([0x90]) * (0x2000 - len(handler))
    phoff = struct.unpack_from("<Q", source, 0x20)[0]
    last = phoff + 13 * 56
    assert p.u64(source, last + 8) == 0xD304000 and p.u64(source, last + 32) == 0x5100
    struct.pack_into("<QQ", patched, last + 32, 0x6100, 0x6100)
    note = phoff + 16 * 56
    assert p.u32(source, note) == 4 and p.u64(source, note + 8) == 0xD3090E0
    struct.pack_into("<IIQQQQQQ", patched, note, 1, 5, RX_FILE, RX, RX, 0x2000, 0x2000, 0x1000)

    result = bytes(patched)
    digest = _sha256(result)
    if expected_sha is not None and digest != expected_sha:
        raise ValueError(f"patched binary SHA mismatch: {digest} != {expected_sha}")
    plan = {
        "source_sha256": SOURCE_SHA256,
        "sha256": digest,
        "hook": "0x8bd113a",
        "donor": hex(DONOR),
        "bootstrap": hex(BOOT),
        "bootstrap_size": hex(len(boot)),
        "handler": hex(RX),
        "handler_size": hex(len(handler)),
        "stub_offset": hex(stub),
        "rw_base": hex(RW),
        "rw_file": hex(RW_FILE),
        "rx_file": hex(RX_FILE),
        "new_md": hex(p.NEW_MD),
        "canonical_itabs": replaced,
        "auto_update_env": "true",
    }
    return result, plan


def patch_file(source: Path, dest: Path, *, expected_sha: str | None = PATCHED_SHA256) -> dict:
    data = source.read_bytes()
    result, plan = apply(data, expected_sha=expected_sha)
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=dest.name + ".", dir=str(dest.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(result)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_name, 0o755)
        os.replace(tmp_name, dest)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    plan["output"] = str(dest)
    return plan


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--dest", required=True, type=Path)
    parser.add_argument("--check", action="store_true",
                        help="verify the source is the pinned build without writing")
    parser.add_argument("--allow-new", action="store_true",
                        help="write the output and print its SHA without enforcing PATCHED_SHA256 "
                             "(use only when intentionally updating the pinned patch)")
    args = parser.parse_args()
    if args.check:
        digest = _sha256(args.source.read_bytes())
        if digest != SOURCE_SHA256:
            raise SystemExit(f"source SHA mismatch: {digest}")
        print(json.dumps({"source_sha256": digest, "ok": True}, indent=2))
        return
    print(json.dumps(patch_file(args.source, args.dest,
                                expected_sha=None if args.allow_new else PATCHED_SHA256),
                     indent=2))


if __name__ == "__main__":
    main()
