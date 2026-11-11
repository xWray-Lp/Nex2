"""
Nex Bytecode — opcode seti, modül modeli, encode/decode, disassembler, verifier.

Sözleşme: NEX_BYTECODE_SPEC.md (v0.1). Tasarım: NEX_VM_DESIGN.md.

Faz 1 hedefi: semantik doğrulama. Bu modül host'tan bağımsızdır; Python'a
özgü hiçbir şey bytecode'a sızmaz. VM (vm.py) ve derleyici (compiler.py)
bu sözleşmeyi kullanır.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Optional


# ─── Hata türleri ─────────────────────────────────────────────────────────────

class BytecodeError(Exception):
    """Encode/decode seviyesinde bozuk modül."""


class VerifyError(BytecodeError):
    """Verifier ihlali; program çalıştırılmaz."""


# ─── Opcode tablosu ──────────────────────────────────────────────────────────
# name -> (code, operand_count). v0.1: tüm operandlar u32.

OPCODES: dict[str, tuple[int, int]] = {
    # yığın / slot
    "NOP":             (0x00, 0),
    "CONST":           (0x01, 1),
    "LOAD_LOCAL":      (0x02, 1),
    "STORE_LOCAL":     (0x03, 1),
    "LOAD_GLOBAL":     (0x04, 1),
    "STORE_GLOBAL":    (0x05, 1),
    "POP":             (0x06, 0),
    "DUP":             (0x07, 0),
    "SWAP":            (0x08, 0),
    # aritmetik / karşılaştırma
    "ADD":             (0x10, 0),
    "SUB":             (0x11, 0),
    "MUL":             (0x12, 0),
    "DIV":             (0x13, 0),
    "MOD":             (0x14, 0),
    "NEG":             (0x15, 0),
    "EQ":              (0x18, 0),
    "NE":              (0x19, 0),
    "LT":              (0x1A, 0),
    "LE":              (0x1B, 0),
    "GT":              (0x1C, 0),
    "GE":              (0x1D, 0),
    "NOT":             (0x1E, 0),
    # kontrol
    "JUMP":            (0x20, 1),
    "JUMP_IF_FALSE":   (0x21, 1),
    "JUMP_IF_TRUE":    (0x22, 1),
    "TRAP":            (0x23, 1),
    # çağrı
    "CALL":            (0x28, 2),
    "CALL_NATIVE":     (0x29, 2),
    "CALL_PURE_NATIVE": (0x2A, 2),
    "RET":             (0x2B, 0),
    # nesne
    "NEW_LIST":        (0x30, 1),
    "NEW_DICT":        (0x31, 1),
    "NEW_TUPLE":       (0x32, 1),
    "GET_INDEX":       (0x33, 0),
    "SET_INDEX":       (0x34, 0),
    "GET_MEMBER":      (0x35, 1),
    "SET_MEMBER":      (0x36, 1),
    # mod
    "OWN_ALLOC":       (0x40, 1),
    "OWN_MOVE":        (0x41, 1),
    "OWN_FREE":        (0x42, 1),
    "STACK_ALLOC":     (0x43, 3),
    "STACK_MATERIALIZE": (0x44, 1),
    "RAW_MAKE":        (0x45, 1),
    # borrow
    "BORROW_SHARED":   (0x50, 2),
    "BORROW_MUT":      (0x51, 2),
    "BORROW_END":      (0x52, 1),
    "BORROW_CHECK":    (0x53, 1),
    # kapsam
    "BLOCK_ENTER":     (0x58, 0),
    "BLOCK_EXIT":      (0x59, 0),
}

OP_BY_CODE: dict[int, tuple[str, int]] = {code: (name, n) for name, (code, n) in OPCODES.items()}

# Slot modları
MODE_GC, MODE_OWN, MODE_STACK, MODE_RAW, MODE_BORROW, MODE_TEMP = range(6)

# Native bayrakları
NAT_RETAINS_REF = 1 << 0
NAT_PURE        = 1 << 1
NAT_STRUCTURAL  = 1 << 2

MAGIC = b"NEXB"
VERSION = 1
HEADER_SIZE = 52
FUNC_INFO_SIZE = 40
NATIVE_INFO_SIZE = 12

# Sabit havuzu etiketleri
TAG_INT, TAG_FLOAT, TAG_STR = 0x01, 0x02, 0x03
TAG_FUNC_REF, TAG_NATIVE_REF, TAG_TYPE = 0x04, 0x05, 0x06
TAG_NAME, TAG_BOOL, TAG_NULL = 0x07, 0x08, 0x09

# TYPE sabiti kind kodları (spec §3)
TYPE_KIND_INT, TYPE_KIND_FLOAT, TYPE_KIND_BYTE = 0x01, 0x02, 0x03
TYPE_KIND_BOOL, TYPE_KIND_PTR = 0x04, 0x05
TYPE_KIND_NAMES = {
    TYPE_KIND_INT: "int",
    TYPE_KIND_FLOAT: "float",
    TYPE_KIND_BYTE: "byte",
    TYPE_KIND_BOOL: "bool",
    TYPE_KIND_PTR: "ptr",
}
TYPE_KIND_BY_NAME = {v: k for k, v in TYPE_KIND_NAMES.items()}

#: Prototip sınırı: bozuk/elle yazılmış bytecode'un STACK_ALLOC ile host
#: belleğini tüketmesini engeller (tree-walker kaynakta aynı sınıra tabi
#: değil; güvenilmeyen bytecode için adım bütçesiyle birlikte Faz 2 işi).
MAX_STACK_ELEMS = 1_000_000


# ─── Modül modeli ────────────────────────────────────────────────────────────

@dataclass
class NativeInfo:
    name:  str
    argc:  int          # 0xFFFFFFFF = variadic
    flags: int = 0

    @property
    def retains_ref(self) -> bool:
        return bool(self.flags & NAT_RETAINS_REF)

    @property
    def pure(self) -> bool:
        return bool(self.flags & NAT_PURE)


@dataclass
class FunctionCode:
    name:       str
    arity:      int
    n_locals:   int
    max_stack:  int
    code:       bytes
    slot_modes: bytes = b""
    line_table: list[tuple[int, int, int]] = field(default_factory=list)

    def line_of(self, pc: int) -> tuple[int, int]:
        """pc için en yakın (line, col); yoksa (0, 0)."""
        best = (0, 0)
        for off, line, col in self.line_table:
            if off > pc:
                break
            best = (line, col)
        return best


@dataclass
class Module:
    consts:    list[tuple[int, object]] = field(default_factory=list)
    functions: list[FunctionCode] = field(default_factory=list)
    natives:   list[NativeInfo] = field(default_factory=list)
    entry:     Optional[int] = None

    def const_value(self, idx: int) -> object:
        if idx < 0 or idx >= len(self.consts):
            raise BytecodeError(f"constant index out of range: {idx}")
        return self.consts[idx][1]

    def function_name(self, idx: int) -> str:
        if idx < 0 or idx >= len(self.functions):
            raise BytecodeError(f"function index out of range: {idx}")
        return self.functions[idx].name

    def native_name(self, idx: int) -> str:
        if idx < 0 or idx >= len(self.natives):
            raise BytecodeError(f"native index out of range: {idx}")
        return self.natives[idx].name


# ─── Yardımcı: sabit kodlama ─────────────────────────────────────────────────

def _enc_const(tag: int, value: object) -> bytes:
    if tag == TAG_INT:
        return bytes([tag]) + struct.pack("<q", int(value))
    if tag == TAG_FLOAT:
        return bytes([tag]) + struct.pack("<d", float(value))
    if tag in (TAG_STR, TAG_NAME):
        data = str(value).encode("utf-8")
        return bytes([tag]) + struct.pack("<I", len(data)) + data
    if tag in (TAG_FUNC_REF, TAG_NATIVE_REF):
        return bytes([tag]) + struct.pack("<I", int(value))
    if tag == TAG_TYPE:
        kind, size = value  # type: ignore[misc]
        return bytes([tag]) + bytes([int(kind)]) + struct.pack("<I", int(size))
    if tag == TAG_BOOL:
        return bytes([tag, 1 if value else 0])
    if tag == TAG_NULL:
        return bytes([tag])
    raise BytecodeError(f"unknown constant tag: {tag}")


def _dec_const(data: bytes, pos: int) -> tuple[tuple[int, object], int]:
    if pos >= len(data):
        raise BytecodeError("truncated constant pool")
    tag = data[pos]
    pos += 1

    def need(n: int) -> None:
        if pos + n > len(data):
            raise BytecodeError("truncated constant payload")

    if tag == TAG_INT:
        need(8)
        (v,) = struct.unpack_from("<q", data, pos)
        return (tag, v), pos + 8
    if tag == TAG_FLOAT:
        need(8)
        (v,) = struct.unpack_from("<d", data, pos)
        return (tag, v), pos + 8
    if tag in (TAG_STR, TAG_NAME):
        need(4)
        (n,) = struct.unpack_from("<I", data, pos)
        pos += 4
        need(n)
        raw = data[pos:pos + n]
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as e:
            raise BytecodeError(f"invalid UTF-8 in string constant: {e}")
        return (tag, text), pos + n
    if tag in (TAG_FUNC_REF, TAG_NATIVE_REF):
        need(4)
        (v,) = struct.unpack_from("<I", data, pos)
        return (tag, v), pos + 4
    if tag == TAG_TYPE:
        need(5)
        kind = data[pos]
        (size,) = struct.unpack_from("<I", data, pos + 1)
        return (tag, (kind, size)), pos + 5
    if tag == TAG_BOOL:
        need(1)
        return (tag, bool(data[pos])), pos + 1
    if tag == TAG_NULL:
        return (tag, None), pos
    raise BytecodeError(f"unknown constant tag: {tag}")


def _enc_line_table(entries: list[tuple[int, int, int]]) -> bytes:
    out = [struct.pack("<I", len(entries))]
    for pc, line, col in entries:
        out.append(struct.pack("<III", pc, line, col))
    return b"".join(out)


def _dec_line_table(data: bytes, off: int, length: int) -> list[tuple[int, int, int]]:
    if length == 0:
        return []
    if length < 4 or off + length > len(data):
        raise BytecodeError("line table exceeds file size")
    end = off + length
    (count,) = struct.unpack_from("<I", data, off)
    pos = off + 4
    out = []
    for _ in range(count):
        if pos + 12 > end:
            raise BytecodeError("truncated line table")
        pc, line, col = struct.unpack_from("<III", data, pos)
        out.append((pc, line, col))
        pos += 12
    return out


# ─── Encode ──────────────────────────────────────────────────────────────────

def encode_module(m: Module) -> bytes:
    """Modülü spec'teki ikili biçime çevir."""
    const_bytes = b"".join(_enc_const(tag, val) for tag, val in m.consts)

    func_table_size = FUNC_INFO_SIZE * len(m.functions)
    native_table_size = NATIVE_INFO_SIZE * len(m.natives)

    slotmodes_off = HEADER_SIZE + len(const_bytes) + func_table_size + native_table_size
    code_off = slotmodes_off + sum(len(f.slot_modes) for f in m.functions)
    debug_off = code_off + sum(len(f.code) for f in m.functions)

    # Debug (line) tabloları
    debug_chunks: list[bytes] = []
    line_meta: list[tuple[int, int]] = []
    cursor = debug_off
    for f in m.functions:
        chunk = _enc_line_table(f.line_table)
        line_meta.append((cursor, len(chunk)))
        debug_chunks.append(chunk)
        cursor += len(chunk)
    debug_bytes = b"".join(debug_chunks)

    def const_index(value: object) -> int:
        for i, (_, v) in enumerate(m.consts):
            if v == value:
                return i
        raise BytecodeError(f"name/constant missing from pool: {value!r}")

    # Fonksiyon tablosu
    func_infos: list[bytes] = []
    code_cursor = code_off
    sm_cursor = slotmodes_off
    for i, f in enumerate(m.functions):
        if len(f.slot_modes) != f.n_locals:
            raise BytecodeError(
                f"function '{f.name}': slot_modes length != n_locals")
        line_off, line_len = line_meta[i]
        func_infos.append(struct.pack(
            "<10I",
            const_index(f.name), f.arity, f.n_locals, f.max_stack,
            code_cursor, len(f.code),
            sm_cursor, line_off, line_len, 0,
        ))
        code_cursor += len(f.code)
        sm_cursor += len(f.slot_modes)

    native_infos = [
        struct.pack("<3I", const_index(n.name), n.argc, n.flags)
        for n in m.natives
    ]

    entry = 0xFFFFFFFF if m.entry is None else m.entry
    header = (
        struct.pack("<4sHH", MAGIC, VERSION, 0)
        + struct.pack(
            "<11I",
            entry,
            len(m.consts), HEADER_SIZE,
            len(m.functions), HEADER_SIZE + len(const_bytes),
            len(m.natives),
            HEADER_SIZE + len(const_bytes) + func_table_size,
            sum(len(f.code) for f in m.functions), code_off,
            len(debug_bytes), debug_off,
        )
    )
    assert len(header) == HEADER_SIZE

    slotmodes_bytes = b"".join(f.slot_modes for f in m.functions)
    code_bytes = b"".join(f.code for f in m.functions)
    return b"".join([
        header, const_bytes, b"".join(func_infos), b"".join(native_infos),
        slotmodes_bytes, code_bytes, debug_bytes,
    ])


