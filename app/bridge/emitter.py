#!/usr/bin/env python3
"""Tested code generator for the fixed-SHA 1.4.2 bridge handler.

This module is imported by :mod:`app.bridge.patcher`, which overrides the
address constants below for the pinned 1.4.2 ELF.  The generator emits:

- handler code in a new RX PT_LOAD segment (request body -> GetChatMessage
  protojson -> NDJSON response events);
- a bootstrap that links a new moduledata into ``firstmoduledata.next`` and
  registers ``/_bridge`` from the ``setUpHTTPServeMux`` return block;
- new pclntab/ftab/stack-map records for the handler and bootstrap.

It is not a standalone patcher.  Do not run this file directly.
"""

from __future__ import annotations

import hashlib
import json
import struct
from pathlib import Path

EXPECTED_SHA = "60e72242f07c131f75afcd9a9e425d6ee097a1a1b3190ea98ae78fa4180bbfa8"
TEXT = 0x6152080
FTAB_VA = 0x321C6F8
GOFUNC = 0x410EC80
FIRST_MD = 0xB91A1A0
FIRST_NEXT_OFF = 0x230

MAYBE_LAUNCH = 0x91E9D31
MAYBE_LAUNCH_OLD = b"\xe8\xea\x66\xfe\xff"
SETUP_CALL1 = 0x8AFC3CA
SETUP_CALL2 = 0x8AFC632
SETUP_OLD = b"\xe8\x11\x58\xff\xff"
SETUP2_OLD = b"\xe8\xa9\x55\xff\xff"
SETUP_MUX = 0x8AF1BE0
MALLOCGC = 0x61E1480
RUNTIME_GC = 0x61848C0
HTTP_ERROR = 0x6624AE0
MORESTACK = 0x61EA480
HANDLEFUNC = 0x66266A0
NEWOBJECT = 0x6178520
PROTO_REFLECT = 0x6EE03E0
PROTOJSON_UNMARSHAL_OPTS = 0x66F1700
CONTEXT_BACKGROUND = 0x62DAA00
REQUEST_CONTEXT = 0x6695120
GETCHAT = 0x7DFA300
MIME_SET = 0x65A4CC0
PROTO_MARSHAL = 0x66B8240
PROTO_MARSHAL_OPTS = 0x66B8300
PROTOJSON_MARSHAL = 0x66F6720
PROTOJSON_UNMARSHAL = 0x66F1660
BASE64_ENC = 0x63023E0
STDENC_GLOBAL = 0xB6B6B90
STRCONV_ITOA = 0x6216EC0
RESP_TYPE = 0xAFC3E18
PTR_RESP_TYPE = 0x9F81948
RESP_PROTO_REFLECT = 0x6EE0820
REQ_TYPE = 0xAFC3B90

WRAPPER = 0x900B780
WRAPPER_FUNCOFF = 0xEAA900
WRAPPER_FRAME = 0x270
WRAPPER_PROLOGUE = 0x25
WRAPPER_CODE_OFF = 0x2A0
WRAPPER_PATTERN_OFF = 0x310
WRAPPER_WRITE = 0x320
WRAPPER_ARGS = 8

BOOT_ENTRY = 0x900C160
BOOT_FUNCOFF = 0xEAA9B0
BOOT_CODE = BOOT_ENTRY + 0x25
BOOT_FRAME = 0x550
BOOT_MAP = 0x900D060

CODE_VA = 0x10060000
CODE_SIZE = 0x2000
CODE_LIMIT = CODE_VA + CODE_SIZE
FRAME = 0x400
PCSP = FRAME + 8
NBIT = (FRAME - 0x20) // 8
PROLOGUE_LEN = 0x1D
# A 1M-token protojson request is several MiB; 64 KiB silently truncated it.
# Reserve 16 MiB per bridge request and reject anything larger with 413.
BODY_LIMIT = 0x1000000

# New RX segment, converted from the second PT_NOTE (file offset 0xD2560E0).
RX_PH_OLD_OFFSET = 0xD2560E0
RX_FILE_OFF = 0xD258000
RX_VADDR = CODE_VA
RX_SIZE = CODE_SIZE
RX_FLAGS = 5                      # PF_R | PF_X
RX_ALIGN = 0x1000

RW_VA = 0x10055100
RW_EXT = 0x600
PCHEADER = RW_VA + 0x80
FUNCNAMES = RW_VA + 0xC8
PCTAB = RW_VA + 0xF0
PCLNTABLE = RW_VA + 0x140
FTAB = RW_VA + 0x180
FINDFUNCTAB = RW_VA + 0x1A0
GOFUNC_NEW = RW_VA + 0x1C8        # after 2 * 20-byte findfunctab buckets
NEW_MD = RW_VA + 0x200
SERVER_GLOBAL = RW_VA + 0x480     # wrapper bootstrap stores *Server here
BRIDGE_STATE = RW_VA + 0x488      # 0=uninit, 1=ready, 2=conflict
INIT_COUNT = RW_VA + 0x490        # must be 1 after the two setup calls
SECOND_RUN = RW_VA + 0x498        # set by the idempotent second bootstrap
MSG_ITAB_NEW = RW_VA + 0x4A0      # hand-built proto.Message itab (request)
TEST_CONFLICT = RW_VA + 0x4C0     # debug build forces the conflict path
RESP_ITAB_NEW = RW_VA + 0x4C8     # hand-built proto.Message itab (response)
STREAM_ITAB_NEW = RW_VA + 0x4E8   # synthetic GetChatMessage stream itab
MOCK_RESP = RW_VA + 0x520         # synthetic stream response pointer
MOCK_STATE = RW_VA + 0x528        # synthetic stream Receive state
TEST_MODE = RW_VA + 0x530         # debug build uses the synthetic stream
SEQ_COUNTER = RW_VA + 0x538       # response sequence counter
PTR_REQ_TYPE = 0x9F81780          # *api_server_pb.GetChatMessageRequest rtype

