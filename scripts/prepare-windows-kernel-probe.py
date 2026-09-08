#!/usr/bin/env python3
"""Extract and rewrite five bounded leaf routines from Microsoft's NT kernel.

Prerequisites: pefile and iced-x86==1.21.0. Generated Microsoft code stays ignored.
This is a restricted experiment, not a general kernel translator or Windows boot.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct
import urllib.request

import iced_x86 as x
import pefile

SHA256 = 'f9215193d514541abdacec7c19e3f8f765894f7abb517b3845a39cf61ddb3066'
URL = 'https://msdl.microsoft.com/download/symbols/ntoskrnl.exe/4CA4F63D1450000/ntoskrnl.exe'
NAMES = ['RtlInitializeBitMap', 'RtlSetBit', 'RtlClearBit', 'RtlTestBit', 'RtlSplay']
CODE_BASE = 0x50000000
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--image', type=Path, default=Path('downloads/windows-native-research/ntoskrnl-26100.9278.exe'))
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args()
if not args.image.exists():
    args.image.parent.mkdir(parents=True, exist_ok=True)
    data = urllib.request.urlopen(URL, timeout=60).read()
    if hashlib.sha256(data).hexdigest() != SHA256:
        raise ValueError('Microsoft kernel download hash mismatch')
    args.image.write_bytes(data)
data = args.image.read_bytes()
if hashlib.sha256(data).hexdigest() != SHA256:
    raise ValueError('This experiment requires the pinned 26100.9278 x64 kernel')
pe = pefile.PE(data=data)
image = pe.get_memory_mapped_image()
base = pe.OPTIONAL_HEADER.ImageBase
exports = {s.name.decode(): s.address for s in pe.DIRECTORY_ENTRY_EXPORT.symbols if s.name}
formatter = x.Formatter(x.FormatterSyntax.INTEL)


def leaf_graph(rva):
    """Follow both sides of each direct branch; reject unsupported behavior."""
    found, pending, occupied = {}, [base + rva], {}
    while pending:
        ip = pending.pop()
        if ip in found:
            continue
        if not base + rva <= ip < base + rva + 8192 or len(found) >= 1024:
            raise ValueError('Leaf control flow escaped bounded function region')
        ins = x.Decoder(64, image[ip - base:ip - base + 15], ip=ip).decode()
        if ins.is_invalid or ins.is_privileged or ins.is_ip_rel_memory_operand:
            raise ValueError(f'Unsupported instruction at {ip:x}: {ins}')
        for n in range(ins.op_count):
            if ins.op_kind(n) == x.OpKind.REGISTER and ins.op_register(n) in (x.Register.RSP, x.Register.ESP, x.Register.SP):
                raise ValueError('Explicit guest stack operations need a separate implementation')
        if ins.segment_prefix != x.Register.NONE:
            raise ValueError('Existing guest segment base needs a separate implementation')
        if ins.memory_base in (x.Register.RSP, x.Register.ESP) or ins.memory_index in (x.Register.RSP, x.Register.ESP):
            raise ValueError('Guest stack-relative memory is outside this leaf probe')
        flow = ins.flow_control
        if flow not in (x.FlowControl.NEXT, x.FlowControl.CONDITIONAL_BRANCH,
                        x.FlowControl.UNCONDITIONAL_BRANCH, x.FlowControl.RETURN):
            raise ValueError(f'Unsupported control transfer: {ins}')
        if flow == x.FlowControl.RETURN and ins.mnemonic != x.Mnemonic.RET:
            raise ValueError('Only a normal near return to the harness is supported')
        for byte in range(ip, ins.next_ip):
            if byte in occupied:
                raise ValueError('Overlapping instruction streams are unsupported')
            occupied[byte] = ip
        found[ip] = ins
        if flow in (x.FlowControl.NEXT, x.FlowControl.CONDITIONAL_BRANCH):
            pending.append(ins.next_ip)
        if flow in (x.FlowControl.CONDITIONAL_BRANCH, x.FlowControl.UNCONDITIONAL_BRANCH):
            pending.append(ins.near_branch_target)
    return [found[ip] for ip in sorted(found)]


args.output.mkdir(parents=True, exist_ok=False)
blob = bytearray(11 * 4096)
blob[:16] = struct.pack('<QQ', 0x314b50544e, CODE_BASE)
manifest = {'source': URL, 'sha256': SHA256, 'version': '10.0.26100.9278',
            'code_base': hex(CODE_BASE), 'routines': []}
for index, name in enumerate(NAMES):
    instructions = leaf_graph(exports[name])
    record = {'name': name, 'rva': hex(exports[name]), 'instructions': len(instructions), 'variants': []}
    for shifted in (False, True):
        encoder = x.BlockEncoder(64)
        disassembly, changed = [], 0
        for original in instructions:
            ins = original.copy()
            if shifted and ins.mnemonic not in (x.Mnemonic.LEA, x.Mnemonic.NOP):
                kinds = [ins.op_kind(n) for n in range(ins.op_count)]
                # Implicit ES-based string accesses cannot be fixed with GS.
                if any(x.OpKind.MEMORY_SEG_SI <= kind < x.OpKind.MEMORY for kind in kinds):
                    raise ValueError(f'Implicit string memory requires dedicated lowering: {ins}')
                if x.OpKind.MEMORY in kinds:
                    ins.segment_prefix = x.Register.GS
                    changed += 1
            encoder.add(ins)
            disassembly.append(f'{original.ip:x}: {formatter.format(ins)}')
        slot = 1 + index * 2 + int(shifted)
        code = encoder.encode(CODE_BASE + slot * 4096)
        if len(code) > 4096:
            raise ValueError('Function does not fit reserved code page')
        blob[slot * 4096:slot * 4096 + len(code)] = code
        record['variants'].append({'shifted': shifted, 'offset': slot * 4096,
                                   'bytes': len(code), 'memory_prefixes': changed})
        (args.output / f'{name}-{int(shifted)}.txt').write_text('\n'.join(disassembly) + '\n')
    manifest['routines'].append(record)
(args.output / 'kernel-leaves.bin').write_bytes(blob)
manifest['blob_sha256'] = hashlib.sha256(blob).hexdigest()
(args.output / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
print(json.dumps(manifest, indent=2))