# ─── Decode ──────────────────────────────────────────────────────────────────

def decode_module(data: bytes) -> Module:
    if len(data) < HEADER_SIZE:
        raise BytecodeError("file too short for header")
    magic, version, flags = struct.unpack_from("<4sHH", data, 0)
    if magic != MAGIC:
        raise BytecodeError("bad magic (not a Nex bytecode module)")
    if version != VERSION:
        raise BytecodeError(f"unsupported bytecode version: {version}")
    if flags != 0:
        raise BytecodeError(f"unsupported header flags: {flags}")

    (entry, const_count, const_off,
     func_count, func_off,
     native_count, native_off,
     code_len, code_off,
     debug_len, debug_off) = struct.unpack_from("<11I", data, 8)

    if const_off < HEADER_SIZE:
        raise BytecodeError("constant pool offset overlaps header")
    if const_off > len(data):
        raise BytecodeError("constant pool offset exceeds file size")
    if func_off + func_count * FUNC_INFO_SIZE > len(data):
        raise BytecodeError("function table exceeds file size")
    if native_off + native_count * NATIVE_INFO_SIZE > len(data):
        raise BytecodeError("native table exceeds file size")
    if code_off + code_len > len(data):
        raise BytecodeError("code section exceeds file size")
    if debug_off + debug_len > len(data):
        raise BytecodeError("debug section exceeds file size")

    m = Module(entry=None if entry == 0xFFFFFFFF else entry)

    pos = const_off
    for _ in range(const_count):
        entry_c, pos = _dec_const(data, pos)
        m.consts.append(entry_c)

    def name_of(idx: int) -> str:
        if idx >= len(m.consts):
            raise BytecodeError(f"function/native name index out of range: {idx}")
        return str(m.consts[idx][1])

    pos = func_off
    for _ in range(func_count):
        if pos + FUNC_INFO_SIZE > len(data):
            raise BytecodeError("truncated function table")
        (name_idx, arity, n_locals, max_stack, f_code_off, f_code_len,
         sm_off, line_off, line_len, _flags) = struct.unpack_from("<10I", data, pos)
        pos += FUNC_INFO_SIZE
        code = data[f_code_off:f_code_off + f_code_len]
        if len(code) != f_code_len:
            raise BytecodeError("truncated function code")
        slot_modes = data[sm_off:sm_off + n_locals]
        if len(slot_modes) != n_locals:
            raise BytecodeError("truncated slot mode table")
        line_table = _dec_line_table(data, line_off, line_len)
        m.functions.append(FunctionCode(
            name=name_of(name_idx), arity=arity, n_locals=n_locals,
            max_stack=max_stack, code=code, slot_modes=slot_modes,
            line_table=line_table,
        ))

    pos = native_off
    for _ in range(native_count):
        if pos + NATIVE_INFO_SIZE > len(data):
            raise BytecodeError("truncated native table")
        name_idx, argc, nflags = struct.unpack_from("<3I", data, pos)
        pos += NATIVE_INFO_SIZE
        m.natives.append(NativeInfo(name=name_of(name_idx), argc=argc,
                                    flags=nflags))

    if code_off + code_len > len(data) or debug_off + debug_len > len(data):
        raise BytecodeError("section offsets exceed file size")

    if m.entry is not None and m.entry >= len(m.functions):
        raise BytecodeError("entry function index out of range")

    return m