# Marked locals slots: 0x80, 0x88, ..., 0xE8 (14 pointer slots).
# They sit above the largest outgoing argument area (0x50 for protojson).
POINTER_OFFSETS = [0x80 + 8 * i for i in range(20)]
POINTER_BITS = [(off - 0x20) // 8 for off in POINTER_OFFSETS]


def u32(b, o):
    return struct.unpack_from("<I", b, o)[0]


def u64(b, o):
    return struct.unpack_from("<Q", b, o)[0]


def zigzag(v):
    return (v << 1) ^ (v >> 63)


def uvarint(v):
    out = bytearray()
    while True:
        x = v & 0x7F
        v >>= 7
        if v:
            out.append(x | 0x80)
        else:
            out.append(x)
            return bytes(out)


def pctab_table(entries):
    # entries: [(value, pcDelta), ...]
    out = bytearray()
    val = -1
    for value, delta in entries:
        out += uvarint(zigzag(value - val))
        out += uvarint(delta)
        val = value
    out.append(0)
    return bytes(out)


class Program:
    def __init__(self, base):
        self.base = base
        self.b = bytearray()
        self.labels = {}
        self.fixups = []

    def emit(self, blob):
        self.b += blob

    def label(self, name):
        self.labels[name] = len(self.b)

    def call(self, target):
        rel = target - (self.base + len(self.b) + 5)
        assert -(1 << 31) <= rel < (1 << 31), (hex(target), hex(rel))
        self.emit(b"\xe8" + struct.pack("<i", rel))

    def jmp_abs(self, target):
        rel = target - (self.base + len(self.b) + 5)
        assert -(1 << 31) <= rel < (1 << 31)
        self.emit(b"\xe9" + struct.pack("<i", rel))

    def jmp(self, label):
        self.fixups.append(("jmp", label, len(self.b)))
        self.emit(b"\xe9\x00\x00\x00\x00")

    def jcc32(self, opcode, label):
        self.fixups.append(("jcc32", label, len(self.b)))
        self.emit(opcode + b"\x00\x00\x00\x00")

    def lea_rip(self, reg, label):
        rex = 0x48 | (1 if reg >= 8 else 0)
        modrm = 0x05 | ((reg & 7) << 3)
        self.fixups.append(("lea", label, len(self.b)))
        self.emit(bytes([rex, 0x8D, modrm]) + b"\x00\x00\x00\x00")

    def lea_abs(self, reg, target):
        rel = target - (self.base + len(self.b) + 7)
        assert -(1 << 31) <= rel < (1 << 31), (hex(target), hex(rel))
        rex = 0x48 | (1 if reg >= 8 else 0)
        modrm = 0x05 | ((reg & 7) << 3)
        self.emit(bytes([rex, 0x8D, modrm]) + struct.pack("<i", rel))

    def finish(self):
        for kind, label, pos in self.fixups:
            target = self.labels[label]
            if kind == "jmp":
                rel = target - (pos + 5)
                struct.pack_into("<i", self.b, pos + 1, rel)
            elif kind == "jcc32":
                rel = target - (pos + 6)
                struct.pack_into("<i", self.b, pos + 2, rel)
            elif kind == "lea":
                rel = target - (pos + 7)
                struct.pack_into("<i", self.b, pos + 3, rel)
        return bytes(self.b)


def mov_rsp_rax(p, off):
    p.emit(b"\x48\x89\x84\x24" + struct.pack("<I", off))


def mov_rax_rsp(p, off):
    p.emit(b"\x48\x8b\x84\x24" + struct.pack("<I", off))


def mov_reg_rsp(p, reg, off):
    """mov reg, [rsp+off]."""
    assert 0 <= reg < 16
    rex = 0x48 | (4 if reg >= 8 else 0)
    p.emit(bytes([rex, 0x8B, 0x84 | ((reg & 7) << 3), 0x24]) + struct.pack("<i", off))


def mov_rsp_reg(p, off, reg):
    """mov [rsp+off], reg."""
    assert 0 <= reg < 16
    rex = 0x48 | (4 if reg >= 8 else 0)
    p.emit(bytes([rex, 0x89, 0x84 | ((reg & 7) << 3), 0x24]) + struct.pack("<i", off))


def mov_reg_mem(p, dst, base, off):
    """mov dst, [base+off] (base must not be rsp/r12)."""
    assert 0 <= dst < 16 and 0 <= base < 16 and (base & 7) not in (4,)
    rex = 0x48 | (4 if dst >= 8 else 0) | (1 if base >= 8 else 0)
    p.emit(bytes([rex, 0x8B, 0x80 | ((dst & 7) << 3) | (base & 7)]) + struct.pack("<i", off))


def mov_mem_reg(p, base, off, src):
    """mov [base+off], src (base must not be rsp/r12)."""
    assert 0 <= base < 16 and 0 <= src < 16 and (base & 7) not in (4,)
    rex = 0x48 | (4 if src >= 8 else 0) | (1 if base >= 8 else 0)
    p.emit(bytes([rex, 0x89, 0x80 | ((src & 7) << 3) | (base & 7)]) + struct.pack("<i", off))


def mov_reg_reg(p, dst, src):
    """mov dst, src."""
    assert 0 <= dst < 16 and 0 <= src < 16
    rex = 0x48 | (4 if src >= 8 else 0) | (1 if dst >= 8 else 0)
    p.emit(bytes([rex, 0x89, 0xC0 | ((src & 7) << 3) | (dst & 7)]))


def test_reg_reg(p, a, b):
    assert 0 <= a < 16 and 0 <= b < 16
    rex = 0x48 | (4 if b >= 8 else 0) | (1 if a >= 8 else 0)
    p.emit(bytes([rex, 0x85, 0xC0 | ((b & 7) << 3) | (a & 7)]))


def xor32(p, a, b):
    assert 0 <= a < 8 and 0 <= b < 8
    p.emit(bytes([0x31, 0xC0 | (b << 3) | a]))


def mov_imm32(p, reg, value):
    assert 0 <= reg < 8
    p.emit(bytes([0xB8 + reg]) + struct.pack("<I", value))



def build_handler():
    p = Program(CODE_VA)
    msg_read = b'{"event":"failed","reason":"invalid_request","detail":"body"}\n'
    msg_body_large = b'{"event":"failed","reason":"request_too_large"}\n'
    msg_parse = b'{"event":"failed","reason":"invalid_request","detail":"protojson"}\n'
    msg_client = b'{"event":"failed","reason":"unavailable","detail":"client"}\n'
    msg_state = b'{"event":"failed","reason":"unavailable","detail":"bootstrap"}\n'
    msg_getchat = b'{"event":"failed","reason":"get_chat_message"}\n'
    msg_accepted = b'{"event":"accepted"}\n'
    msg_completed = b'{"event":"completed"}\n'
    msg_stream_failed = b'{"event":"failed","reason":"stream"}\n'
    msg_marshal_failed = b'{"event":"failed","reason":"marshal"}\n'
    msg_mock_failed = b'{"event":"failed","reason":"mock"}\n'
    resp_prefix = b'{"event":"response","seq":'
    resp_json_key = b',"json":'
    resp_raw_key = b',"raw_b64":"'
    resp_suffix = b'"}\n'
    mock_json = b'{"deltaText":"mock"}'
    ct_key = b"Content-Type"
    ct_val = b"application/x-ndjson"

    def emit_resp_itab():
        """Build the proto.Message itab for *GetChatMessageResponse."""
        p.lea_abs(0, RESP_ITAB_NEW)
        mov_rsp_reg(p, 0xF8, 0)                    # resp proto itab
        p.emit(bytes.fromhex("48c70000000000"))    # Inter=0
        p.lea_abs(2, PTR_RESP_TYPE)
        p.emit(bytes.fromhex("48895008"))          # Type
        p.emit(bytes.fromhex("c7401000000000"))    # Hash
        p.emit(bytes.fromhex("c7401400000000"))
        p.lea_abs(2, RESP_PROTO_REFLECT)
        p.emit(bytes.fromhex("48895018"))          # Fun[0]

    # Prologue: stack check, save rbp, allocate FRAME.
    p.emit(bytes.fromhex("4c8da42408fcffff"))  # lea r12,[rsp-0x3f8]
    p.emit(bytes.fromhex("4d3b6610"))          # cmp r12,0x10(r14)
    p.jcc32(bytes.fromhex("0f86"), "stub")     # jbe stub
    p.emit(b"\x55")                            # push rbp
    p.emit(bytes.fromhex("4889e5"))            # mov rbp,rsp
    p.emit(bytes.fromhex("4881ec00040000"))    # sub rsp,0x400
    assert len(p.b) == PROLOGUE_LEN, hex(len(p.b))

    # Preserve incoming (w.itab, w.data, r) while zeroing the frame.
    p.emit(bytes.fromhex("4989c0"))            # mov r8,rax
    p.emit(bytes.fromhex("4989d9"))            # mov r9,rbx
    p.emit(bytes.fromhex("4989ca"))            # mov r10,rcx
    p.emit(bytes.fromhex("488d3c24"))          # lea rdi,[rsp]
    p.emit(b"\xb9" + struct.pack("<I", FRAME // 8))
    p.emit(bytes.fromhex("31c0"))              # xor eax,eax
    p.emit(bytes.fromhex("fc"))                # cld
    p.emit(bytes.fromhex("f348ab"))            # rep stosq
    mov_rsp_reg(p, 0x80, 8)                    # w.itab
    mov_rsp_reg(p, 0x88, 9)                    # w.data
    mov_rsp_reg(p, 0x90, 10)                   # *http.Request

    # Fail closed unless bootstrap completed exactly once and the second
    # setup call took the idempotent path.
    p.lea_abs(0, BRIDGE_STATE)
    p.emit(bytes.fromhex("488b00"))            # mov rax,[rax]
    p.emit(bytes.fromhex("4883f801"))          # cmp rax,1
    p.jcc32(bytes.fromhex("0f85"), "state_fail")
    p.lea_abs(0, INIT_COUNT)
    p.emit(bytes.fromhex("488b00"))
    p.emit(bytes.fromhex("4883f801"))
    p.jcc32(bytes.fromhex("0f85"), "state_fail")
    p.lea_abs(0, SECOND_RUN)
    p.emit(bytes.fromhex("488b00"))
    p.emit(bytes.fromhex("4883f801"))
    p.jcc32(bytes.fromhex("0f85"), "state_fail")

    # Read the request body through the concrete (*http.body).Read method.
    # Passing the io.ReadCloser itab to io.ReadAll would dispatch Close
    # (interface method tables are name-sorted), so call Read directly.
    mov_reg_rsp(p, 2, 0x90)                    # rdx = r
    mov_reg_mem(p, 0, 2, 0x40)                 # rax = r.Body.itab
    test_reg_reg(p, 0, 0)
    p.jcc32(bytes.fromhex("0f84"), "read_err")
    mov_imm32(p, 0, BODY_LIMIT)                # eax = request body buffer
    xor32(p, 3, 3)                             # ebx = nil type
    xor32(p, 1, 1)                             # ecx = needscan false
    p.call(MALLOCGC)
    mov_rsp_reg(p, 0x98, 0)                    # body buffer (marked)
    p.emit(b"\x48\xc7\x84\x24" + struct.pack("<i", 0x300) + struct.pack("<I", 0))
    p.label("read_loop")
    mov_reg_rsp(p, 2, 0x90)                    # rdx = r
    mov_reg_mem(p, 11, 2, 0x40)                # r11 = Body.itab
    mov_reg_mem(p, 11, 11, 0x20)               # r11 = Body.Read (Fun[1])
    mov_reg_mem(p, 0, 2, 0x48)                 # rax = Body.data
    mov_reg_rsp(p, 3, 0x98)                    # rbx = buffer + total
    mov_reg_rsp(p, 1, 0x300)
    p.emit(b"\x48\x01\xcb")                    # add rbx,rcx
    mov_imm32(p, 1, BODY_LIMIT)
    mov_reg_rsp(p, 7, 0x300)
    p.emit(b"\x48\x29\xf9")                    # sub rcx,rdi
    mov_reg_reg(p, 7, 1)                       # rdi = rcx
    p.emit(b"\x41\xff\xd3")                    # call r11
    p.emit(b"\x48\x01\x84\x24" + struct.pack("<i", 0x300))  # add [total],rax
    test_reg_reg(p, 0, 0)
    p.jcc32(bytes.fromhex("0f84"), "read_done")
    p.emit(b"\x48\x81\xbc\x24" + struct.pack("<i", 0x300) + struct.pack("<I", BODY_LIMIT))
    p.jcc32(bytes.fromhex("0f82"), "read_loop")  # jb read_loop
    p.label("read_done")
    # A body that filled the buffer may have more bytes waiting. Reject it
    # rather than unmarshalling a silently truncated protojson document.
    mov_reg_rsp(p, 0, 0x300)
    p.emit(b"\x48\x3d" + struct.pack("<I", BODY_LIMIT))  # cmp rax, BODY_LIMIT
    p.jcc32(bytes.fromhex("0f83"), "body_large")         # jae body_large

    # Debug build: bypass the real request/GetChatMessage and drive the same
    # response/stream loop from a synthetic stream.
    p.lea_abs(0, TEST_MODE)
    p.emit(bytes.fromhex("488b00"))            # mov rax,[rax]
    p.emit(bytes.fromhex("4885c0"))            # test rax,rax
    p.jcc32(bytes.fromhex("0f85"), "mock_path")

    # Allocate *api_server_pb.GetChatMessageRequest (type 0xAFC3B90).
    # The type pointer is a PIE link-time address, so load it RIP-relative.
    p.lea_abs(0, REQ_TYPE)
    p.call(NEWOBJECT)
    mov_rsp_reg(p, 0xA0, 0)                    # req

    # Build a proto.Message itab: Fun[0] must be the generated ProtoReflect.
    # Inter/Hash are not used for a direct interface method call; the dynamic
    # type is *api_server_pb.GetChatMessageRequest (PtrToThis of the struct).
    p.lea_abs(0, MSG_ITAB_NEW)
    mov_rsp_reg(p, 0xA8, 0)                    # itab runtime address
    p.emit(bytes.fromhex("48c70000000000"))    # mov qword [rax],0
    p.lea_abs(2, PTR_REQ_TYPE)
    p.emit(bytes.fromhex("48895008"))          # mov [rax+8],rdx
    p.emit(bytes.fromhex("c7401000000000"))    # mov dword [rax+0x10],0
    p.emit(bytes.fromhex("c7401400000000"))    # mov dword [rax+0x14],0
    p.lea_abs(2, PROTO_REFLECT)
    p.emit(bytes.fromhex("48895018"))          # mov [rax+0x18],rdx

    # Exercise GC with the typed request/body/message on the marked stack.
    p.call(RUNTIME_GC)

    # protojson.UnmarshalOptions.unmarshal(options{}, body, proto.Message).
    mov_reg_rsp(p, 0, 0xA8)
    mov_rsp_reg(p, 0x0, 0)                    # iface.itab
    mov_reg_rsp(p, 0, 0xA0)
    mov_rsp_reg(p, 0x8, 0)                    # iface.data = req
    xor32(p, 0, 0)                             # options: ax
    xor32(p, 3, 3)                             # options: bx
    xor32(p, 1, 1)                             # options: cx
    xor32(p, 7, 7)                             # options: di
    xor32(p, 6, 6)                             # options: si
    mov_reg_rsp(p, 8, 0x98)                    # r8 = body.ptr
    mov_reg_rsp(p, 9, 0x300)                    # r9 = body.len
    mov_reg_reg(p, 10, 9)                      # r10 = body.cap (= len)
    p.call(PROTOJSON_UNMARSHAL_OPTS)
    test_reg_reg(p, 0, 0)
    p.jcc32(bytes.fromhex("0f85"), "parse_err")

    # Request.Context(): propagate client disconnect/cancellation upstream.
    mov_reg_rsp(p, 0, 0x90)                    # rax = *http.Request
    p.call(REQUEST_CONTEXT)                    # ax=ctx.itab, bx=ctx.data
    mov_rsp_reg(p, 0xB8, 0)
    mov_rsp_reg(p, 0xC0, 3)

    # [Server+0x3d0] -> *CodeAssistClient.
    p.lea_abs(0, SERVER_GLOBAL)
    mov_reg_mem(p, 0, 0, 0)
    test_reg_reg(p, 0, 0)
    p.jcc32(bytes.fromhex("0f84"), "no_client")
    mov_reg_mem(p, 0, 0, 0x3D0)
    test_reg_reg(p, 0, 0)
    p.jcc32(bytes.fromhex("0f84"), "no_client")
    mov_rsp_reg(p, 0xC8, 0)

    # CodeAssistClient.GetChatMessage(ctx, req).
    mov_reg_rsp(p, 7, 0xA0)                    # rdi = req
    mov_reg_rsp(p, 3, 0xB8)                    # rbx = ctx.itab
    mov_reg_rsp(p, 1, 0xC0)                    # rcx = ctx.data
    mov_reg_rsp(p, 0, 0xC8)                    # rax = client
    p.call(GETCHAT)
    mov_rsp_reg(p, 0xD0, 0)                    # stream.itab
    mov_rsp_reg(p, 0xD8, 3)                    # stream.data
    mov_rsp_reg(p, 0xE0, 1)                    # err.itab (rcx)
    mov_rsp_reg(p, 0xE8, 7)                    # err.data (rdi)
    test_reg_reg(p, 1, 1)
    p.jcc32(bytes.fromhex("0f85"), "getchat_err")

    # Real stream: build the response itab and enter the shared stream loop.
    emit_resp_itab()
    p.emit(b"\x48\xc7\x84\x24" + struct.pack("<i", 0x320) + struct.pack("<I", 0))
    p.jmp("stream_prepare")

    # ---- synthetic response/stream path (debug build) ----
    p.label("mock_path")
    p.lea_abs(0, RESP_TYPE)
    p.call(NEWOBJECT)
    mov_rsp_reg(p, 0xF0, 0)                    # resp message
    emit_resp_itab()
    # protojson.Unmarshal(mock_json, resp)
    p.lea_rip(0, "mock_json")
    mov_imm32(p, 3, len(mock_json))
    mov_imm32(p, 1, len(mock_json))
    mov_reg_rsp(p, 7, 0xF8)                    # DI = proto.Message itab
    mov_reg_rsp(p, 6, 0xF0)                    # SI = resp
    p.call(PROTOJSON_UNMARSHAL)
    test_reg_reg(p, 0, 0)
    p.jcc32(bytes.fromhex("0f85"), "mock_fail")
    p.lea_abs(0, MOCK_RESP)
    mov_reg_rsp(p, 2, 0xF0)
    p.emit(bytes.fromhex("488910"))            # mov [rax],rdx
    p.lea_abs(0, MOCK_STATE)
    p.emit(bytes.fromhex("48c70002000000"))    # MOCK_STATE=2 (two messages)
    p.lea_abs(0, STREAM_ITAB_NEW)
    mov_rsp_reg(p, 0xD0, 0)                    # synthetic stream itab
    xor32(p, 0, 0)
    mov_rsp_reg(p, 0xD8, 0)                    # stream data = 0
    p.emit(b"\x48\xc7\x84\x24" + struct.pack("<i", 0x320) + struct.pack("<I", 0))
    p.jmp("stream_prepare")

    # ---- shared response writer / stream loop ----
    def emit_write_label(label, blob):
        p.lea_rip(3, label)
        mov_imm32(p, 1, len(blob))
        mov_reg_reg(p, 7, 1)
        mov_reg_rsp(p, 0, 0x88)
        mov_reg_rsp(p, 2, 0x80)
        mov_reg_mem(p, 2, 2, 0x20)
        p.emit(b"\xff\xd2")

    def emit_write_slice(ptr_off, len_off):
        mov_reg_rsp(p, 3, ptr_off)
        mov_reg_rsp(p, 1, len_off)
        mov_reg_reg(p, 7, 1)
        mov_reg_rsp(p, 0, 0x88)
        mov_reg_rsp(p, 2, 0x80)
        mov_reg_mem(p, 2, 2, 0x20)
        p.emit(b"\xff\xd2")

    def emit_stream_close():
        mov_reg_rsp(p, 0, 0xD8)
        mov_reg_rsp(p, 2, 0xD0)
        mov_reg_mem(p, 2, 2, 0x18)
        p.emit(b"\xff\xd2")

    p.label("stream_prepare")
    mov_imm32(p, 6, 200)
    mov_rsp_reg(p, 0x318, 6)                   # status=200
    mov_reg_rsp(p, 0, 0x80)
    test_reg_reg(p, 0, 0)
    p.jcc32(bytes.fromhex("0f84"), "epilogue")
    mov_reg_rsp(p, 3, 0x88)
    mov_reg_mem(p, 1, 0, 0x18)                 # Header
    mov_reg_reg(p, 0, 3)
    p.emit(b"\xff\xd1")
    p.lea_rip(3, "ct_key")
    mov_imm32(p, 1, len(ct_key))
    p.lea_rip(7, "ct_val")
    mov_imm32(p, 6, len(ct_val))
    p.call(MIME_SET)
    mov_reg_rsp(p, 2, 0x80)
    mov_reg_mem(p, 2, 2, 0x28)                 # WriteHeader
    mov_reg_rsp(p, 0, 0x88)
    mov_reg_rsp(p, 3, 0x318)
    p.emit(b"\xff\xd2")
    emit_write_label("msg_accepted", msg_accepted)

    p.label("stream_loop")
    # Receive()
    mov_reg_rsp(p, 0, 0xD8)
    mov_reg_rsp(p, 2, 0xD0)
    mov_reg_mem(p, 2, 2, 0x30)
    p.emit(b"\xff\xd2")
    p.emit(b"\x84\xc0")                        # test al,al
    p.jcc32(bytes.fromhex("0f84"), "stream_end")
    # Msg()
    mov_reg_rsp(p, 0, 0xD8)
    mov_reg_rsp(p, 2, 0xD0)
    mov_reg_mem(p, 2, 2, 0x28)
    p.emit(b"\xff\xd2")
    test_reg_reg(p, 0, 0)
    p.jcc32(bytes.fromhex("0f84"), "stream_failed")
    mov_rsp_reg(p, 0xF0, 0)                    # response message
    # proto.MarshalOptions{Deterministic:true}.Marshal(msg)
    mov_imm32(p, 0, 1)                         # AL = Deterministic
    xor32(p, 3, 3)                             # BL = AllowPartial false
    xor32(p, 1, 1)                             # CL = UseCachedSize false
    mov_reg_rsp(p, 7, 0xF8)                    # DI = proto.Message itab
    mov_reg_rsp(p, 6, 0xF0)                    # SI = msg
    p.call(PROTO_MARSHAL_OPTS)
    mov_rsp_reg(p, 0x100, 0)
    mov_rsp_reg(p, 0x328, 3)
    mov_rsp_reg(p, 0x330, 1)
    test_reg_reg(p, 7, 7)
    p.jcc32(bytes.fromhex("0f85"), "marshal_failed")
    # protojson.Marshal(msg)
    mov_reg_rsp(p, 0, 0xF8)
    mov_reg_rsp(p, 3, 0xF0)
    p.call(PROTOJSON_MARSHAL)
    mov_rsp_reg(p, 0x108, 0)
    mov_rsp_reg(p, 0x338, 3)
    mov_rsp_reg(p, 0x340, 1)
    test_reg_reg(p, 7, 7)
    p.jcc32(bytes.fromhex("0f85"), "marshal_failed")
    # base64.StdEncoding.EncodeToString(raw)
    p.lea_abs(0, STDENC_GLOBAL)
    p.emit(bytes.fromhex("488b00"))            # mov rax,[rax]
    mov_reg_rsp(p, 3, 0x100)
    mov_reg_rsp(p, 1, 0x328)
    mov_reg_rsp(p, 7, 0x330)
    p.call(BASE64_ENC)
    mov_rsp_reg(p, 0x110, 0)
    mov_rsp_reg(p, 0x348, 3)
    # strconv.Itoa(seq)
    mov_reg_rsp(p, 0, 0x320)
    p.call(STRCONV_ITOA)
    mov_rsp_reg(p, 0x118, 0)
    mov_rsp_reg(p, 0x350, 3)
    # NDJSON response envelope (multiple ResponseWriter.Write calls).
    emit_write_label("resp_prefix", resp_prefix)
    emit_write_slice(0x118, 0x350)
    emit_write_label("resp_json_key", resp_json_key)
    emit_write_slice(0x108, 0x338)
    emit_write_label("resp_raw_key", resp_raw_key)
    emit_write_slice(0x110, 0x348)
    emit_write_label("resp_suffix", resp_suffix)
    p.emit(b"\x48\xff\x84\x24" + struct.pack("<i", 0x320))  # seq++
    p.jmp("stream_loop")

    p.label("stream_end")
    # Err()
    mov_reg_rsp(p, 0, 0xD8)
    mov_reg_rsp(p, 2, 0xD0)
    mov_reg_mem(p, 2, 2, 0x20)
    p.emit(b"\xff\xd2")
    test_reg_reg(p, 0, 0)
    p.jcc32(bytes.fromhex("0f85"), "stream_failed")
    emit_stream_close()
    emit_write_label("msg_completed", msg_completed)
    p.jmp("epilogue")

    p.label("stream_failed")
    emit_write_label("msg_stream_failed", msg_stream_failed)
    emit_stream_close()
    p.jmp("epilogue")

    p.label("marshal_failed")
    emit_write_label("msg_marshal_failed", msg_marshal_failed)
    emit_stream_close()
    p.jmp("epilogue")

    p.label("mock_fail")
    p.lea_rip(1, "msg_mock_failed")
    mov_imm32(p, 7, len(msg_mock_failed))
    mov_imm32(p, 6, 502)
    p.jmp("respond")

    p.label("read_err")
    p.lea_rip(1, "msg_read")
    mov_imm32(p, 7, len(msg_read))
    mov_imm32(p, 6, 400)
    p.jmp("respond")

    p.label("body_large")
    p.lea_rip(1, "msg_body_large")
    mov_imm32(p, 7, len(msg_body_large))
    mov_imm32(p, 6, 413)
    p.jmp("respond")

    p.label("parse_err")
    p.lea_rip(1, "msg_parse")
    mov_imm32(p, 7, len(msg_parse))
    mov_imm32(p, 6, 400)
    p.jmp("respond")

    p.label("no_client")
    p.lea_rip(1, "msg_client")
    mov_imm32(p, 7, len(msg_client))
    mov_imm32(p, 6, 503)
    p.jmp("respond")

    p.label("state_fail")
    p.lea_rip(1, "msg_state")
    mov_imm32(p, 7, len(msg_state))
    mov_imm32(p, 6, 503)
    p.jmp("respond")

    p.label("getchat_err")
    p.lea_rip(1, "msg_getchat")
    mov_imm32(p, 7, len(msg_getchat))
    mov_imm32(p, 6, 502)

    p.label("respond")
    # Save the message and status, then set Content-Type and write the NDJSON
    # line through ResponseWriter instead of http.Error (text/plain).
    mov_rsp_reg(p, 0x308, 1)                    # msg.ptr
    mov_rsp_reg(p, 0x310, 7)                   # msg.len
    mov_rsp_reg(p, 0x318, 6)                   # status
    mov_reg_rsp(p, 0, 0x80)                    # rax = w.itab
    test_reg_reg(p, 0, 0)
    p.jcc32(bytes.fromhex("0f84"), "epilogue")
    mov_reg_rsp(p, 3, 0x88)                    # rbx = w.data
    mov_reg_mem(p, 1, 0, 0x18)                 # rcx = Header method
    mov_reg_reg(p, 0, 3)                       # rax = w.data
    p.emit(b"\xff\xd1")                        # call rcx
    # Header map in RAX; MIMEHeader.Set(map, key, value).
    p.lea_rip(3, "ct_key")
    mov_imm32(p, 1, len(ct_key))
    p.lea_rip(7, "ct_val")
    mov_imm32(p, 6, len(ct_val))
    p.call(MIME_SET)
    # w.WriteHeader(status).
    mov_reg_rsp(p, 2, 0x80)
    mov_reg_mem(p, 2, 2, 0x28)
    mov_reg_rsp(p, 0, 0x88)
    mov_reg_rsp(p, 3, 0x318)
    p.emit(b"\xff\xd2")                        # call rdx
    # w.Write(msg).
    mov_reg_rsp(p, 2, 0x80)
    mov_reg_mem(p, 2, 2, 0x20)
    mov_reg_rsp(p, 0, 0x88)
    mov_reg_rsp(p, 3, 0x308)
    mov_reg_rsp(p, 1, 0x310)
    mov_reg_reg(p, 7, 1)
    p.emit(b"\xff\xd2")                        # call rdx

    p.label("epilogue")
    p.emit(bytes.fromhex("4881c400040000"))    # add rsp,0x400
    p.emit(b"\x5d")
    p.emit(b"\xc3")

    p.label("stub")
    p.call(MORESTACK)
    p.jmp_abs(CODE_VA)

    # Synthetic GetChatMessageServerStreamForClientInterface methods.
    # These run after stub (empty StackMapIndex) and make no calls.
    p.label("mock_close")
    p.emit(b"\xc3")                            # ret
    p.label("mock_err")
    p.emit(b"\x31\xc0")                        # xor eax,eax
    p.emit(b"\x31\xdb")                        # xor ebx,ebx
    p.emit(b"\xc3")
    p.label("mock_msg")
    p.lea_abs(0, MOCK_RESP)
    p.emit(b"\x48\x8b\x00")                    # mov rax,[rax]
    p.emit(b"\xc3")
    p.label("mock_receive")
    p.lea_abs(0, MOCK_STATE)
    p.emit(b"\x48\x8b\x10")                    # mov rdx,[rax]
    p.emit(b"\x48\x85\xd2")                    # test rdx,rdx
    p.jcc32(bytes.fromhex("0f84"), "mock_recv_false")
    p.emit(b"\x48\xff\xca")                    # dec rdx
    p.emit(b"\x48\x89\x10")                    # mov [rax],rdx
    p.emit(b"\xb8\x01\x00\x00\x00")            # mov eax,1
    p.emit(b"\xc3")
    p.label("mock_recv_false")
    p.emit(b"\x31\xc0")                        # xor eax,eax
    p.emit(b"\xc3")

    p.label("msg_read")
    p.emit(msg_read)
    p.label("msg_body_large")
    p.emit(msg_body_large)
    p.label("msg_parse")
    p.emit(msg_parse)
    p.label("msg_client")
    p.emit(msg_client)
    p.label("msg_state")
    p.emit(msg_state)
    p.label("msg_getchat")
    p.emit(msg_getchat)
    p.label("msg_accepted")
    p.emit(msg_accepted)
    p.label("ct_key")
    p.emit(ct_key)
    p.label("ct_val")
    p.emit(ct_val)
    p.label("msg_completed")
    p.emit(msg_completed)
    p.label("msg_stream_failed")
    p.emit(msg_stream_failed)
    p.label("msg_marshal_failed")
    p.emit(msg_marshal_failed)
    p.label("msg_mock_failed")
    p.emit(msg_mock_failed)
    p.label("resp_prefix")
    p.emit(resp_prefix)
    p.label("resp_json_key")
    p.emit(resp_json_key)
    p.label("resp_raw_key")
    p.emit(resp_raw_key)
    p.label("resp_suffix")
    p.emit(resp_suffix)
    p.label("mock_json")
    p.emit(mock_json)
    return p.finish(), p.labels["stub"], dict(p.labels)


def mov_store_ptr(p, off, target):
    p.lea_abs(0, target)              # rax = target
    if off <= 0x7F:
        p.emit(bytes.fromhex("488943") + bytes([off]))
    else:
        p.emit(bytes.fromhex("488983") + struct.pack("<I", off))


def mov_store_imm(p, off, value):
    if off <= 0x7F:
        p.emit(bytes.fromhex("48c743") + bytes([off]) + struct.pack("<I", value))
    else:
        p.emit(bytes.fromhex("48c783") + struct.pack("<I", off) + struct.pack("<I", value))


def build_bootstrap(code_size):
    p = Program(BOOT_CODE)
    p.lea_abs(2, SERVER_GLOBAL)       # rdx = &server_global
    p.emit(b"\x48\x89\x02")           # mov [rdx], rax (Server)
    p.lea_abs(3, NEW_MD)              # rbx = new moduledata
    mov_store_ptr(p, 0x00, PCHEADER)
    mov_store_ptr(p, 0x08, FUNCNAMES)
    mov_store_imm(p, 0x10, 16)
    mov_store_imm(p, 0x18, 16)
    mov_store_ptr(p, 0x50, PCTAB)
    mov_store_imm(p, 0x58, 0)         # patched later
    mov_store_imm(p, 0x60, 0)
    mov_store_ptr(p, 0x68, PCLNTABLE)
    mov_store_imm(p, 0x70, 64)
    mov_store_imm(p, 0x78, 64)
    mov_store_ptr(p, 0x80, FTAB)
    mov_store_imm(p, 0x88, 2)
    mov_store_imm(p, 0x90, 2)
    mov_store_ptr(p, 0x98, FINDFUNCTAB)
    mov_store_ptr(p, 0xA0, CODE_VA)
    mov_store_ptr(p, 0xA8, CODE_VA + code_size)
    mov_store_ptr(p, 0xB0, CODE_VA)
    mov_store_ptr(p, 0xB8, CODE_VA + code_size)
    mov_store_ptr(p, 0x158, GOFUNC_NEW)
    # firstmoduledata.next = new_md
    p.lea_abs(0, FIRST_MD)
    p.lea_abs(2, NEW_MD)
    p.emit(bytes.fromhex("488990") + struct.pack("<I", FIRST_NEXT_OFF))
    # epilogue
    p.emit(bytes.fromhex("4881c4") + struct.pack("<I", BOOT_FRAME))
    p.emit(bytes.fromhex("5d"))
    p.emit(bytes.fromhex("c3"))
    return p.finish()


def build_wrapper(code_size_unused):
    code = bytearray(b"\x90" * WRAPPER_WRITE)
    prologue = None
    return code, prologue


def main():
    import os

    repo = Path(__file__).resolve().parents[4]
    src = repo / ".devdata/release-v0.0.17/native-cli/agy"
    force_conflict = os.environ.get("DAY2C_FORCE_CONFLICT") == "1"
    force_mock = os.environ.get("DAY3_MOCK_STREAM") == "1"
    out_name = "agy.fullmodule"
    if force_conflict:
        out_name = "agy.fullmodule.conflict"
    elif force_mock:
        out_name = "agy.fullmodule.mock"
    out = Path(__file__).resolve().parent / out_name
    data = bytearray(src.read_bytes())
    assert hashlib.sha256(data).hexdigest() == EXPECTED_SHA

    # Handler code: real code at the start, NOP padding to the full 0x2000
    # segment so the module metadata/ftab/findfunctab cover the whole segment.
    handler_code, stub_off, handler_labels = build_handler()
    assert len(handler_code) <= CODE_SIZE, hex(len(handler_code))
    assert PROLOGUE_LEN < stub_off < len(handler_code), hex(stub_off)
    handler = handler_code + b"\x90" * (CODE_SIZE - len(handler_code))
    code_size = CODE_SIZE

    # pctab tables.
    pcsp_off = 1
    # Prologue and the morestack stub have no allocated frame; only the body
    # uses PCSP. A non-zero pcsp at the stub makes newstack believe a frame
    # exists and unwind uninitialized stack garbage.
    pcsp_tab = pctab_table(
        [(0, PROLOGUE_LEN), (PCSP, stub_off - PROLOGUE_LEN), (0, code_size - stub_off)]
    )
    unsafe_off = pcsp_off + len(pcsp_tab)
    unsafe_tab = pctab_table([(-2, code_size)])
    stack_off = unsafe_off + len(unsafe_tab)
    # StackMapIndex: 0 = empty (prologue and morestack stub), 1 = full body.
    stack_tab = pctab_table(
        [
            (0, PROLOGUE_LEN),
            (1, stub_off - PROLOGUE_LEN),
            (0, code_size - stub_off),
        ]
    )
    pctab = b"\x00" + pcsp_tab + unsafe_tab + stack_tab

    # Stack maps: index 0 (prologue/morestack stub) marks the register-ABI
    # incoming w/r spills so stack growth preserves them; index 1 (body) keeps
    # args empty so callee spills in the outgoing area are never scanned.
    args_map = struct.pack("<ii", 2, 3) + b"\x07\x00"
    locals_bits = bytearray((NBIT + 7) // 8)
    for i in POINTER_BITS:
        locals_bits[i // 8] |= 1 << (i % 8)
    locals_empty = bytes((NBIT + 7) // 8)
    locals_map = (
        struct.pack("<ii", 2, NBIT) + locals_empty + bytes(locals_bits)
    )
    gofunc = args_map + locals_map
    assert len(gofunc) <= NEW_MD - GOFUNC_NEW, (hex(len(gofunc)), hex(NEW_MD - GOFUNC_NEW))

    # _func record.
    func = bytearray(64)
    struct.pack_into("<I", func, 0x00, 0)          # entryOff
    struct.pack_into("<i", func, 0x04, 1)          # nameOff
    struct.pack_into("<i", func, 0x08, 24)         # args
    struct.pack_into("<i", func, 0x0C, 0)          # deferreturn
    struct.pack_into("<I", func, 0x10, pcsp_off)
    struct.pack_into("<I", func, 0x14, 0)          # pcfile
    struct.pack_into("<I", func, 0x18, 0)          # pcln
    struct.pack_into("<I", func, 0x1C, 2)          # npcdata
    struct.pack_into("<I", func, 0x20, 0)          # cuOffset
    struct.pack_into("<i", func, 0x24, 0)          # startLine
    func[0x28] = 0                                  # funcID
    func[0x29] = 0                                  # flag
    func[0x2B] = 3                                  # nfuncdata
    struct.pack_into("<I", func, 0x2C, unsafe_off)
    struct.pack_into("<I", func, 0x30, stack_off)
    struct.pack_into("<I", func, 0x34, 0)
    struct.pack_into("<I", func, 0x38, len(args_map))
    struct.pack_into("<I", func, 0x3C, 0xFFFFFFFF)

    # pcHeader: copy the target header then patch the offsets.
    header = bytearray(data[0x14FB240:0x14FB288])
    struct.pack_into("<Q", header, 0x08, 1)
    struct.pack_into("<Q", header, 0x10, 0)
    struct.pack_into("<Q", header, 0x18, 0)
    struct.pack_into("<Q", header, 0x20, FUNCNAMES - PCHEADER)
    struct.pack_into("<Q", header, 0x28, 0)
    struct.pack_into("<Q", header, 0x30, 0)
    struct.pack_into("<Q", header, 0x38, PCTAB - PCHEADER)
    struct.pack_into("<Q", header, 0x40, PCLNTABLE - PCHEADER)

    # ftab + findfunctab (one 20-byte bucket per 4 KiB of code).
    ftab = struct.pack("<II", 0, 0) + struct.pack("<II", code_size, 0)
    n_buckets = (code_size + 0xFFF) // 0x1000
    assert n_buckets == 2, n_buckets
    findfunctab = b"\x00" * (20 * n_buckets)

    # New moduledata (bootstrap fills pointers/lengths).
    new_md = bytearray(0x238)

    # Bootstrap code only needs code_size; patch pctab len after pctab built.
    boot = build_bootstrap(code_size)
    # Replace the placeholder pctab length/cap (currently 0) by rebuilding with
    # the actual length: easiest is to locate the two imm32 stores at offsets
    # 0x58/0x60 and patch by regenerating with a length parameter.
    # Rebuild with actual pctab len.
    def build_bootstrap_len(code_size, pctab_len, labels):
        p = Program(BOOT_CODE)
        # RAX = *Server from the wrapper. Record it before clobbering RAX.
        p.lea_abs(2, SERVER_GLOBAL)
        p.emit(b"\x48\x89\x02")       # mov [rdx], rax
        # Synthetic stream itab (Inter/Type/Hash 0; methods after stub).
        p.lea_abs(0, STREAM_ITAB_NEW)
        p.emit(bytes.fromhex("48c70000000000"))    # Inter=0
        p.emit(bytes.fromhex("48c7400800000000"))  # Type=0
        p.emit(bytes.fromhex("48c7401000000000"))  # Hash
        for slot, name in (
            (0x18, "mock_close"),
            (0x20, "mock_err"),
            (0x28, "mock_msg"),
            (0x30, "mock_receive"),
        ):
            p.lea_abs(2, CODE_VA + labels[name])
            p.emit(bytes.fromhex("488950") + bytes([slot]))

        # Debug hook: force the conflict path in a dedicated negative-control
        # build. Normal builds leave TEST_CONFLICT == 0.
        p.lea_abs(2, TEST_CONFLICT)
        p.emit(b"\x48\x8b\x12")       # mov rdx,[rdx]
        p.emit(b"\x48\x85\xd2")       # test rdx,rdx
        p.jcc32(bytes.fromhex("0f85"), "conflict")

        # Fail closed unless firstmoduledata.next is 0 (first run) or already
        # points at our module (second setup call).
        p.lea_abs(0, FIRST_MD)
        p.emit(bytes.fromhex("488b90") + struct.pack("<I", FIRST_NEXT_OFF))
        p.emit(b"\x48\x85\xd2")       # test rdx,rdx
        p.jcc32(bytes.fromhex("0f84"), "init")
        p.lea_abs(1, NEW_MD)
        p.emit(b"\x48\x39\xca")       # cmp rdx,rcx
        p.jcc32(bytes.fromhex("0f84"), "already")
        p.jmp("conflict")

        p.label("init")
        p.lea_abs(3, NEW_MD)
        mov_store_ptr(p, 0x00, PCHEADER)
        mov_store_ptr(p, 0x08, FUNCNAMES)
        mov_store_imm(p, 0x10, 16)
        mov_store_imm(p, 0x18, 16)
        mov_store_ptr(p, 0x50, PCTAB)
        mov_store_imm(p, 0x58, pctab_len)
        mov_store_imm(p, 0x60, pctab_len)
        mov_store_ptr(p, 0x68, PCLNTABLE)
        mov_store_imm(p, 0x70, 64)
        mov_store_imm(p, 0x78, 64)
        mov_store_ptr(p, 0x80, FTAB)
        mov_store_imm(p, 0x88, 2)
        mov_store_imm(p, 0x90, 2)
        mov_store_ptr(p, 0x98, FINDFUNCTAB)
        mov_store_ptr(p, 0xA0, CODE_VA)
        mov_store_ptr(p, 0xA8, CODE_VA + code_size)
        mov_store_ptr(p, 0xB0, CODE_VA)
        mov_store_ptr(p, 0xB8, CODE_VA + code_size)
        mov_store_ptr(p, 0x158, GOFUNC_NEW)
        # firstmoduledata.next = new_md.
        p.lea_abs(0, FIRST_MD)
        p.lea_abs(2, NEW_MD)
        p.emit(bytes.fromhex("488990") + struct.pack("<I", FIRST_NEXT_OFF))
        # init_count++ (must stay exactly 1) and state=ready.
        p.lea_abs(2, INIT_COUNT)
        p.emit(b"\x48\xff\x02")       # inc qword [rdx]
        p.lea_abs(2, BRIDGE_STATE)
        p.emit(bytes.fromhex("48c70201000000"))
        p.jmp("boot_done")

        p.label("already")
        # Second setUpHTTPServeMux call: do not rewrite moduledata.
        p.lea_abs(2, SECOND_RUN)
        p.emit(bytes.fromhex("48c70201000000"))
        p.lea_abs(2, BRIDGE_STATE)
        p.emit(bytes.fromhex("48c70201000000"))
        p.jmp("boot_done")

        p.label("conflict")
        # A foreign firstmoduledata.next (or the debug hook): never overwrite.
        p.lea_abs(2, BRIDGE_STATE)
        p.emit(bytes.fromhex("48c70202000000"))  # state=2

        p.label("boot_done")
        p.emit(bytes.fromhex("4881c4") + struct.pack("<I", BOOT_FRAME))
        p.emit(bytes.fromhex("5d"))
        p.emit(bytes.fromhex("c3"))
        return p.finish()

    boot = build_bootstrap_len(code_size, len(pctab), handler_labels)
    assert BOOT_CODE + len(boot) < BOOT_MAP, (hex(BOOT_CODE + len(boot)), hex(BOOT_MAP))

    # Wrapper code (based on Q6 Stage 3).
    code = bytearray(b"\x90" * WRAPPER_WRITE)
    prologue = data[WRAPPER:WRAPPER + WRAPPER_PROLOGUE]
    assert prologue[:3] == b"\x4c\x8d\xa4", prologue[:3].hex()
    assert prologue[0x0C:0x0E] == b"\x0f\x86", prologue[0x0C:0x0E].hex()
    code[:WRAPPER_PROLOGUE] = prologue
    pos = WRAPPER_PROLOGUE

    def emit(blob):
        nonlocal pos
        code[pos:pos + len(blob)] = blob
        pos += len(blob)

    emit(bytes.fromhex("48890424"))
    emit(bytes.fromhex("488d7c2408"))
    emit(b"\xb9" + struct.pack("<I", (WRAPPER_FRAME - 8) // 8))
    emit(bytes.fromhex("31c0"))
    emit(bytes.fromhex("f348ab"))
    emit(bytes.fromhex("488b0424"))
    pos = WRAPPER_CODE_OFF

    def wcall(target):
        rel = target - (WRAPPER + pos + 5)
        assert -(1 << 31) <= rel < (1 << 31), (hex(target), hex(rel))
        emit(b"\xe8" + struct.pack("<i", rel))

    # Link the new module before registering the route.
    wcall(BOOT_ENTRY)
    emit(bytes.fromhex("488b0424"))          # mov rax,[rsp]
    wcall(SETUP_MUX)
    emit(bytes.fromhex("4889442430"))        # mov [rsp+0x30],rax
    emit(b"\xb8" + struct.pack("<I", 16))
    emit(bytes.fromhex("31db"))
    emit(bytes.fromhex("31c9"))
    wcall(MALLOCGC)
    emit(bytes.fromhex("4889c2"))
    lea_off = pos
    emit(bytes.fromhex("488d0d00000000"))
    delta = CODE_VA - (WRAPPER + lea_off + 7)
    assert -(1 << 31) <= delta < (1 << 31)
    emit(bytes.fromhex("4881c1") + struct.pack("<i", delta))
    emit(bytes.fromhex("48890a"))
    lea_pattern = pos
    emit(bytes.fromhex("488d1d00000000"))
    emit(b"\xb9" + struct.pack("<I", 8))
    emit(bytes.fromhex("4889d7"))
    emit(bytes.fromhex("488b442430"))
    wcall(HANDLEFUNC)
    emit(bytes.fromhex("488b442430"))
    emit(bytes.fromhex("4881c4") + struct.pack("<I", WRAPPER_FRAME))
    emit(bytes.fromhex("5dc3"))
    assert pos <= WRAPPER_PATTERN_OFF, (hex(pos), hex(WRAPPER_PATTERN_OFF))
    struct.pack_into("<i", code, lea_pattern + 3, WRAPPER_PATTERN_OFF - (lea_pattern + 7))
    code[WRAPPER_PATTERN_OFF:WRAPPER_PATTERN_OFF + 8] = b"/_bridge"

    # Metadata records.
    wrapper_slot = FTAB_VA + WRAPPER_FUNCOFF
    assert data[wrapper_slot:wrapper_slot + 4] == struct.pack("<I", WRAPPER - TEXT)
    wrapper_npc = u32(data, wrapper_slot + 28)
    wrapper_record = bytearray(data[wrapper_slot:wrapper_slot + 44 + 4 * wrapper_npc + 4 * 7])
    struct.pack_into("<i", wrapper_record, 8, WRAPPER_ARGS)
    struct.pack_into("<I", wrapper_record, 44 + 4 * wrapper_npc, BOOT_MAP - GOFUNC)
    struct.pack_into("<I", wrapper_record, 44 + 4 * wrapper_npc + 8, 0xFFFFFFFF)

    boot_slot = FTAB_VA + BOOT_FUNCOFF
    assert data[boot_slot:boot_slot + 4] == struct.pack("<I", BOOT_ENTRY - TEXT)
    boot_npc = u32(data, boot_slot + 28)
    boot_record = bytearray(data[boot_slot:boot_slot + 44 + 4 * boot_npc + 4 * 7])
    struct.pack_into("<i", boot_record, 8, 0)
    struct.pack_into("<I", boot_record, 44 + 4 * boot_npc, BOOT_MAP - GOFUNC)
    struct.pack_into("<I", boot_record, 44 + 4 * boot_npc + 8, 0xFFFFFFFF)

    # Patch binary.
    patched = bytearray(data)
    patched[MAYBE_LAUNCH:MAYBE_LAUNCH + 5] = b"\x90" * 5
    assert patched[SETUP_CALL1:SETUP_CALL1 + 5] == SETUP_OLD
    assert patched[SETUP_CALL2:SETUP_CALL2 + 5] == SETUP2_OLD
    struct.pack_into("<i", patched, SETUP_CALL1 + 1, WRAPPER - (SETUP_CALL1 + 5))
    struct.pack_into("<i", patched, SETUP_CALL2 + 1, WRAPPER - (SETUP_CALL2 + 5))
    patched[WRAPPER:WRAPPER + WRAPPER_WRITE] = code
    patched[BOOT_ENTRY:BOOT_ENTRY + 0x25] = data[BOOT_ENTRY:BOOT_ENTRY + 0x25]
    patched[BOOT_CODE:BOOT_CODE + len(boot)] = boot
    patched[wrapper_slot:wrapper_slot + len(wrapper_record)] = wrapper_record
    patched[boot_slot:boot_slot + len(boot_record)] = boot_record
    # Empty args map for both donor functions.
    empty_map = struct.pack("<ii", 2, 3) + b"\x00\x00"
    patched[BOOT_MAP:BOOT_MAP + len(empty_map)] = empty_map

    # Append new module metadata and extend the last RW PT_LOAD.
    rw_file = len(data)
    assert rw_file == 0xD256100, hex(rw_file)
    patched += b"\x00" * RW_EXT
    def put_rw(va, blob):
        off = rw_file + (va - RW_VA)
        patched[off:off + len(blob)] = blob

    put_rw(PCHEADER, bytes(header))
    put_rw(FUNCNAMES, b"\x00bridge.handler\x00")
    put_rw(PCTAB, pctab)
    put_rw(PCLNTABLE, bytes(func))
    put_rw(FTAB, ftab)
    put_rw(FINDFUNCTAB, findfunctab)
    put_rw(GOFUNC_NEW, gofunc)
    put_rw(NEW_MD, bytes(new_md))
    if force_conflict:
        put_rw(TEST_CONFLICT, struct.pack("<Q", 1))
    if force_mock:
        put_rw(TEST_MODE, struct.pack("<Q", 1))

    # Pad after the RW extension and append the new RX segment payload.
    assert len(patched) == rw_file + RW_EXT, hex(len(patched))
    patched += b"\x00" * (RX_FILE_OFF - len(patched))
    assert len(patched) == RX_FILE_OFF, hex(len(patched))
    patched += handler
    assert len(patched) == RX_FILE_OFF + RX_SIZE, hex(len(patched))

    # Program headers: extend the last RW PT_LOAD and convert the second
    # PT_NOTE into a new RX PT_LOAD for the handler segment.
    phoff = struct.unpack_from("<Q", patched, 0x20)[0]
    phentsize = struct.unpack_from("<H", patched, 0x36)[0]
    phnum = struct.unpack_from("<H", patched, 0x38)[0]
    found_rw = False
    found_rx = False
    for i in range(phnum):
        off = phoff + i * phentsize
        p_type = struct.unpack_from("<I", patched, off)[0]
        p_offset = struct.unpack_from("<Q", patched, off + 8)[0]
        if p_type == 1 and p_offset == 0xD251000:
            old_filesz = struct.unpack_from("<Q", patched, off + 32)[0]
            old_memsz = struct.unpack_from("<Q", patched, off + 40)[0]
            assert old_filesz == 0x5100 and old_memsz == 0x5100, (hex(old_filesz), hex(old_memsz))
            struct.pack_into("<Q", patched, off + 32, 0x5100 + RW_EXT)
            struct.pack_into("<Q", patched, off + 40, 0x5100 + RW_EXT)
            found_rw = True
        elif p_type == 4 and p_offset == RX_PH_OLD_OFFSET:
            old_vaddr = struct.unpack_from("<Q", patched, off + 16)[0]
            old_filesz = struct.unpack_from("<Q", patched, off + 32)[0]
            old_flags = struct.unpack_from("<I", patched, off + 4)[0]
            assert old_vaddr == 0x100550E0, hex(old_vaddr)
            assert old_filesz == 0x20, hex(old_filesz)
            assert old_flags == 4, old_flags
            struct.pack_into("<I", patched, off + 0, 1)            # PT_LOAD
            struct.pack_into("<I", patched, off + 4, RX_FLAGS)     # R+X
            struct.pack_into("<Q", patched, off + 8, RX_FILE_OFF)
            struct.pack_into("<Q", patched, off + 16, RX_VADDR)
            struct.pack_into("<Q", patched, off + 24, RX_VADDR)
            struct.pack_into("<Q", patched, off + 32, RX_SIZE)
            struct.pack_into("<Q", patched, off + 40, RX_SIZE)
            struct.pack_into("<Q", patched, off + 48, RX_ALIGN)
            found_rx = True
    assert found_rw
    assert found_rx

    out.write_bytes(patched)
    plan = {
        "output": str(out),
        "sha256": hashlib.sha256(patched).hexdigest(),
        "handler": hex(CODE_VA),
        "handler_code_size": hex(code_size),
        "handler_real_size": hex(len(handler_code)),
        "handler_segment": {
            "file_offset": hex(RX_FILE_OFF),
            "vaddr": hex(RX_VADDR),
            "size": hex(RX_SIZE),
            "flags": RX_FLAGS,
            "converted_from_pt_note_offset": hex(RX_PH_OLD_OFFSET),
        },
        "bootstrap": hex(BOOT_CODE),
        "wrapper": hex(WRAPPER),
        "new_module": hex(NEW_MD),
        "pcsp": hex(PCSP),
        "nbit": NBIT,
        "pointer_slots": len(POINTER_OFFSETS),
        "pointer_offsets": [hex(x) for x in POINTER_OFFSETS],
        "body_limit": hex(BODY_LIMIT),
        "force_conflict": force_conflict,
        "force_mock_stream": force_mock,
        "server_global": hex(SERVER_GLOBAL),
        "bridge_state": hex(BRIDGE_STATE),
        "init_count": hex(INIT_COUNT),
        "second_run": hex(SECOND_RUN),
        "test_conflict": hex(TEST_CONFLICT),
        "test_mode": hex(TEST_MODE),
        "resp_itab": hex(RESP_ITAB_NEW),
        "stream_itab": hex(STREAM_ITAB_NEW),
        "mock_resp": hex(MOCK_RESP),
        "mock_state": hex(MOCK_STATE),
        "seq_counter": hex(SEQ_COUNTER),
        "msg_itab": hex(MSG_ITAB_NEW),
        "req_type": hex(REQ_TYPE),
        "ptr_req_type": hex(PTR_REQ_TYPE),
        "pctab": hex(PCTAB),
        "pclntable": hex(PCLNTABLE),
        "ftab": hex(FTAB),
        "findfunctab": hex(FINDFUNCTAB),
        "findfunctab_buckets": n_buckets,
        "gofunc": hex(GOFUNC_NEW),
        "rw_ext": hex(RW_EXT),
    }
    print(json.dumps(plan, indent=2))
    (Path(__file__).resolve().parent / "full-plan.json").write_text(
        json.dumps(plan, indent=2) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    raise SystemExit(
        "app.bridge.emitter is the code generator used by app.bridge.patcher; "
        "run `python /app/app/bridge/patcher.py --source ... --dest ...` instead"
    )
