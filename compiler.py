"""
Nex Compiler — AST → bytecode (prototip alt kümesi).

Kapsam (v0.1 prototip): int/float/str/bool/null, print/str/int/float/len
native'leri, aritmetik/karşılaştırma, let/atama, if/elif/else, while,
break/continue, fonksiyon tanımı + çağrı, return, and/or (eager semantik).

Kapsam dışı (bilinçli): @own/@stack/@raw ve borrow opcode'ları, list/dict/
tuple, sınıflar, match, for, import, lambda, f-string, pipe. Bunlar açık
CompileError üretir — sessizce yanlış kod üretilmez.

Sözleşme: NEX_BYTECODE_SPEC.md. İsim çözümleme kuralı (shadowing):
önce kullanıcı fonksiyonları, sonra native tablo → `fn len` tanımlıysa
`CALL` üretilir, `CALL_PURE_NATIVE` asla.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import ast_nodes as A
from bytecode import (
    FunctionCode, Instr, Module, NativeInfo, OPCODES,
    NAT_PURE, NAT_STRUCTURAL, MODE_TEMP,
    TAG_FLOAT, TAG_INT, TAG_NAME, TAG_NULL, TAG_BOOL, TAG_STR,
    stack_effect,
)


class CompileError(Exception):
    """Derleme hatası (konumlu)."""

    def __init__(self, message: str, node: Optional[A.Node] = None):
        super().__init__(message)
        self.message = message
        self.line = getattr(node, "line", 0) if node is not None else 0
        self.col = getattr(node, "col", 0) if node is not None else 0

    def __str__(self) -> str:
        if self.line:
            return f"line {self.line}:{self.col} — {self.message}"
        return self.message


# ─── Yerleşikler ─────────────────────────────────────────────────────────────

NATIVES: list[NativeInfo] = [
    # print variadic; hiçbiri referans saklamaz → pure işaretlenebilir.
    NativeInfo("print", 0xFFFFFFFF, NAT_PURE),
    NativeInfo("str",   1, NAT_PURE),
    NativeInfo("int",   1, NAT_PURE),
    NativeInfo("float", 1, NAT_PURE),
    NativeInfo("len",   1, NAT_PURE | NAT_STRUCTURAL),
]
_NATIVE_INDEX = {n.name: i for i, n in enumerate(NATIVES)}


# ─── Emisyon yardımcıları ────────────────────────────────────────────────────

@dataclass
class _Label:
    id: int


@dataclass
class _Emit:
    name: str
    args: list
    line: int
    col:  int


class _FnBuilder:
    def __init__(self, name: str, arity: int):
        self.name = name
        self.arity = arity
        self.instrs: list[_Emit] = []
        self.label_pos: dict[int, int] = {}
        self._next_label = 0
        self._next_tmp = 0
        self.n_locals = 0
        self.slot_modes: list[int] = []
        self._scopes: list[dict[str, int]] = [{}]

    # -- sembol/slot ---------------------------------------------------------
    def push_scope(self) -> None:
        self._scopes.append({})

    def pop_scope(self) -> None:
        self._scopes.pop()

    def declare(self, name: str, node: A.Node) -> int:
        if name in self._scopes[-1]:
            raise CompileError(f"variable '{name}' already declared in this block", node)
        slot = self.n_locals
        self.n_locals += 1
        self.slot_modes.append(MODE_TEMP)
        self._scopes[-1][name] = slot
        return slot

    def resolve(self, name: str) -> Optional[int]:
        for scope in reversed(self._scopes):
            if name in scope:
                return scope[name]
        return None

    def new_tmp(self, node: A.Node) -> int:
        """Çakışmayan geçici slot (and/or codegen)."""
        name = f"$tmp{self._next_tmp}"
        self._next_tmp += 1
        return self.declare(name, node)

    # -- emisyon -------------------------------------------------------------
    def new_label(self) -> _Label:
        lbl = _Label(self._next_label)
        self._next_label += 1
        return lbl

    def mark(self, label: _Label) -> None:
        self.label_pos[label.id] = len(self.instrs)

    def emit(self, name: str, *args, node: Optional[A.Node] = None) -> None:
        self.instrs.append(_Emit(
            name, list(args),
            getattr(node, "line", 0) if node is not None else 0,
            getattr(node, "col", 0) if node is not None else 0,
        ))

    # -- assemble ------------------------------------------------------------
    def assemble(self) -> FunctionCode:
        offsets: list[int] = []
        cursor = 0
        for e in self.instrs:
            offsets.append(cursor)
            cursor += 1 + 4 * len(e.args)
        total = cursor

        def resolve_arg(arg):
            if isinstance(arg, _Label):
                if arg.id not in self.label_pos:
                    raise CompileError(
                        f"internal: unresolved label {arg.id} in fn {self.name}")
                idx = self.label_pos[arg.id]
                if idx == len(self.instrs):
                    return total
                return offsets[idx]
            return arg

        resolved: list[Instr] = []
        for i, e in enumerate(self.instrs):
            args = tuple(resolve_arg(a) for a in e.args)
            resolved.append(Instr(offsets[i], e.name, args))

        max_stack = self._analyze_max_stack(resolved)

        code = bytearray()
        for ins in resolved:
            opcode, nargs = OPCODES[ins.name]
            code.append(opcode)
            for a in ins.args:
                code += int(a).to_bytes(4, "little")

        line_table = [
            (ins.pc, e.line, e.col)
            for ins, e in zip(resolved, self.instrs)
        ]

        return FunctionCode(
            name=self.name,
            arity=self.arity,
            n_locals=self.n_locals,
            max_stack=max_stack,
            code=bytes(code),
            slot_modes=bytes(self.slot_modes),
            line_table=line_table,
        )

    @staticmethod
    def _analyze_max_stack(instrs: list[Instr]) -> int:
        if not instrs:
            raise CompileError("internal: empty function")
        by_pc = {ins.pc: i for i, ins in enumerate(instrs)}
        depth = {0: 0}
        max_depth = 0
        work = [0]
        while work:
            i = work.pop()
            ins = instrs[i]
            d = depth[i]
            pops, pushes = stack_effect(ins.name, ins.args)
            if d < pops:
                raise CompileError(
                    f"internal: stack underflow at pc {ins.pc} ({ins.name})")
            nd = d - pops + pushes
            max_depth = max(max_depth, nd)
            if ins.name == "RET":
                continue
            succ: list[int] = []
            if ins.name in ("JUMP", "JUMP_IF_FALSE", "JUMP_IF_TRUE"):
                target = ins.args[0]
                if target not in by_pc:
                    raise CompileError(
                        f"internal: jump target {target} is not an instruction")
                succ.append(by_pc[target])
            if ins.name != "JUMP":
                if i + 1 >= len(instrs):
                    raise CompileError("internal: fallthrough past end")
                succ.append(i + 1)
            for s in succ:
                if s not in depth:
                    depth[s] = nd
                    work.append(s)
                elif depth[s] != nd:
                    raise CompileError(
                        f"internal: inconsistent stack depth at pc {instrs[s].pc}")
        return max(max_depth, 1)


# ─── Derleyici ───────────────────────────────────────────────────────────────

class Compiler:
    def __init__(self, tree: A.Program, filename: str = ""):
        self.tree = tree
        self.filename = filename
        self.module = Module()
        self._func_index: dict[str, int] = {}
        self._func_decls: list[A.FnDecl] = []
        self._const_index: dict[tuple[int, object], int] = {}
        self._builder: Optional[_FnBuilder] = None
        self._loops: list[tuple[_Label, _Label]] = []  # (break, continue)

    # -- sabit havuzu --------------------------------------------------------
    def _const(self, tag: int, value: object) -> int:
        key = (tag, value)
        idx = self._const_index.get(key)
        if idx is None:
            idx = len(self.module.consts)
            self.module.consts.append((tag, value))
            self._const_index[key] = idx
        return idx

    def _name_const(self, name: str) -> int:
        return self._const(TAG_NAME, name)

    # -- giriş ---------------------------------------------------------------
    def compile(self) -> Module:
        for stmt in self.tree.stmts:
            if isinstance(stmt, A.FnDecl):
                if stmt.name in self._func_index:
                    raise CompileError(
                        f"duplicate function '{stmt.name}'", stmt)
                if stmt.body is None:
                    raise CompileError(
                        f"function '{stmt.name}' has no body (interface?)", stmt)
                if stmt.is_async:
                    raise CompileError("async functions are not supported in VM", stmt)
                self._func_index[stmt.name] = len(self._func_decls)
                self._func_decls.append(stmt)
            elif isinstance(stmt, (A.ClassDecl, A.InterfaceDecl, A.ImportStmt,
                                   A.ModuleStmt)):
                raise CompileError(
                    f"{type(stmt).__name__} is not supported in the VM prototype",
                    stmt)
            else:
                raise CompileError(
                    "top-level statements are not supported in the VM prototype "
                    "(put code in fn main)", stmt)

        if not self._func_decls:
            raise CompileError("no functions to compile")
        if "main" not in self._func_index:
            raise CompileError("VM prototype requires a 'main' function")

        # Native tablo
        self.module.natives = list(NATIVES)
        for nat in self.module.natives:
            self._name_const(nat.name)

        # Fonksiyon adları debug/sembol amaçlı sabit havuzunda olmalı
        # (encode_module function tablosu addan const indeksi yazar).
        for decl in self._func_decls:
            self._name_const(decl.name)

        for decl in self._func_decls:
            self.module.functions.append(self._compile_function(decl))

        self.module.entry = self._func_index["main"]
        return self.module

    # -- fonksiyon -----------------------------------------------------------
    def _compile_function(self, decl: A.FnDecl) -> FunctionCode:
        b = _FnBuilder(decl.name, arity=len(decl.params))
        self._builder = b
        self._loops = []

        for p in decl.params:
            if p.default is not None:
                raise CompileError(
                    "default parameters are not supported in the VM prototype", p)
            if p.is_ref or p.is_mut_ref:
                raise CompileError(
                    "reference parameters are not supported in the VM prototype", p)
            if isinstance(p.type_ann, A.TypeRef):
                raise CompileError(
                    "reference parameters are not supported in the VM prototype", p)
            b.declare(p.name, p)

        self._compile_block(decl.body, new_scope=False)
        # Düşen akış: null dön.
        b.emit("CONST", self._const(TAG_NULL, None), node=decl)
        b.emit("RET", node=decl)

        self._builder = None
        return b.assemble()

    # -- statement'lar -------------------------------------------------------
    def _compile_block(self, block: A.Block, new_scope: bool = True) -> None:
        b = self._builder
        if new_scope:
            b.push_scope()
        for stmt in block.stmts:
            self._compile_stmt(stmt)
        if new_scope:
            b.pop_scope()

    def _compile_stmt(self, node: A.Node) -> None:
        b = self._builder
        if isinstance(node, A.VarDecl):
            if node.mem_mode is not None:
                raise CompileError(
                    f"@{node.mem_mode} is not supported in the VM prototype", node)
            if node.value is not None:
                self._compile_expr(node.value)
            else:
                b.emit("CONST", self._const(TAG_NULL, None), node=node)
            slot = b.declare(node.name, node)
            b.emit("STORE_LOCAL", slot, node=node)
            return

        if isinstance(node, A.ExprStmt):
            self._compile_expr(node.expr)
            # İfade sonucu kullanılmıyorsa at (atama da değer bırakır).
            b.emit("POP", node=node)
            return

        if isinstance(node, A.IfStmt):
            self._compile_if(node)
            return

        if isinstance(node, A.WhileStmt):
            start = b.new_label()
            end = b.new_label()
            b.mark(start)
            self._compile_expr(node.condition)
            b.emit("JUMP_IF_FALSE", end, node=node)
            self._loops.append((end, start))
            self._compile_block(node.body)
            self._loops.pop()
            b.emit("JUMP", start, node=node)
            b.mark(end)
            return

        if isinstance(node, A.ReturnStmt):
            if node.value is not None:
                self._compile_expr(node.value)
            else:
                b.emit("CONST", self._const(TAG_NULL, None), node=node)
            b.emit("RET", node=node)
            return

        if isinstance(node, A.BreakStmt):
            if not self._loops:
                raise CompileError("'break' outside loop", node)
            b.emit("JUMP", self._loops[-1][0], node=node)
            return

        if isinstance(node, A.ContinueStmt):
            if not self._loops:
                raise CompileError("'continue' outside loop", node)
            b.emit("JUMP", self._loops[-1][1], node=node)
            return

        if isinstance(node, A.FnDecl):
            raise CompileError(
                "nested function declarations are not supported in the VM prototype",
                node)

        raise CompileError(
            f"{type(node).__name__} is not supported in the VM prototype", node)

    def _compile_if(self, node: A.IfStmt) -> None:
        b = self._builder
        end = b.new_label()
        arms: list[tuple[A.Node, A.Block]] = [(node.condition, node.then_block)]
        arms.extend(node.elif_arms)
        for cond, block in arms:
            next_arm = b.new_label()
            self._compile_expr(cond)
            b.emit("JUMP_IF_FALSE", next_arm, node=cond)
            self._compile_block(block)
            b.emit("JUMP", end, node=node)
            b.mark(next_arm)
        if node.else_block is not None:
            self._compile_block(node.else_block)
        b.mark(end)

    # -- expression'lar ------------------------------------------------------
    def _compile_expr(self, node: A.Node) -> None:
        b = self._builder
        if isinstance(node, A.IntLit):
            b.emit("CONST", self._const(TAG_INT, int(node.value)), node=node)
            return
        if isinstance(node, A.FloatLit):
            b.emit("CONST", self._const(TAG_FLOAT, float(node.value)), node=node)
            return
        if isinstance(node, A.StrLit):
            b.emit("CONST", self._const(TAG_STR, node.value), node=node)
            return
        if isinstance(node, A.BoolLit):
            val = (node.value == "true") if isinstance(node.value, str) \
                else bool(node.value)
            b.emit("CONST", self._const(TAG_BOOL, val), node=node)
            return
        if isinstance(node, A.NullLit):
            b.emit("CONST", self._const(TAG_NULL, None), node=node)
            return

        if isinstance(node, A.Ident):
            slot = b.resolve(node.name)
            if slot is None:
                raise CompileError(f"undefined variable '{node.name}'", node)
            b.emit("LOAD_LOCAL", slot, node=node)
            return

        if isinstance(node, A.UnaryExpr):
            self._compile_expr(node.operand)
            if node.op == "-":
                b.emit("NEG", node=node)
            elif node.op == "not":
                b.emit("NOT", node=node)
            else:
                raise CompileError(
                    f"unary operator '{node.op}' is not supported in the VM prototype",
                    node)
            return

        if isinstance(node, A.BinaryExpr):
            self._compile_binary(node)
            return

        if isinstance(node, A.AssignExpr):
            if node.op != "ASSIGN":
                raise CompileError(
                    f"compound assignment '{node.op}' is not supported in the VM prototype",
                    node)
            if not isinstance(node.target, A.Ident):
                raise CompileError(
                    "only variable assignment is supported in the VM prototype",
                    node)
            slot = b.resolve(node.target.name)
            if slot is None:
                raise CompileError(
                    f"undefined variable '{node.target.name}'", node)
            self._compile_expr(node.value)
            b.emit("DUP", node=node)
            b.emit("STORE_LOCAL", slot, node=node)
            return

        if isinstance(node, A.CallExpr):
            self._compile_call(node)
            return

        raise CompileError(
            f"{type(node).__name__} is not supported in the VM prototype", node)

    def _compile_binary(self, node: A.BinaryExpr) -> None:
        b = self._builder
        op = node.op

        # Eager and/or (tree-walker semantiği): iki taraf da değerlendirilir,
        # sonuç sol değerin truthiness'ine göre operandlardan biridir.
        if op in ("and", "or"):
            tmp = b.new_tmp(node)
            self._compile_expr(node.left)
            b.emit("STORE_LOCAL", tmp, node=node)
            self._compile_expr(node.right)          # stack: [right]
            b.emit("LOAD_LOCAL", tmp, node=node)    # stack: [right, left]
            pick_left = b.new_label()
            end = b.new_label()
            # and: sol falsy → sonuç sol; or: sol truthy → sonuç sol.
            cond_jump = "JUMP_IF_FALSE" if op == "and" else "JUMP_IF_TRUE"
            b.emit(cond_jump, pick_left, node=node)  # sol'u tüketir
            # Aksi hâlde sonuç sağ; stack'te zaten [right] var.
            b.emit("JUMP", end, node=node)
            b.mark(pick_left)
            b.emit("POP", node=node)                 # right'ı at
            b.emit("LOAD_LOCAL", tmp, node=node)     # sol'u geri getir
            b.mark(end)
            return

        table = {
            "+": "ADD", "-": "SUB", "*": "MUL", "/": "DIV", "%": "MOD",
            "==": "EQ", "!=": "NE", "<": "LT", "<=": "LE",
            ">": "GT", ">=": "GE",
        }
        opcode = table.get(op)
        if opcode is None:
            raise CompileError(
                f"binary operator '{op}' is not supported in the VM prototype", node)
        self._compile_expr(node.left)
        self._compile_expr(node.right)
        b.emit(opcode, node=node)

    def _compile_call(self, node: A.CallExpr) -> None:
        b = self._builder
        if not isinstance(node.callee, A.Ident):
            raise CompileError(
                "only direct function calls are supported in the VM prototype",
                node)
        if node.type_args:
            raise CompileError("call-site type args are not supported in VM", node)
        name = node.callee.name

        # Yerel değişken aynı adı gölgeliyorsa çağrılamaz: aksi hâlde
        # `let len = 5; len(1)` sessizce native'e düşerdi.
        if b.resolve(name) is not None:
            raise CompileError(
                f"'{name}' is a variable, not a callable function", node)

        for arg in node.args:
            self._compile_expr(arg)

        # İsim çözümleme: kullanıcı fonksiyonu ÖNCE, native SONRA.
        if name in self._func_index:
            b.emit("CALL", self._func_index[name], len(node.args), node=node)
            return
        if name in _NATIVE_INDEX:
            b.emit("CALL_PURE_NATIVE", _NATIVE_INDEX[name], len(node.args),
                   node=node)
            return

        raise CompileError(f"undefined function '{name}'", node)


# ─── Kolaylık API ────────────────────────────────────────────────────────────

def compile_program(tree: A.Program, filename: str = "") -> Module:
    return Compiler(tree, filename).compile()