# ─── Instruction decode ──────────────────────────────────────────────────────

@dataclass
class Instr:
    pc:   int
    name: str
    args: tuple[int, ...]

    @property
    def width(self) -> int:
        return 1 + 4 * len(self.args)


def decode_code(code: bytes) -> list[Instr]:
    out: list[Instr] = []
    pos = 0
    while pos < len(code):
        op = code[pos]
        if op not in OP_BY_CODE:
            raise BytecodeError(f"unknown opcode 0x{op:02X} at pc {pos}")
        name, nargs = OP_BY_CODE[op]
        need = pos + 1 + 4 * nargs
        if need > len(code):
            raise BytecodeError(f"truncated instruction at pc {pos}")
        args = struct.unpack_from("<" + "I" * nargs, code, pos + 1) if nargs else ()
        out.append(Instr(pos, name, args))
        pos = need
    return out


def stack_effect(name: str, args: tuple[int, ...]) -> tuple[int, int]:
    """(pops, pushes)."""
    if name in ("CONST", "LOAD_LOCAL", "LOAD_GLOBAL", "OWN_MOVE",
                "STACK_MATERIALIZE"):
        return (0, 1)
    if name in ("STORE_LOCAL", "STORE_GLOBAL", "POP", "RET",
                "JUMP_IF_FALSE", "JUMP_IF_TRUE"):
        return (1, 0)
    if name == "DUP":
        return (1, 2)
    if name == "SWAP":
        return (2, 2)
    if name in ("ADD", "SUB", "MUL", "DIV", "MOD",
                "EQ", "NE", "LT", "LE", "GT", "GE"):
        return (2, 1)
    if name in ("NEG", "NOT"):
        return (1, 1)
    if name in ("CALL", "CALL_NATIVE", "CALL_PURE_NATIVE"):
        return (args[1], 1)
    if name in ("NEW_LIST", "NEW_TUPLE"):
        return (args[0], 1)
    if name == "NEW_DICT":
        return (2 * args[0], 1)
    if name == "GET_INDEX":
        return (2, 1)
    if name == "SET_INDEX":
        return (3, 0)
    if name == "GET_MEMBER":
        return (1, 1)
    if name == "SET_MEMBER":
        return (2, 0)
    if name in ("OWN_ALLOC", "RAW_MAKE"):
        return (1, 0)
    if name in ("NOP", "JUMP", "TRAP", "OWN_FREE", "STACK_ALLOC",
                "BORROW_SHARED", "BORROW_MUT", "BORROW_END", "BORROW_CHECK",
                "BLOCK_ENTER", "BLOCK_EXIT"):
        return (0, 0)
    raise BytecodeError(f"no stack effect for opcode {name}")


