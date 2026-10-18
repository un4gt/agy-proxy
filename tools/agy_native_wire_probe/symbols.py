"""Resolve Go 1.20+ function PCs from the exact ELF under test."""
import hashlib
import json
import os
import struct
from pathlib import Path

binary = Path(os.environ.get('AGY_WIRE_BINARY','/root/.local/bin/agy')).read_bytes()
assert binary[:5] == b'\x7fELF\x02'
section_offset = struct.unpack_from('<Q', binary, 40)[0]
section_size, section_count, names_index = struct.unpack_from('<HHH', binary, 58)
sections = [struct.unpack_from('<IIQQQQIIQQ', binary, section_offset+i*section_size) for i in range(section_count)]
names = sections[names_index]
names_blob = binary[names[4]:names[4]+names[5]]
def cstring(blob, pos):
    return blob[pos:blob.index(b'\0', pos)].decode()
offset = binary.index(b'\xf1\xff\xff\xff\x00\x00\x01\x08')
blob = binary[offset:]
assert struct.unpack_from('<I', blob)[0] == 0xfffffff1
nfunc, nfiles, text, funcnames, cutab, filetab, pctab, pcln = struct.unpack_from('<8Q', blob,8)
if not text:
    owner = next(s for s in sections if s[4] <= offset < s[4]+s[5] and s[1] != 8)
    text_slot = owner[3] + offset - owner[4] + 24
    for s in sections:
        if s[1] == 4:
            for pos in range(s[4], s[4]+s[5], s[9]):
                address, info, addend = struct.unpack_from('<QQq',binary,pos)
                if address == text_slot:
                    assert info & 0xffffffff == 8
                    text = addend
                    break
if not text:
    relocs = {struct.unpack_from('<Q',binary,p)[0]:struct.unpack_from('<q',binary,p+16)[0]
              for s in sections if s[1]==4 for p in range(s[4],s[4]+s[5],s[9])}
    for addr, addend in relocs.items():
        if addend == text_slot-24:
            text = relocs.get(addr+176,0)
            if text:
                break
assert text
targets = {'crypto/tls.(*Conn).Write', 'crypto/tls.(*Conn).Read', 'net/http.(*Transport).roundTrip'}
targets.update({'encoding/json/v2.Marshal', 'google3/third_party/golang/protobuf/v2/encoding/protojson/protojson.MarshalOptions.marshal'})
found = {}
for i in range(nfunc):
    entry, offset = struct.unpack_from('<II',blob,pcln+i*8)
    nameoff = struct.unpack_from('<i',blob,pcln+offset+4)[0]
    name = cstring(blob,funcnames+nameoff)
    if name in targets:
        end = struct.unpack_from('<I',blob,pcln+(i+1)*8)[0]
        found[name] = {'start':text+entry,'end':text+end}
result = {'binary_sha256':hashlib.sha256(binary).hexdigest(),'elf_type':struct.unpack_from('<H',binary,16)[0],'functions':found}
Path(os.environ.get('AGY_WIRE_SYMBOLS','/evidence/symbols.json')).write_text(json.dumps(result,indent=2))
print(json.dumps(result,indent=2))
