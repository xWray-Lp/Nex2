"""
Nex VM — stack tabanlı yürütücü (prototip alt kümesi).

Bytecode sözleşmesi: NEX_BYTECODE_SPEC.md. Bu VM yalnız modsuz (ownership
opcode'ları olmayan) alt kümeyi çalıştırır; OWN/STACK/RAW/BORROW opcode'ları
geldiğinde buraya eklenecek.

Faz 1 hedefi semantik doğrulamadır; performans değil. Değerler host Python
nesneleridir (int/float/str/bool/None) — Faz 1'in "host GC" kararı gereği.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from bytecode import FunctionCode, Instr, Module, decode_code, verify_module


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

def _nex_str(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def _nex_len(v: Any) -> int:
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
        if isinstance(v, (str, list, dict)):
            return len(v) > 0
        return True


def run_module(module: Module, max_call_depth: Optional[int] = None) -> Any:
    return VM(module, max_call_depth=max_call_depth).run()