# ─── Verifier ────────────────────────────────────────────────────────────────

def verify_module(m: Module) -> None:
    """Modülü doğrula; ihlalde VerifyError fırlat."""
    errors: list[str] = []
    if m.entry is not None and not (0 <= m.entry < len(m.functions)):
        errors.append("entry function index out of range")
    for i, (tag, val) in enumerate(m.consts):
        if tag == TAG_FUNC_REF and not (0 <= int(val) < len(m.functions)):
            errors.append(f"const {i}: FUNC_REF index {val} out of range")
        if tag == TAG_NATIVE_REF and not (0 <= int(val) < len(m.natives)):
            errors.append(f"const {i}: NATIVE_REF index {val} out of range")
    for f in m.functions:
        errors.extend(f"fn {f.name}: {e}" for e in _verify_function(m, f))
    if errors:
        raise VerifyError("\n".join(errors))


def _verify_function(m: Module, f: FunctionCode) -> list[str]:
    errs: list[str] = []
    try:
        instrs = decode_code(f.code)
    except BytecodeError as e:
        return [str(e)]

    if not instrs:
        return ["empty function code"]

    by_pc = {i.pc: i for i in instrs}
    starts = set(by_pc)

    if len(f.slot_modes) != f.n_locals:
        errs.append("slot mode table size mismatch")
    if f.n_locals < f.arity:
        errs.append("n_locals is smaller than arity")
    # Parametre slotları argüman bağlamayla kurulur; STACK modu orada
    # anlamsızdır ve callee çıkışında çağıranın canlı tamponunu öldürürdü.
    for i in range(min(f.arity, len(f.slot_modes))):
        if f.slot_modes[i] == MODE_STACK:
            errs.append(f"parameter slot {i} cannot be STACK")
    if any(m > MODE_TEMP for m in f.slot_modes):
        errs.append("invalid slot mode value")
    if f.max_stack < 1:
        errs.append("max_stack must be >= 1")
    prev_pc = -1
    for pc, _line, _col in f.line_table:
        if pc <= prev_pc:
            errs.append("line table pc entries are not strictly increasing")
        prev_pc = pc
        if pc not in starts:
            errs.append(f"line table pc {pc} is not an instruction boundary")

    # Yapısal kontroller
    for ins in instrs:
        a = ins.args
        if ins.name in ("JUMP", "JUMP_IF_FALSE", "JUMP_IF_TRUE"):
            if a[0] not in starts:
                errs.append(f"jump target {a[0]} is not an instruction boundary")
        elif ins.name in ("CONST", "TRAP"):
            if a[0] >= len(m.consts):
                errs.append(f"constant index {a[0]} out of range")
        elif ins.name in ("LOAD_LOCAL", "STORE_LOCAL"):
            if a[0] >= f.n_locals:
                errs.append(f"local slot {a[0]} out of range")
            elif (ins.name == "STORE_LOCAL" and a[0] < len(f.slot_modes)
                  and f.slot_modes[a[0]] == MODE_STACK):
                # STACK slotları yalnız STACK_ALLOC ile kurulur; ham atama
                # stack değerinin ömrünü/kaçış kuralını bozar.
                errs.append(f"STORE_LOCAL into STACK slot {a[0]}")
            elif (a[0] < len(f.slot_modes)
                  and f.slot_modes[a[0]] == MODE_OWN):
                # OWN slotları okunamaz/yazılamaz: tek kuruluş yolu
                # OWN_ALLOC, tek tüketim yolu OWN_MOVE/OWN_FREE. LOAD/STORE
                # wrapper'ı kopyalar veya sahipliği sessizce kaybettirirdi.
                errs.append(
                    f"{ins.name} on OWN slot {a[0]} "
                    f"(OWN values are not readable/writable directly)")
        elif ins.name == "CALL":
            if a[0] >= len(m.functions):
                errs.append(f"function index {a[0]} out of range")
            elif a[1] != m.functions[a[0]].arity:
                errs.append(
                    f"call argc {a[1]} != arity {m.functions[a[0]].arity}")
        elif ins.name in ("CALL_NATIVE", "CALL_PURE_NATIVE"):
            if a[0] >= len(m.natives):
                errs.append(f"native index {a[0]} out of range")
            else:
                nat = m.natives[a[0]]
                if nat.argc != 0xFFFFFFFF and a[1] != nat.argc:
                    errs.append(
                        f"native '{nat.name}' argc {a[1]} != {nat.argc}")
                if ins.name == "CALL_PURE_NATIVE" and not (
                        nat.pure and not nat.retains_ref):
                    errs.append(
                        f"native '{nat.name}' is not pure/non-retaining")
        # Slot modu kontrolleri (v1: yalnız mod isteyen opcode'lar).
        # slot_modes kısa olabilir (bozuk modül); indekslemeden önce boyut
        # denetlenir, aksi hâlde verifier IndexError ile çökerdi.
        def slot_mode(si: int):
            return f.slot_modes[si] if si < len(f.slot_modes) else None

        if ins.name in ("OWN_ALLOC", "OWN_MOVE", "OWN_FREE"):
            if a[0] >= f.n_locals:
                errs.append(f"{ins.name} local slot {a[0]} out of range")
            elif slot_mode(a[0]) not in (None, MODE_OWN):
                errs.append(f"{ins.name} on non-OWN slot {a[0]}")
        if ins.name == "RAW_MAKE":
            if a[0] >= f.n_locals:
                errs.append(f"RAW_MAKE local slot {a[0]} out of range")
            elif slot_mode(a[0]) not in (None, MODE_RAW):
                errs.append(f"RAW_MAKE on non-RAW slot {a[0]}")
        if ins.name == "STACK_MATERIALIZE":
            if a[0] >= f.n_locals:
                errs.append(
                    f"STACK_MATERIALIZE local slot {a[0]} out of range")
            elif slot_mode(a[0]) not in (None, MODE_STACK):
                errs.append(
                    f"STACK_MATERIALIZE on non-STACK slot {a[0]}")
        if ins.name == "STACK_ALLOC":
            si, ti, n = a
            if si >= f.n_locals:
                errs.append(f"STACK_ALLOC local slot {si} out of range")
            elif slot_mode(si) not in (None, MODE_STACK):
                errs.append(f"STACK_ALLOC on non-STACK slot {si}")
            if ti >= len(m.consts):
                errs.append(
                    f"STACK_ALLOC type const index {ti} out of range")
            elif m.consts[ti][0] != TAG_TYPE:
                errs.append(
                    f"STACK_ALLOC const {ti} is not a TYPE constant")
            else:
                payload = m.consts[ti][1]
                if (not isinstance(payload, tuple) or len(payload) != 2
                        or not all(isinstance(x, int) for x in payload)):
                    errs.append(
                        f"STACK_ALLOC const {ti} has malformed TYPE payload")
                else:
                    kind, size = payload
                    if kind not in TYPE_KIND_NAMES:
                        errs.append(f"STACK_ALLOC type kind {kind} is invalid")
                    if n < 1:
                        errs.append("STACK_ALLOC element count must be >= 1")
                    elif n > MAX_STACK_ELEMS:
                        errs.append(
                            f"STACK_ALLOC element count {n} exceeds "
                            f"limit {MAX_STACK_ELEMS}")
                    elif size != n:
                        errs.append(
                            f"STACK_ALLOC element count {n} != type size {size}")
        if ins.name in ("BORROW_SHARED", "BORROW_MUT"):
            dst, src = a[0], a[1]
            if dst >= f.n_locals or src >= f.n_locals:
                errs.append(f"{ins.name} local slot out of range "
                            f"(dst={dst}, src={src})")
            else:
                if slot_mode(src) not in (None, MODE_OWN):
                    errs.append(f"{ins.name} src slot {src} is not OWN")
                if slot_mode(dst) not in (None, MODE_BORROW):
                    errs.append(f"{ins.name} dst slot {dst} is not BORROW")
        if ins.name in ("BORROW_END", "BORROW_CHECK"):
            if a[0] >= f.n_locals:
                errs.append(f"{ins.name} local slot {a[0]} out of range")
            elif slot_mode(a[0]) not in (None, MODE_BORROW):
                errs.append(f"{ins.name} on non-BORROW slot {a[0]}")

    # Akış + yığın derinliği
    depth: dict[int, int] = {instrs[0].pc: 0}
    work = [instrs[0].pc]
    while work:
        pc = work.pop()
        ins = by_pc[pc]
        d = depth[pc]
        pops, pushes = stack_effect(ins.name, ins.args)
        if d < pops:
            errs.append(f"stack underflow at pc {pc} ({ins.name})")
            continue
        nd = d - pops + pushes
        if nd > f.max_stack:
            errs.append(
                f"stack overflow at pc {pc} ({ins.name}): {nd} > {f.max_stack}")
        if ins.name == "RET":
            if nd != 0:
                errs.append(f"stack not empty at RET (pc {pc}): {nd}")
            continue
        next_pc = pc + ins.width
        succ: list[int] = []
        if ins.name in ("JUMP", "JUMP_IF_FALSE", "JUMP_IF_TRUE"):
            succ.append(ins.args[0])
        if ins.name not in ("JUMP",):
            if next_pc >= len(f.code):
                errs.append(f"fallthrough past end of code at pc {pc} ({ins.name})")
            else:
                succ.append(next_pc)
        for s in succ:
            if s not in starts:
                continue
            if s in depth:
                if depth[s] != nd:
                    errs.append(
                        f"inconsistent stack depth at pc {s}: "
                        f"{depth[s]} vs {nd}")
                continue
            depth[s] = nd
            work.append(s)

    last = instrs[-1]
    if last.name != "RET":
        errs.append(f"function does not end with RET (last: {last.name})")
    return errs


