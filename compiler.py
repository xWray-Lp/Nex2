"""
Nex Compiler — AST → bytecode (prototip alt kümesi).

Kapsam (v0.1 prototip): int/float/str/bool/null, print/str/int/float/len
native'leri, aritmetik/karşılaştırma, let/atama, if/elif/else, while,
break/continue, fonksiyon tanımı + çağrı, return, and/or (eager semantik),
`@stack` sabit boyutlu diziler (STACK_ALLOC/STACK_MATERIALIZE, indeks
okuma/yazma, kaçışta derin heap kopyası), `@own` sahipliği
(OWN_ALLOC/OWN_MOVE/OWN_FREE; borrow yok) ve genel list/dict literalleri
(NEW_LIST/NEW_DICT + GET_INDEX/SET_INDEX).

Kapsam dışı (bilinçli): @raw, nested @stack, `str` elemanlı stack dizi,
tuple, sınıflar, match, for, import, lambda, f-string, pipe; ayrıca
list/dict üzerinde compound atama ve metotlar (`.push`, `.length`).
Bunlar açık CompileError üretir — sessizce yanlış kod üretilmez.

`@own` kuralları (tree-walker parity) ve borrow (Phase B, v1 alt kümesi):
- `@own let x = to_own(expr)` ve mevcut @own değişkene `x = to_own(expr)`
  tek meşru kuruluş yollarıdır; OWN_ALLOC derin kopya üretir.
- `to_gc(x)` / `free(x)` yalnızca @own değişken adı alır → OWN_MOVE /
  OWN_FREE. `free` ifade bağlamı için null bırakır (tree-walker None).
- @own değer ifade olarak okunamaz (LOAD_LOCAL yasak): `cannot copy @own
  value` hatası. Fonksiyon sınırı, print, return, list/dict depolama yok.
- `let r = &x` / `&mut x` → BORROW_SHARED/BORROW_MUT; borrow block-lokal
  (scope çıkışında BORROW_END, ters bildirim sırası). BorrowExpr yalnız
  @own let başlatıcısı ve allowlist saf native argümanında (len/keys/
  values, gölgelemesiz) derlenir; başka her bağlamda `borrowed references
  cannot cross function boundaries` hatası.

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
    NAT_PURE, NAT_STRUCTURAL, MODE_TEMP, MODE_STACK, MODE_OWN, MODE_BORROW,
    TAG_FLOAT, TAG_INT, TAG_NAME, TAG_NULL, TAG_BOOL, TAG_STR, TAG_TYPE,
    TYPE_KIND_INT, TYPE_KIND_FLOAT, TYPE_KIND_BYTE, TYPE_KIND_BOOL,
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
    # keys/values: saf/yapısal (borç argümanı allowlist'i, spec §5).
    NativeInfo("keys",   1, NAT_PURE | NAT_STRUCTURAL),
    NativeInfo("values", 1, NAT_PURE | NAT_STRUCTURAL),
]
_NATIVE_INDEX = {n.name: i for i, n in enumerate(NATIVES)}

#: VM'in ilk @stack kapsamı: ilkel eleman tipleri. `ptr` bilinçli olarak
#: yok — Nex kaynak dilinde `ptr` tipi yok, yalnız TYPE kodlamasında rezerve.
_STACK_ELEM_KINDS = {
    "int":   TYPE_KIND_INT,
    "float": TYPE_KIND_FLOAT,
    "byte":  TYPE_KIND_BYTE,
    "bool":  TYPE_KIND_BOOL,
}

#: Sahiplik builtin'leri. Bunlar yalnız özel call konumlarında geçerlidir
#: (`@own` başlatıcısı, to_gc/free move'u); başka her kullanım CompileError.
_OWN_BUILTINS = ("to_own", "to_gc", "free")


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
        #: Her scope'ta bildirilen BORROW slotları; pop_scope blok çıkışında
        #: ters bildirim sırasıyla BORROW_END üretir (tree-walker close()).
        self._scope_borrows: list[list[int]] = [[]]

    # -- sembol/slot ---------------------------------------------------------
    def push_scope(self) -> None:
        self._scopes.append({})
        self._scope_borrows.append([])

    def pop_scope(self) -> None:
        # Blok çıkışı: borrow release → scope kayıtları kapanır
        # (tree-walker close() sırasının borrow ayağı).
        for slot in reversed(self._scope_borrows.pop()):
            self.emit("BORROW_END", slot)
        self._scopes.pop()

    def declare(self, name: str, node: A.Node, mode: int = MODE_TEMP) -> int:
        if name in self._scopes[-1]:
            raise CompileError(f"variable '{name}' already declared in this block", node)
        slot = self.n_locals
        self.n_locals += 1
        self.slot_modes.append(mode)
        self._scopes[-1][name] = slot
        return slot

    def slot_mode(self, slot: int) -> int:
        return self.slot_modes[slot]

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

    def register_borrow(self, slot: int) -> None:
        """BORROW slotunu bulunduğu scope'un çıkış listesine ekle."""
        self._scope_borrows[-1].append(slot)

    def new_borrow_tmp(self, node: A.Node) -> int:
        """Çakışmayan geçici BORROW slot'u (allowlist saf native argümanı)."""
        name = f"$brw{self._next_tmp}"
        self._next_tmp += 1
        slot = self.declare(name, node, MODE_BORROW)
        self.register_borrow(slot)
        return slot

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
        # (break, continue, base_depth): base_depth = döngü gövde scope'unun
        # borrow kayıt indeksi; break/continue bu derinliğe kadar açık
        # borrow'ları JUMP'tan önce release eder (bkz. _release_borrows_to).
        self._loops: list[tuple[_Label, _Label, int]] = []

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

    def _release_borrows_to(self, base: int) -> None:
        """`base` derinliğine kadar açık tüm scope borrow'larını release et.

        `continue`/`break`/`return` pop_scope'u atladığı için bu yollarda
        borrow sayaçları sızardı; burada her scope'un borrow slotları için
        pop_scope ile aynı ters bildirim sırasında BORROW_END üretilir.
        Düzenli pop_scope yolundaki ikinci BORROW_END zararsızdır:
        BorrowValue.release released borrow'da erken döner."""
        b = self._builder
        for i in range(len(b._scope_borrows) - 1, base - 1, -1):
            for slot in reversed(b._scope_borrows[i]):
                b.emit("BORROW_END", slot)

    def _compile_stmt(self, node: A.Node) -> None:
        b = self._builder
        if isinstance(node, A.VarDecl):
            if node.mem_mode == "stack":
                self._compile_stack_decl(node)
                return
            if node.mem_mode == "own":
                self._compile_own_decl(node)
                return
            if node.mem_mode is not None:
                raise CompileError(
                    f"@{node.mem_mode} is not supported in the VM prototype", node)
            if isinstance(node.value, A.BorrowExpr):
                # `let r = &x` / `let r = &mut x` → BORROW_SHARED/MUT.
                self._compile_borrow_decl(node)
                return
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
            # Gövde scope'u açılmadan önceki derinlik = gövde scope indeksi;
            # continue/break bu derinliğe kadar açık borrow'ları release eder.
            base = len(b._scope_borrows)
            self._loops.append((end, start, base))
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
            # RET pop_scope'u atlar: tüm açık scope borrow'larını release et
            # (frame-exit kill istisna yolu için genel güvenlik ağı kalır).
            self._release_borrows_to(0)
            b.emit("RET", node=node)
            return

        if isinstance(node, A.BreakStmt):
            if not self._loops:
                raise CompileError("'break' outside loop", node)
            self._release_borrows_to(self._loops[-1][2])
            b.emit("JUMP", self._loops[-1][0], node=node)
            return

        if isinstance(node, A.ContinueStmt):
            if not self._loops:
                raise CompileError("'continue' outside loop", node)
            self._release_borrows_to(self._loops[-1][2])
            b.emit("JUMP", self._loops[-1][1], node=node)
            return

        if isinstance(node, A.FnDecl):
            raise CompileError(
                "nested function declarations are not supported in the VM prototype",
                node)

        raise CompileError(
            f"{type(node).__name__} is not supported in the VM prototype", node)

    def _compile_stack_decl(self, node: A.VarDecl) -> None:
        """`@stack let buf: [T; N]` → STACK_ALLOC (+ literal init için SET_INDEX).

        İlk VM kapsamı bilinçli olarak dar: yalnız ilkel eleman tipleri,
        literal init veya öntanımlı değerler, indeks erişimi ve kaçışta
        STACK_MATERIALIZE. Nested stack dizi / `str` eleman / for / `.length`
        bu turda kapsam dışı (konumlu CompileError)."""
        b = self._builder
        ta = node.type_ann
        if (not isinstance(ta, A.TypeStackArray)
                or not isinstance(ta.size, A.IntLit)):
            raise CompileError(
                f"@stack declaration '{node.name}' requires a fixed-size "
                f"array type such as [int; 4]", node)
        size = int(ta.size.value)
        if size < 1:
            raise CompileError("@stack array size must be at least 1", node)
        elem = ta.elem
        if isinstance(elem, A.TypeStackArray):
            raise CompileError(
                "nested stack arrays are not supported in the VM prototype", node)
        if not isinstance(elem, A.TypeName) or elem.name not in _STACK_ELEM_KINDS:
            name = getattr(elem, "name", type(elem).__name__)
            raise CompileError(
                f"stack array element type '{name}' is not supported in the "
                f"VM prototype (int/float/byte/bool only)", node)

        if node.value is not None and not isinstance(node.value, A.ListLit):
            raise CompileError(
                f"@stack declaration '{node.name}' requires an array literal",
                node.value)
        if node.value is not None and len(node.value.elements) != size:
            raise CompileError(
                f"Stack array size mismatch for '{node.name}': declared size "
                f"{size}, got {len(node.value.elements)} element(s)", node.value)

        kind = _STACK_ELEM_KINDS[elem.name]
        slot = b.declare(node.name, node, MODE_STACK)
        type_const = self._const(TAG_TYPE, (kind, size))
        b.emit("STACK_ALLOC", slot, type_const, size, node=node)

        # Başlatıcı yoksa tampon TYPE kind'a göre öntanımlı doldurulur (VM).
        if node.value is not None:
            for i, element in enumerate(node.value.elements):
                b.emit("LOAD_LOCAL", slot, node=node)
                b.emit("CONST", self._const(TAG_INT, i), node=node)
                self._compile_expr(element)
                b.emit("SET_INDEX", node=element)

    def _compile_own_decl(self, node: A.VarDecl) -> None:
        """`@own let x = to_own(expr)` → expr; OWN_ALLOC x.

        Yalnız to_own(...) başlatıcısı kabul edilir (tree-walker: `@own`
        bildirimi to_own sonucu olmalı). OWN_ALLOC derin kopyayı VM'de
        yapar; burada varsayılan değer üretilmez."""
        b = self._builder
        self._compile_own_source(node.value, node,
                                 f"declaration '{node.name}'")
        slot = b.declare(node.name, node, MODE_OWN)
        b.emit("OWN_ALLOC", slot, node=node)

    def _compile_borrow_decl(self, node: A.VarDecl) -> None:
        """`let r = &x` / `let r = &mut x` → BORROW_SHARED/BORROW_MUT r, x.

        Borrow block-lokaldir: slot MODE_BORROW'a düşer, bulunduğu scope'un
        çıkış listesine yazılır ve pop_scope blok çıkışında BORROW_END
        üretir (fonksiyon en dış bloğu frame çıkışı kill'e emanettir).
        Varsayılan değer itilmez; slot BORROW_END'a kadar boş kalır."""
        b = self._builder
        src = self._borrow_source(node.value)
        slot = b.declare(node.name, node, MODE_BORROW)
        b.register_borrow(slot)
        opcode = "BORROW_MUT" if node.value.mutable else "BORROW_SHARED"
        b.emit(opcode, slot, src, node=node)

    def _borrow_source(self, node: A.BorrowExpr) -> int:
        """`&x` / `&mut x` operandını MODE_OWN slot'a çözümle.

        v1: operand yalnızca Ident olabilir; hedef @own slot değilse
        tree-walker ile aynı hata."""
        b = self._builder
        if not isinstance(node.operand, A.Ident):
            raise CompileError("only @own values can be borrowed", node.operand)
        slot = b.resolve(node.operand.name)
        if slot is None:
            raise CompileError(
                f"undefined variable '{node.operand.name}'", node.operand)
        if b.slot_mode(slot) != MODE_OWN:
            raise CompileError("only @own values can be borrowed", node.operand)
        return slot

    def _compile_own_source(self, value: Optional[A.Node],
                            node: A.Node, what: str) -> None:
        """@own kuruluş RHS'i: `to_own(expr)` → expr'i stack'e iter.

        OWN_ALLOC çağıran tarafından yayınlanır. Başka her RHS reddedilir;
        `to_own`'ın argümanı serbest ifade olabilir (to_own(f(x)))."""
        if isinstance(value, A.BorrowExpr):
            raise CompileError(
                "borrowed references cannot cross function boundaries", value)
        if not (isinstance(value, A.CallExpr)
                and isinstance(value.callee, A.Ident)
                and value.callee.name == "to_own"):
            raise CompileError(
                f"@own {what} can only be initialized from to_own(...)", node)
        if value.type_args:
            raise CompileError("to_own does not take type arguments", value)
        if len(value.args) != 1:
            raise CompileError("to_own requires exactly 1 argument", value)
        # Argüman @own bir değişkense _compile_expr "cannot copy @own value"
        # üretir — to_own(zaten-own) tree-walker'da da hatadır.
        self._compile_expr(value.args[0])

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

        if isinstance(node, A.ListLit):
            # Genel liste ifadesi: elemanlar sırayla değerlendirilir, sonra
            # NEW_LIST hepsini tek host listesinde toplar. Eleman bir @stack
            # değeri ise Ident yolu STACK_MATERIALIZE üretir (bağımsız kopya).
            for elem in node.elements:
                self._compile_expr(elem)
            b.emit("NEW_LIST", len(node.elements), node=node)
            return

        if isinstance(node, A.DictLit):
            # Genel dict ifadesi: k1 v1 k2 v2 ... sırasıyla stack'e itilir,
            # NEW_DICT n ikiliyi sözlüğe toplar. Boş literal NEW_DICT 0.
            for key_node, value_node in node.pairs:
                self._compile_expr(key_node)
                self._compile_expr(value_node)
            b.emit("NEW_DICT", len(node.pairs), node=node)
            return

        if isinstance(node, A.Ident):
            if node.name in _OWN_BUILTINS:
                raise CompileError(
                    f"'{node.name}' is an @own builtin and can only be called "
                    f"directly", node)
            slot = b.resolve(node.name)
            if slot is None:
                raise CompileError(f"undefined variable '{node.name}'", node)
            if b.slot_mode(slot) == MODE_OWN:
                # OWN slotu okunamaz: kopyalama sahipliği ikiye böler.
                raise CompileError(
                    f"cannot copy @own value '{node.name}' "
                    f"(move it with to_gc(x))", node)
            if b.slot_mode(slot) == MODE_STACK:
                # Kaçış kuralı: stack değeri ifade bağlamında bağımsız heap
                # kopyasıdır. STACK_MATERIALIZE slotu okur, stack'e list iter.
                b.emit("STACK_MATERIALIZE", slot, node=node)
            else:
                b.emit("LOAD_LOCAL", slot, node=node)
            return

        if isinstance(node, A.BorrowExpr):
            # BorrowExpr yalnız iki bağlamda derlenir: @own let başlatıcısı
            # (`let r = &x`) ve allowlist saf native argümanı (len/keys/
            # values). Diğer her bağlam fonksiyon sınırını ihlal eder.
            raise CompileError(
                "borrowed references cannot cross function boundaries", node)

        if isinstance(node, A.IndexExpr):
            self._compile_index(node)
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
            if isinstance(node.value, A.BorrowExpr):
                # `r = &x` yeniden ataması v1'de yok: borrow taze let
                # bildirimiyle açılır (scope çıkışı BORROW_END'i kesin
                # bilir; yeniden atama release zamanını belirsizleştirir).
                raise CompileError(
                    "borrow expressions need a fresh let binding (v1)", node)
            if isinstance(node.target, A.Ident):
                slot = b.resolve(node.target.name)
                if slot is None:
                    raise CompileError(
                        f"undefined variable '{node.target.name}'", node)
                if b.slot_mode(slot) == MODE_OWN:
                    if node.op != "ASSIGN":
                        raise CompileError(
                            f"compound assignment is not supported on @own "
                            f"variable '{node.target.name}'", node)
                    # Yeniden atama: eski yük ölür, yeni to_own derin kopyası
                    # OWN_ALLOC ile slota yazılır. İfade değeri olarak null
                    # bırakılır (tree-walker own wrapper döner; okunamaz).
                    self._compile_own_source(
                        node.value, node.target,
                        f"variable '{node.target.name}'")
                    b.emit("OWN_ALLOC", slot, node=node)
                    b.emit("CONST", self._const(TAG_NULL, None), node=node)
                    return
                if b.slot_mode(slot) == MODE_STACK:
                    raise CompileError(
                        "whole stack array reassignment is not supported in the "
                        "VM prototype (assign elements instead)", node)
                self._compile_expr(node.value)
                b.emit("DUP", node=node)
                b.emit("STORE_LOCAL", slot, node=node)
                return
            if isinstance(node.target, A.IndexExpr):
                self._compile_index_assign(node)
                return
            raise CompileError(
                "only variable and index assignment are supported in the "
                "VM prototype", node)

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

    def _compile_index(self, node: A.IndexExpr) -> None:
        b = self._builder
        # Stack dizisinin kendisi kaçmaz: ham slot yüklenip GET_INDEX uygulanır
        # (STACK_MATERIALIZE yalnız değer bağlamlarında).
        if isinstance(node.obj, A.Ident):
            slot = b.resolve(node.obj.name)
            if slot is not None and b.slot_mode(slot) == MODE_STACK:
                b.emit("LOAD_LOCAL", slot, node=node.obj)
                self._compile_expr(node.index)
                b.emit("GET_INDEX", node=node)
                return
        self._compile_expr(node.obj)
        self._compile_expr(node.index)
        b.emit("GET_INDEX", node=node)

    def _compile_index_assign(self, node: A.AssignExpr) -> None:
        """`obj[i] = v` — ifade değeri olarak atanan değer stack'te kalır."""
        b = self._builder
        target = node.target
        assert isinstance(target, A.IndexExpr)
        if isinstance(target.obj, A.Ident):
            slot = b.resolve(target.obj.name)
            if slot is not None and b.slot_mode(slot) == MODE_OWN:
                raise CompileError(
                    f"index assignment through @own value '{target.obj.name}' "
                    f"is not supported in the VM prototype "
                    f"(move it with to_gc(x) first)", node)
        tmp = b.new_tmp(node)
        self._compile_expr(node.value)          # [v]
        b.emit("DUP", node=node)                # [v, v]
        b.emit("STORE_LOCAL", tmp, node=node)   # [v]
        if isinstance(target.obj, A.Ident):
            slot = b.resolve(target.obj.name)
            if slot is not None and b.slot_mode(slot) == MODE_STACK:
                b.emit("LOAD_LOCAL", slot, node=target.obj)
            else:
                self._compile_expr(target.obj)
        else:
            self._compile_expr(target.obj)      # [v, obj]
        self._compile_expr(target.index)        # [v, obj, idx]
        b.emit("LOAD_LOCAL", tmp, node=node)    # [v, obj, idx, v]
        b.emit("SET_INDEX", node=node)          # [v]

    def _compile_call(self, node: A.CallExpr) -> None:
        b = self._builder
        if not isinstance(node.callee, A.Ident):
            raise CompileError(
                "only direct function calls are supported in the VM prototype",
                node)
        if node.type_args:
            raise CompileError("call-site type args are not supported in VM", node)
        name = node.callee.name

        # Sahiplik builtin'leri isimle çözülür (tree-walker/checker gibi):
        # yerel gölgeleme bu çağrıları native fonksiyona düşürmez.
        if name == "to_own":
            raise CompileError(
                "to_own(...) is only allowed as the initializer of an @own "
                "declaration or assignment", node)
        if name in ("to_gc", "free"):
            self._compile_own_move_call(node, name)
            return

        # Yerel değişken aynı adı gölgeliyorsa çağrılamaz: aksi hâlde
        # `let len = 5; len(1)` sessizce native'e düşerdi.
        if b.resolve(name) is not None:
            raise CompileError(
                f"'{name}' is a variable, not a callable function", node)

        # Borrow argüman kuralı (checker visit_CallExpr ile aynı): yalnız
        # gölgelemesiz allowlist saf native'ler (len/keys/values) &arg
        # kabul eder; kullanıcı fonksiyonu dahil her şeyde borrow fonksiyon
        # sınırını geçemez.
        allow_borrow = (name in ("len", "keys", "values")
                        and name not in self._func_index)
        borrow_temps: list[int] = []
        for arg in node.args:
            if isinstance(arg, A.BorrowExpr):
                if not allow_borrow:
                    raise CompileError(
                        "borrowed references cannot cross function "
                        "boundaries", arg)
                # `len(&xs)`: taze BORROW temp aç, borrow'u kur, temp'i
                # yığına it; çağrıdan hemen sonra BORROW_END ile release.
                src = self._borrow_source(arg)
                tmp = b.new_borrow_tmp(arg)
                b.emit("BORROW_MUT" if arg.mutable else "BORROW_SHARED",
                       tmp, src, node=arg)
                b.emit("LOAD_LOCAL", tmp, node=arg)
                borrow_temps.append(tmp)
            else:
                self._compile_expr(arg)

        # İsim çözümleme: kullanıcı fonksiyonu ÖNCE, native SONRA.
        if name in self._func_index:
            b.emit("CALL", self._func_index[name], len(node.args), node=node)
        elif name in _NATIVE_INDEX:
            b.emit("CALL_PURE_NATIVE", _NATIVE_INDEX[name], len(node.args),
                   node=node)
        else:
            raise CompileError(f"undefined function '{name}'", node)
        for tmp in borrow_temps:
            b.emit("BORROW_END", tmp, node=node)

    def _compile_own_move_call(self, node: A.CallExpr, name: str) -> None:
        """`to_gc(x)` / `free(x)` — x @own slot olmalı; OWN_MOVE/OWN_FREE.

        Borrow denetimi Faz B'de gelecek; Phase A'da açık borrow yok.
        `free` ifade bağlamında null bırakır (tree-walker `free` → None)."""
        b = self._builder
        if node.type_args:
            raise CompileError(f"{name} does not take type arguments", node)
        if len(node.args) != 1:
            raise CompileError(f"{name} requires exactly 1 argument", node)
        arg = node.args[0]
        if not isinstance(arg, A.Ident):
            raise CompileError(
                f"{name} requires a variable reference", arg)
        slot = b.resolve(arg.name)
        if slot is None:
            raise CompileError(f"undefined variable '{arg.name}'", arg)
        if b.slot_mode(slot) != MODE_OWN:
            raise CompileError(
                f"{name} requires an @own variable; '{arg.name}' is not @own",
                arg)
        # Borrow çakışması runtime'da OWN_MOVE/OWN_FREE içinde denetlenir:
        # owner sayaçları (shared/mut_borrowed) 0'dan fazlaysa
        # "cannot move/free '<slot>' while borrowed" hatası (Phase B).
        b.emit("OWN_MOVE" if name == "to_gc" else "OWN_FREE", slot, node=node)
        if name == "free":
            # İfade bağlamı (ExprStmt POP'u, let, return) için null.
            b.emit("CONST", self._const(TAG_NULL, None), node=node)


# ─── Kolaylık API ────────────────────────────────────────────────────────────

def compile_program(tree: A.Program, filename: str = "") -> Module:
    return Compiler(tree, filename).compile()
