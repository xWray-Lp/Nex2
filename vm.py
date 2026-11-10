"""
Nex VM — stack tabanlı yürütücü (prototip alt kümesi).

Bytecode sözleşmesi: NEX_BYTECODE_SPEC.md. Bu VM modsuz alt kümenin yanında
`@stack` (STACK_ALLOC/STACK_MATERIALIZE, index erişimi, frame ömrü) desteği
taşır; OWN/RAW/BORROW opcode'ları geldiğinde buraya eklenecek.

Faz 1 hedefi semantik doğrulamadır; performans değil. Değerler host Python
nesneleridir (int/float/str/bool/None) — Faz 1'in "host GC" kararı gereği.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from bytecode import (
    FunctionCode, Instr, Module, decode_code, verify_module,
    MODE_STACK, TAG_TYPE, MAX_STACK_ELEMS,
    TYPE_KIND_INT, TYPE_KIND_FLOAT, TYPE_KIND_BYTE,
    TYPE_KIND_BOOL, TYPE_KIND_PTR, TYPE_KIND_NAMES,
)


class VMError(Exception):
    """Çalışma zamanı hatası (konumlu)."""

    def __init__(self, message: str, line: int = 0, col: int = 0):
        super().__init__(message)
        self.message = message
        self.line = line
        self.col = col

    def __str__(self) -> str:
        if self.line:
            return f"line {self.line}:{self.col} — {self.message}"
        return self.message


# ─── Native yardımcıları (tree-walker ile birebir) ───────────────────────────

class StackArrayValue:
    """`@stack` gömülü tamponu (Faz 1 temsili).

    Frame ömrü boyunca `alive`; frame çıkışında ölür. Değer bağlamına
    kaçarken `STACK_MATERIALIZE` derin heap kopyası üretir; ölü tampona
    erişim tree-walker ile aynı `stack-use-after-scope` hatasına dönüşür."""

    __slots__ = ("items", "size", "kind", "alive")

    _DEFAULTS = {
        TYPE_KIND_INT: 0,
        TYPE_KIND_FLOAT: 0.0,
        TYPE_KIND_BYTE: 0,
        TYPE_KIND_BOOL: False,
        TYPE_KIND_PTR: 0,
    }

    def __init__(self, size: int, kind: int):
        self.size = size
        self.kind = kind
        self.items = [self._DEFAULTS.get(kind, 0)] * size
        self.alive = True

    def check(self) -> None:
        if not self.alive:
            raise VMError(
                "Stack value used after its scope ended (stack-use-after-scope)")

    def to_list(self) -> list:
        self.check()
        return _deep_copy(self.items)

    def __len__(self) -> int:
        self.check()
        return self.size

    def __repr__(self) -> str:
        if not self.alive:
            return "<dead stack array>"
        return "[" + ", ".join(repr(x) for x in self.items) + "]"

    __str__ = __repr__


def _deep_copy(v: Any) -> Any:
    if isinstance(v, StackArrayValue):
        return _deep_copy(v.items)
    if isinstance(v, list):
        return [_deep_copy(x) for x in v]
    if isinstance(v, dict):
        return {k: _deep_copy(x) for k, x in v.items()}
    return v


def _nex_str(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def _nex_len(v: Any) -> int:
    if isinstance(v, StackArrayValue):
        return len(v)
    if isinstance(v, (str, list, dict)):
        return len(v)
    raise TypeError(
        f"len() expects a list, dict or string, got {type(v).__name__}")


NATIVE_FUNCS = {
    "print": lambda *args: print(" ".join(str(a) for a in args)),
    "str":   _nex_str,
    "int":   int,
    "float": float,
    "len":   _nex_len,
}


@dataclass
class _Frame:
    fn:    FunctionCode
    slots: list[Any]
    stack: list[Any]
    pc:    int  # instruction index
    decoded: list[Instr]
    pc_to_index: dict[int, int]
    last_pc: int = 0


class VM:
    #: Prototip için varsayılan çağrı derinliği sınırı. Host Python'un
    #: özyineleme limitinden (~496 Nex frame) güvenli pay bırakır; aşılırsa
    #: çıplak RecursionError yerine konumlu VMError üretilir.
    MAX_CALL_DEPTH = 256

    def __init__(self, module: Module, max_call_depth: Optional[int] = None):
        # Spec §7: verifier yükleme anında zorunlu; doğrudan `run_module`
        # çağıranlar da doğrulanmamış bytecode çalıştıramaz.
        verify_module(module)
        self.module = module
        self.max_call_depth = (self.MAX_CALL_DEPTH if max_call_depth is None
                               else max_call_depth)
        if self.max_call_depth < 1:
            raise ValueError("max_call_depth must be >= 1")
        self._depth = 0
        self._decoded: list[list[Instr]] = []
        self._pc_maps: list[dict[int, int]] = []
        for fn in module.functions:
            ins = decode_code(fn.code)
            self._decoded.append(ins)
            self._pc_maps.append({i.pc: idx for idx, i in enumerate(ins)})

    # -- genel giriş ---------------------------------------------------------
    def run(self) -> Any:
        if self.module.entry is None:
            raise VMError("module has no entry function")
        try:
            return self.call(self.module.entry, [])
        except RecursionError:
            # Ortamın host recursion limiti VM sınırından düşükse son savunma:
            # çıplak RecursionError VMError'a çevrilir.
            raise VMError(
                f"call depth limit exceeded ({self.max_call_depth})") from None

    def call(self, fi: int, args: list[Any]) -> Any:
        fn = self.module.functions[fi]
        if self._depth >= self.max_call_depth:
            raise VMError(
                f"call depth limit exceeded ({self.max_call_depth}) "
                f"in function '{fn.name}'")
        if len(args) != fn.arity:
            raise VMError(
                f"function '{fn.name}' expects {fn.arity} args, got {len(args)}")
        for i, a in enumerate(args):
            # Compiler kaçışta STACK_MATERIALIZE üretir; buraya canlı bir
            # stack tamponu ulaşması ham bytecode işaretidir.
            if isinstance(a, StackArrayValue):
                raise VMError(
                    f"argument {i + 1} of '{fn.name}' is a live stack array "
                    f"(materialize it first)")
        slots: list[Any] = [None] * fn.n_locals
        for i, a in enumerate(args):
            slots[i] = a
        frame = _Frame(
            fn=fn, slots=slots, stack=[],
            pc=0, decoded=self._decoded[fi], pc_to_index=self._pc_maps[fi],
        )
        self._depth += 1
        try:
            return self._execute(frame)
        finally:
            self._depth -= 1
            self._kill_stack_slots(fn, slots)

    @staticmethod
    def _kill_stack_slots(fn: FunctionCode, slots: list[Any]) -> None:
        """Frame çıkışında @stack tamponlarını öldür (tree-walker pop_env).

        Kaçış kuralı gereği kaçan değerler zaten STACK_MATERIALIZE kopyasıdır;
        bu yalnız ham bayt kodu / iç içe sızıntı için güvenlik ağıdır."""
        for i, mode in enumerate(fn.slot_modes):
            if mode == MODE_STACK and i < len(slots):
                v = slots[i]
                if isinstance(v, StackArrayValue):
                    v.alive = False
                    v.items = []

    # -- ana döngü -----------------------------------------------------------
    def _execute(self, fr: _Frame) -> Any:
        decoded = fr.decoded
        while True:
            if fr.pc >= len(decoded):
                raise VMError("instruction pointer past end of code",
                              *fr.fn.line_of(fr.last_pc))
            ins = decoded[fr.pc]
            fr.last_pc = ins.pc
            name, a = ins.name, ins.args
            st = fr.stack

            if name == "NOP":
                pass
            elif name == "CONST":
                st.append(self.module.const_value(a[0]))
            elif name == "LOAD_LOCAL":
                st.append(fr.slots[a[0]])
            elif name == "STORE_LOCAL":
                fr.slots[a[0]] = st.pop()
            elif name == "POP":
                st.pop()
            elif name == "DUP":
                st.append(st[-1])
            elif name == "SWAP":
                st[-1], st[-2] = st[-2], st[-1]

            elif name == "STACK_ALLOC":
                si, ti, n = a
                loc = fr.fn.line_of(ins.pc)
                if (ti >= len(self.module.consts)
                        or self.module.consts[ti][0] != TAG_TYPE):
                    raise VMError(
                        "STACK_ALLOC requires a TYPE constant", *loc)
                payload = self.module.consts[ti][1]
                if (not isinstance(payload, tuple) or len(payload) != 2
                        or not all(isinstance(x, int) for x in payload)):
                    raise VMError(
                        "STACK_ALLOC has a malformed TYPE payload", *loc)
                kind, size = payload
                if kind not in TYPE_KIND_NAMES:
                    raise VMError(
                        f"STACK_ALLOC has invalid kind {kind}", *loc)
                if n < 1 or n > MAX_STACK_ELEMS:
                    raise VMError(
                        f"STACK_ALLOC element count {n} is out of range", *loc)
                if n != size:
                    raise VMError(
                        f"STACK_ALLOC size {n} != type size {size}", *loc)
                # Aynı slot yeniden kuruluyorsa eski tampon ölür (döngü
                # gövdesindeki bildirim); ham referanslar UAF görmeli.
                prev = fr.slots[si]
                if isinstance(prev, StackArrayValue):
                    prev.alive = False
                    prev.items = []
                fr.slots[si] = StackArrayValue(n, kind)

            elif name == "STACK_MATERIALIZE":
                v = fr.slots[a[0]]
                loc = fr.fn.line_of(ins.pc)
                if not isinstance(v, StackArrayValue):
                    raise VMError(
                        "STACK_MATERIALIZE on non-stack value", *loc)
                try:
                    st.append(v.to_list())
                except VMError as e:
                    raise VMError(e.message, *loc) from None

            elif name == "GET_INDEX":
                idx = st.pop()
                obj = st.pop()
                st.append(self._get_index(obj, idx, fr, ins))

            elif name == "SET_INDEX":
                val = st.pop()
                idx = st.pop()
                obj = st.pop()
                self._set_index(obj, idx, val, fr, ins)

            elif name in ("ADD", "SUB", "MUL", "DIV", "MOD",
                          "EQ", "NE", "LT", "LE", "GT", "GE"):
                right = st.pop()
                left = st.pop()
                st.append(self._binary(name, left, right, fr, ins))

            elif name == "NEG":
                st.append(-st.pop())
            elif name == "NOT":
                st.append(not st.pop())

            elif name == "JUMP":
                fr.pc = fr.pc_to_index[a[0]]
                continue
            elif name in ("JUMP_IF_FALSE", "JUMP_IF_TRUE"):
                cond = st.pop()
                truthy = self._truthy(cond)
                take = (not truthy) if name == "JUMP_IF_FALSE" else truthy
                if take:
                    fr.pc = fr.pc_to_index[a[0]]
                    continue

            elif name in ("CALL", "CALL_NATIVE", "CALL_PURE_NATIVE"):
                argc = a[1]
                if len(st) < argc:
                    raise VMError("stack underflow at call",
                                  *fr.fn.line_of(ins.pc))
                call_args = [st.pop() for _ in range(argc)]
                call_args.reverse()
                if name == "CALL":
                    if self._depth >= self.max_call_depth:
                        raise VMError(
                            f"call depth limit exceeded "
                            f"({self.max_call_depth})", *fr.fn.line_of(ins.pc))
                    result = self.call(a[0], call_args)
                else:
                    result = self._native_call(a[0], call_args, fr, ins)
                st.append(result)

            elif name == "RET":
                return st.pop() if st else None

            elif name == "TRAP":
                msg = str(self.module.const_value(a[0]))
                raise VMError(msg, *fr.fn.line_of(ins.pc))

            else:
                raise VMError(
                    f"opcode {name} is not implemented in the prototype VM",
                    *fr.fn.line_of(ins.pc))

            fr.pc += 1

    # -- yardımcılar ---------------------------------------------------------
    def _get_index(self, obj: Any, idx: Any,
                   fr: _Frame, ins: Instr) -> Any:
        loc = fr.fn.line_of(ins.pc)
        if isinstance(obj, StackArrayValue):
            if not obj.alive:
                raise VMError(
                    "Stack value used after its scope ended "
                    "(stack-use-after-scope)", *loc)
            if not isinstance(idx, int) or isinstance(idx, bool):
                raise VMError(
                    f"Stack array index must be 'int', got "
                    f"'{type(idx).__name__}'", *loc)
            if idx < 0 or idx >= obj.size:
                raise VMError(
                    f"Index {idx} out of bounds for stack array of size "
                    f"{obj.size}", *loc)
            return obj.items[idx]
        try:
            return obj[idx]
        except (IndexError, KeyError) as e:
            raise VMError(f"Index error: {e}", *loc)
        except Exception as e:  # noqa: BLE001 — konumlu hata
            raise VMError(str(e), *loc)

    def _set_index(self, obj: Any, idx: Any, val: Any,
                   fr: _Frame, ins: Instr) -> None:
        loc = fr.fn.line_of(ins.pc)
        if isinstance(obj, StackArrayValue):
            if not obj.alive:
                raise VMError(
                    "Stack value used after its scope ended "
                    "(stack-use-after-scope)", *loc)
            if not isinstance(idx, int) or isinstance(idx, bool):
                raise VMError(
                    f"Stack array index must be 'int', got "
                    f"'{type(idx).__name__}'", *loc)
            if idx < 0 or idx >= obj.size:
                raise VMError(
                    f"Index {idx} out of bounds for stack array of size "
                    f"{obj.size}", *loc)
            obj.items[idx] = val
            return
        try:
            obj[idx] = val
        except (IndexError, KeyError) as e:
            raise VMError(f"Index error: {e}", *loc)
        except Exception as e:  # noqa: BLE001 — konumlu hata
            raise VMError(str(e), *loc)

    def _binary(self, name: str, left: Any, right: Any,
                fr: _Frame, ins: Instr) -> Any:
        try:
            if name == "ADD":
                if isinstance(left, str) or isinstance(right, str):
                    return str(left) + str(right)
                return left + right
            if name == "SUB":
                return left - right
            if name == "MUL":
                return left * right
            if name == "DIV":
                if right == 0:
                    raise VMError("Division by zero", *fr.fn.line_of(ins.pc))
                return left / right
            if name == "MOD":
                if right == 0:
                    raise VMError("Division by zero", *fr.fn.line_of(ins.pc))
                return left % right
            if name == "EQ":
                return left == right
            if name == "NE":
                return left != right
            if name == "LT":
                return left < right
            if name == "LE":
                return left <= right
            if name == "GT":
                return left > right
            if name == "GE":
                return left >= right
        except VMError:
            raise
        except Exception as e:
            raise VMError(str(e), *fr.fn.line_of(ins.pc))
        raise VMError(f"unknown binary op {name}", *fr.fn.line_of(ins.pc))

    def _native_call(self, idx: int, args: list[Any],
                     fr: _Frame, ins: Instr) -> Any:
        if idx < 0 or idx >= len(self.module.natives):
            raise VMError("native index out of range", *fr.fn.line_of(ins.pc))
        nat = self.module.natives[idx]
        func = NATIVE_FUNCS.get(nat.name)
        if func is None:
            raise VMError(f"native '{nat.name}' is not implemented",
                          *fr.fn.line_of(ins.pc))
        try:
            return func(*args)
        except VMError:
            raise
        except Exception as e:
            raise VMError(str(e), *fr.fn.line_of(ins.pc))

    @staticmethod
    def _truthy(v: Any) -> bool:
        if v is None:
            return False
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            return v != 0
        if isinstance(v, StackArrayValue):
            return v.alive and v.size > 0
        if isinstance(v, (str, list, dict)):
            return len(v) > 0
        return True


def run_module(module: Module, max_call_depth: Optional[int] = None) -> Any:
    return VM(module, max_call_depth=max_call_depth).run()