# ─── Disassembler ────────────────────────────────────────────────────────────

def _fmt_value(v: object) -> str:
    if isinstance(v, str):
        return repr(v)
    if v is None:
        return "null"
    return repr(v)


def disassemble(m: Module) -> str:
    """Deterministik metin disassembly (test/CLI için)."""
    lines: list[str] = []
    for fi, f in enumerate(m.functions):
        tag = " [entry]" if m.entry == fi else ""
        lines.append(
            f"; fn {f.name}{tag} arity={f.arity} locals={f.n_locals} "
            f"max_stack={f.max_stack}")
        for ins in decode_code(f.code):
            extra = ""
            if ins.name == "CONST":
                idx = ins.args[0]
                val = m.const_value(idx) if idx < len(m.consts) else "?"
                extra = f" ; {_fmt_value(val)}"
                args = f"K{idx}"
            elif ins.name in ("JUMP", "JUMP_IF_FALSE", "JUMP_IF_TRUE"):
                args = f"{ins.args[0]:04X}"
            elif ins.name == "CALL":
                fn = m.function_name(ins.args[0]) if ins.args[0] < len(m.functions) else "?"
                args = f"{ins.args[0]} argc={ins.args[1]} ; {fn}"
            elif ins.name in ("CALL_NATIVE", "CALL_PURE_NATIVE"):
                nat = m.natives[ins.args[0]].name if ins.args[0] < len(m.natives) else "?"
                args = f"{ins.args[0]} argc={ins.args[1]} ; {nat}"
            elif ins.name == "STACK_ALLOC":
                si, ti, n = ins.args
                args = f"{si} K{ti} n={n}"
                if ti < len(m.consts) and m.consts[ti][0] == TAG_TYPE:
                    payload = m.consts[ti][1]
                    if isinstance(payload, tuple) and len(payload) == 2:
                        kind, size = payload
                        extra = (f" ; {TYPE_KIND_NAMES.get(kind, '?')}"
                                 f" size={size}")
                    else:
                        extra = " ; malformed TYPE payload"
            elif ins.name == "STACK_MATERIALIZE":
                args = f"{ins.args[0]}"
                mode = (f.slot_modes[ins.args[0]]
                        if ins.args[0] < len(f.slot_modes) else "?")
                extra = f" ; stack slot {ins.args[0]} (mode {mode})"
            elif ins.args:
                args = " ".join(str(x) for x in ins.args)
            else:
                args = ""
            lines.append(f"{ins.pc:04X}  {ins.name:<18}{args}{extra}")
        if fi != len(m.functions) - 1:
            lines.append("")
    return "\n".join(lines)
