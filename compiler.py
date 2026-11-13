"""
Nex Compiler — AST → bytecode (prototip alt kümesi).

Kapsam (v0.1 prototip): int/float/str/bool/null, print/println/str/int/float/len
native'leri, aritmetik/karşılaştırma, let/atama, if/elif/else, while,
break/continue, for (A.ForStmt: range → while-style tamsayı döngüsü,
list/str/@stack → indeksle yineleme; destructuring hedef reddedilir),
fonksiyon tanımı + çağrı, return, and/or (eager semantik), `@stack`
sabit boyutlu diziler (STACK_ALLOC/STACK_MATERIALIZE, indeks okuma/yazma,
kaçışta derin heap kopyası), `@own` sahipliği (OWN_ALLOC/OWN_MOVE/
OWN_FREE; borrow yok), genel list/dict literalleri (NEW_LIST/NEW_DICT +
GET_INDEX/SET_INDEX) ve f-string (`FStringExpr`). `list.push` metodu
CALL_NATIVE push ile desteklenir; sınıf metotları GET_MEMBER + CALL_BOUND
ile çağrılır.

Sınıflar (stage 2, NEX_CLASS_DESIGN.md): modül seviyesinde `class` kabul
edilir. Metotlar gerçek fonksiyonlar olarak derlenir; `self` örtük ilk
parametredir (CALL_BOUND `[instance]+args` ile eşleşir). Örnekleme:
NEW_INSTANCE + alan varsayılanları + `init` çağrısı (init yoksa argümanlar
değerlendirilip atılır). Alan okuma/yazma GET_MEMBER/SET_MEMBER, metot
çağrısı GET_MEMBER + CALL_BOUND. Kapsam dışı (bilinçli): kalıtım, interface,
generic sınıf/metot, `__init__` (yalnız `init`), dinamik alan ekleme,
dict-benzeri instance API'si, karmaşık alan varsayılanları.

Kapsam dışı (bilinçli): @raw, nested @stack, `str` elemanlı stack dizi,
tuple, match, for'da destructuring, import, lambda, pipe; ayrıca
list/dict üzerinde compound atama ve diğer metotlar (`.length`, `.pop`,
...). Bunlar açık CompileError üretir — sessizce yanlış kod üretilmez.

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
    ClassInfo, FunctionCode, Instr, Module, NativeInfo, OPCODES,
    NAT_PURE, NAT_STRUCTURAL, MODE_TEMP, MODE_STACK, MODE_OWN, MODE_BORROW,
    MODE_RAW,
    TAG_FLOAT, TAG_INT, TAG_NAME, TAG_NULL, TAG_BOOL, TAG_STR, TAG_TYPE,
    TAG_CLASS_REF, TAG_FUNC_REF,
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
    # push: impure (list'i mutasyona uğratır) → NAT_PURE yok; derleyici
    # bunu CALL_NATIVE ile kodlar (CALL_PURE_NATIVE verifier'da reddedilir).
    NativeInfo("push", 2, 0),
    # println: print ile aynı (variadic, saf; ikisi de trailing newline koyar).
    NativeInfo("println", 0xFFFFFFFF, NAT_PURE),
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

#: Sınıf alanı varsayılanı olarak kabul edilen basit literal biçimleri
#: (NEX_CLASS_DESIGN.md §2 "alan varsayılanları"): int/float/str/bool/null
#: literal'leri ve negatif sayılar (`-1`, `-2.5`). Karmaşık ifadeler
#: (ör. `x + 1`, fonksiyon çağrısı, liste literal'i) CompileError üretir.
def _is_simple_field_default(node: A.Node) -> bool:
    if isinstance(node, (A.IntLit, A.FloatLit, A.StrLit, A.BoolLit, A.NullLit)):
        return True
    if isinstance(node, A.UnaryExpr) and node.op == "-":
        return isinstance(node.operand, (A.IntLit, A.FloatLit))
    return False


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
        #: Sınıf adı → sınıf indeksi (bildirim sırası; ClassTable sırası).
        self._class_index: dict[str, int] = {}
        #: Sınıf adı → ClassDecl (alan sırası, metot listesi).
        self._class_decls: dict[str, A.ClassDecl] = {}
        #: Bildirim sırasına göre sınıflar (cls_index = pozisyon).
        self._class_order: list[A.ClassDecl] = []
        #: Sınıf adı → metot adı → fonksiyon indeksi.
        self._class_methods: dict[str, dict[str, int]] = {}
        #: Sınıf adı → 'init' metotu var mı.
        self._class_init: dict[str, bool] = {}
        #: Derlenen metot gövdesinin ait olduğu sınıf (None → normal fn).
        self._current_class: Optional[str] = None
        #: Slot → sınıf adı ipucu (instance tipi çıkarımı; push/bound ayrımı).
        self._local_class: dict[int, str] = {}
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
        script_stmts: list[A.Node] = []
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
            elif isinstance(stmt, A.ClassDecl):
                # Sınıf: iki geçişli toplama (forward ref parity — tree-walker
                # Program'ı önce tüm ClassDecl'leri yürütür).
                self._collect_class(stmt)
            elif isinstance(stmt, (A.InterfaceDecl, A.ImportStmt,
                                   A.ModuleStmt)):
                raise CompileError(
                    f"{type(stmt).__name__} is not supported in the VM prototype",
                    stmt)
            else:
                # Üst seviye statement: sentetik `<script>` girişine taşınır
                # (aşağıda). Ad `<script>` geçerli bir tanımlayıcı olmadığı
                # için kullanıcı fonksiyonlarıyla çakışamaz.
                script_stmts.append(stmt)

        if not self._func_decls and not script_stmts and not self._class_order:
            raise CompileError("no functions to compile")

        # Üst seviye statement'lar varsa sentetik giriş fonksiyonu: dosya
        # sırasıyla gövdeye konur, kullanıcı fonksiyonları serbestçe
        # çağrılabilir (tree-walker semantiği). `fn main` tanımlıysa ikisi
        # de yaşar; VM girişi sentetik `<script>`'tir. Statement yoksa
        # eski davranış korunur: giriş `fn main` (zorunlu).
        script_decl: Optional[A.FnDecl] = None
        if script_stmts:
            first = script_stmts[0]
            script_decl = A.FnDecl(
                name="<script>", type_params=[], params=[], return_type=None,
                body=A.Block(stmts=script_stmts, line=first.line, col=first.col),
                line=first.line, col=first.col)

        # Native tablo
        self.module.natives = list(NATIVES)
        for nat in self.module.natives:
            self._name_const(nat.name)

        # Fonksiyon adları debug/sembol amaçlı sabit havuzunda olmalı
        # (encode_module function tablosu addan const indeksi yazar).
        for decl in self._func_decls:
            self._name_const(decl.name)
        if script_decl is not None:
            self._name_const(script_decl.name)

        for decl in self._func_decls:
            self.module.functions.append(self._compile_function(decl))

        # Sınıf metotları: her metot gerçek bir fonksiyon olur (modül
        # fonksiyon tablosunda ayrı giriş); `self` örtük ilk parametredir.
        for cls_decl in self._class_order:
            for m in cls_decl.methods:
                self._name_const(m.name)
                fn_index = len(self.module.functions)
                self.module.functions.append(
                    self._compile_method(cls_decl.name, m))
                self._class_methods[cls_decl.name][m.name] = fn_index

        if script_decl is not None:
            # Sentetik giriş en sona: arşiv sırası kullanıcı fonksiyonları
            # önce, `<script>` sonra. Gövde fn main gövdesiyle aynı yolla
            # derlenir (return → RET script'i bitirir; sonda düşen akış
            # null döner). Params boş olduğundan _compile_function'daki
            # param denetimleri tetiklenmez; null-body reddi yalnız yukarıdaki
            # toplama döngüsünde olduğundan sentetik node oraya girmez.
            self.module.functions.append(self._compile_function(script_decl))
            self.module.entry = len(self.module.functions) - 1
        else:
            if "main" not in self._func_index:
                raise CompileError("VM prototype requires a 'main' function")
            self.module.entry = self._func_index["main"]

        # Sınıf tablosu (ClassTable): alan sırası = bildirim sırası,
        # metot girdileri (ad const, FUNC_REF const).
        self._build_class_table()
        return self.module

    # -- sınıf toplama/tablolar ------------------------------------------------
    def _collect_class(self, decl: A.ClassDecl) -> None:
        """Sınıf bildirimini doğrula ve topla (iki geçişli derleme).

        Sınıf adı → cls_index eşlemesi bildirim sırasına göre verilir
        (ClassTable sırası). Desteklenmeyen sınıf özellikleri (kalıtım,
        interface, generic, `__init__`, karmaşık alan varsayılanı) konumlu
        CompileError üretir."""
        if decl.name in self._class_index:
            raise CompileError(f"duplicate class '{decl.name}'", decl)
        if decl.name in self._func_index:
            raise CompileError(
                f"'{decl.name}' is declared both as a class and a function",
                decl)
        if decl.base is not None:
            raise CompileError(
                "class inheritance is not supported in the VM prototype", decl)
        if decl.interfaces:
            raise CompileError(
                "class interfaces are not supported in the VM prototype", decl)
        if decl.type_params:
            raise CompileError(
                "generic classes are not supported in the VM prototype", decl)
        for f in decl.fields:
            if f.default is not None and not _is_simple_field_default(f.default):
                raise CompileError(
                    "complex field default expressions are not supported in "
                    "the VM prototype", f.default)
        seen: set[str] = set()
        for m in decl.methods:
            if m.name == "__init__":
                # Kilit karar: `init` tek kurucudur.
                raise CompileError("__init__ is not supported; use 'init'", m)
            if m.name in seen:
                raise CompileError(f"duplicate method '{m.name}'", m)
            seen.add(m.name)
            if m.type_params:
                raise CompileError(
                    "generic methods are not supported in the VM prototype", m)
            if m.is_async:
                raise CompileError(
                    "async methods are not supported in the VM prototype", m)
            if m.body is None:
                raise CompileError(f"method '{m.name}' has no body", m)
            for p in m.params:
                if p.name == "self":
                    raise CompileError(
                        "duplicate 'self' parameter: methods bind 'self' "
                        "implicitly", p)
        self._class_index[decl.name] = len(self._class_order)
        self._class_order.append(decl)
        self._class_decls[decl.name] = decl
        self._class_methods[decl.name] = {}
        self._class_init[decl.name] = any(m.name == "init" for m in decl.methods)

    def _build_class_table(self) -> None:
        """ClassInfo üret: ad const, alan listesi (sıralı), metot listesi."""
        for cls_decl in self._class_order:
            name_idx = self._name_const(cls_decl.name)
            field_idxs = [self._name_const(f.name) for f in cls_decl.fields]
            methods: list[tuple[int, int]] = []
            for m in cls_decl.methods:
                mname_idx = self._name_const(m.name)
                fn_index = self._class_methods[cls_decl.name][m.name]
                methods.append((mname_idx, self._const(TAG_FUNC_REF, fn_index)))
            self.module.classes.append(ClassInfo(
                name_idx=name_idx,
                field_count=len(field_idxs),
                field_name_idxs=field_idxs,
                methods=methods,
            ))

    def _class_has_method(self, class_name: str, member: str) -> bool:
        decl = self._class_decls.get(class_name)
        if decl is None:
            return False
        return any(m.name == member for m in decl.methods)

    # -- fonksiyon -----------------------------------------------------------
    def _compile_function(self, decl: A.FnDecl,
                          implicit_self: bool = False) -> FunctionCode:
        """Fonksiyon (veya sınıf metodu) derle.

        `implicit_self=True` → metot: `self` örtük ilk parametredir, arity
        = len(params)+1 (CALL_BOUND `[instance]+args` sözleşmesi)."""
        arity = len(decl.params) + (1 if implicit_self else 0)
        b = _FnBuilder(decl.name, arity=arity)
        self._builder = b
        self._loops = []
        self._local_class = {}

        if implicit_self:
            for p in decl.params:
                if p.name == "self":
                    raise CompileError(
                        "duplicate 'self' parameter: methods bind 'self' "
                        "implicitly", p)
            # `self` normal bir yerel slot (MODE_TEMP).
            b.declare("self", decl)

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
            slot = b.declare(p.name, p)
            # Sınıf tipi parametre → instance ipucu (push/bound ayrımı).
            ann = p.type_ann
            if isinstance(ann, A.TypeName) and ann.name in self._class_index:
                self._local_class[slot] = ann.name

        self._compile_block(decl.body, new_scope=False)
        # Düşen akış: null dön.
        b.emit("CONST", self._const(TAG_NULL, None), node=decl)
        b.emit("RET", node=decl)

        self._builder = None
        return b.assemble()

    def _compile_method(self, class_name: str, decl: A.FnDecl) -> FunctionCode:
        """Sınıf metodunu derle (`self` örtük ilk parametre).

        Metot, serbest fonksiyon gibi `FuncInfo` olur; gövdesi derlenirken
        `self` slotu (0) hazırdır. Arity = len(params)+1 → CALL_BOUND
        `[instance]+args` ile birebir eşleşir (tree-walker `Function._call`
        örtük `self` bağlama sözleşmesiyle aynı)."""
        saved = self._current_class
        self._current_class = class_name
        try:
            return self._compile_function(decl, implicit_self=True)
        finally:
            self._current_class = saved

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
            if node.mem_mode == "raw":
                if node.value is None:
                    raise CompileError(
                        f"@raw variable '{node.name}' requires an initializer", node)
                slot = b.declare(node.name, node, MODE_RAW)
                self._compile_expr(node.value)
                b.emit("RAW_MAKE", slot, node=node)
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
            # `let x = Foo(...)` → x bir sınıf örneği (instance ipucu).
            ctor = self._ctor_class_of(node.value)
            if ctor is not None:
                self._local_class[slot] = ctor
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

        if isinstance(node, A.ForStmt):
            self._compile_for(node)
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

        if isinstance(node, A.ClassDecl):
            # Sınıf yalnız modül seviyesinde tanımlanabilir.
            raise CompileError(
                "class declarations inside functions are not supported in "
                "the VM prototype", node)

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

    def _compile_for(self, node: A.ForStmt) -> None:
        """`for item in iterable:` — while makinesiyle aynı ödünç alma
        semantiği (self._loops = (end, continue=incr, base)).

        - RangeExpr (`0..10`): host range nesnesi YOK; start/end bir kez
          temp slot'lara alınır, tamsayı sayaç `counter < end` ile döner.
        - Diğer iterable'lar (list/str, `@stack` Ident yolu zaten
          STACK_MATERIALIZE ile kopya üretir — tree-walker to_list kopya
          semantiği) İNDEKSLE döner: `_iter`/`_i`/`_len` temp slot'ları;
          `len` bir kez CALL_PURE_NATIVE ile hesaplanır; her turda
          `LOAD _iter; LOAD _i; GET_INDEX → hedef slot`.
        - Döngü kapsamı: push_scope → pop_scope; hedef yalnız gövdede
          görünür. break/continue gövde borrow'larını döngü tabanına
          kadar release eder (while ile aynı); continue etiketi ARTIŞ
          adımına iner (koşula değil, yoksa sonsuz döngü)."""
        b = self._builder
        if not isinstance(node.target, A.Ident):
            raise CompileError(
                "destructuring in for loops is not supported in the VM "
                "prototype", node.target)

        is_range = isinstance(node.iterable, A.RangeExpr)
        cond = b.new_label()
        incr = b.new_label()
        end = b.new_label()

        if is_range:
            self._compile_expr(node.iterable.start)
            start_slot = b.new_tmp(node)
            b.emit("STORE_LOCAL", start_slot, node=node)
            self._compile_expr(node.iterable.end)
            end_slot = b.new_tmp(node)
            b.emit("STORE_LOCAL", end_slot, node=node)
            b.emit("LOAD_LOCAL", start_slot, node=node)
            counter_slot = b.new_tmp(node)
            b.emit("STORE_LOCAL", counter_slot, node=node)
            cond_a, cond_b = counter_slot, end_slot
        else:
            # İterable bir kez değerlendirilir (tree-walker sequence kopyası).
            self._compile_expr(node.iterable)
            iter_slot = b.new_tmp(node)
            b.emit("STORE_LOCAL", iter_slot, node=node)
            b.emit("CONST", self._const(TAG_INT, 0), node=node)
            i_slot = b.new_tmp(node)
            b.emit("STORE_LOCAL", i_slot, node=node)
            b.emit("LOAD_LOCAL", iter_slot, node=node)
            b.emit("CALL_PURE_NATIVE", _NATIVE_INDEX["len"], 1, node=node)
            len_slot = b.new_tmp(node)
            b.emit("STORE_LOCAL", len_slot, node=node)
            cond_a, cond_b = i_slot, len_slot

        # Döngü kapsamı: hedef yalnız gövdede görünür.
        base = len(b._scope_borrows)
        b.push_scope()
        target_slot = b.declare(node.target.name, node.target)
        self._loops.append((end, incr, base))
        b.mark(cond)
        b.emit("LOAD_LOCAL", cond_a, node=node)
        b.emit("LOAD_LOCAL", cond_b, node=node)
        b.emit("LT", node=node)
        b.emit("JUMP_IF_FALSE", end, node=node)
        # item: range → sayaç değeri; diğer → _iter[_i].
        if is_range:
            b.emit("LOAD_LOCAL", counter_slot, node=node)
        else:
            b.emit("LOAD_LOCAL", iter_slot, node=node)
            b.emit("LOAD_LOCAL", i_slot, node=node)
            b.emit("GET_INDEX", node=node)
        b.emit("STORE_LOCAL", target_slot, node=node)
        self._compile_block(node.body, new_scope=True)
        b.mark(incr)
        step_slot = counter_slot if is_range else i_slot
        b.emit("LOAD_LOCAL", step_slot, node=node)
        b.emit("CONST", self._const(TAG_INT, 1), node=node)
        b.emit("ADD", node=node)
        b.emit("STORE_LOCAL", step_slot, node=node)
        b.emit("JUMP", cond, node=node)
        b.mark(end)
        self._loops.pop()
        b.pop_scope()

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

        if isinstance(node, A.MemberExpr):
            # Üye okuma (çağrı olmayan bağlam): `obj` değerlendir, GET_MEMBER
            # (alan önce, metot sonra; metot okuması bound üretir).
            self._compile_member_read(node)
            return

        if isinstance(node, A.FStringExpr):
            # f-string: parçaları soldan sağa birleştir. Literal parça → CONST STR;
            # ifade parçası → expr + CALL_PURE_NATIVE str 1. Parça yoksa "".
            if not node.parts:
                b.emit("CONST", self._const(TAG_STR, ""), node=node)
                return
            for i, part in enumerate(node.parts):
                if isinstance(part, str):
                    b.emit("CONST", self._const(TAG_STR, part), node=node)
                else:
                    self._compile_expr(part)
                    b.emit("CALL_PURE_NATIVE", _NATIVE_INDEX["str"], 1, node=node)
                if i > 0:
                    b.emit("ADD", node=node)
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
                # Yeniden atama → instance ipucu düşer (tip artık belirsiz).
                self._local_class.pop(slot, None)
                return
            if isinstance(node.target, A.IndexExpr):
                self._compile_index_assign(node)
                return
            if isinstance(node.target, A.MemberExpr):
                self._compile_member_assign(node)
                return
            raise CompileError(
                "only variable, index and member assignment are supported in "
                "the VM prototype", node)

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
        if isinstance(node.callee, A.MemberExpr):
            # `obj.push(v)` (liste native'i) veya `obj.method(args)`
            # (sınıf metotu → GET_MEMBER + CALL_BOUND).
            self._compile_member_call(node)
            return
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

        # Sınıf örnekleme: `Foo(args...)` → NEW_INSTANCE + alan varsayılanları
        # + `init` çağrısı (init yoksa argümanlar değerlendirilip atılır).
        if name in self._class_index:
            self._compile_constructor_call(node, name)
            return

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

        # İsim çözümleme: kullanıcı fonksiyonu ÖNCE, native SONRA. Impure
        # native (push) CALL_NATIVE ister: CALL_PURE_NATIVE verifier'da
        # "not pure" ile reddedilir.
        if name in self._func_index:
            b.emit("CALL", self._func_index[name], len(node.args), node=node)
        elif name in _NATIVE_INDEX:
            nat = NATIVES[_NATIVE_INDEX[name]]
            opcode = ("CALL_PURE_NATIVE"
                      if nat.pure and not nat.retains_ref else "CALL_NATIVE")
            b.emit(opcode, _NATIVE_INDEX[name], len(node.args), node=node)
        else:
            raise CompileError(f"undefined function '{name}'", node)
        for tmp in borrow_temps:
            b.emit("BORROW_END", tmp, node=node)

    def _compile_member_call(self, node: A.CallExpr) -> None:
        """`obj.push(v)` — list.push native'i (CALL_NATIVE, argc=2).

        `obj.method(args)` — sınıf metotu: `obj` değerlendir, GET_MEMBER
        (bound üret), argümanları değerlendir, CALL_BOUND argc.

        Alıcı bir sınıf örneği olarak biliniyorsa (statik ipucu) daima bound
        çağrı yolu kullanılır; aksi hâlde `push` liste native'ine düşer
        (prototipteki tek liste metot çağrısı)."""
        b = self._builder
        assert isinstance(node.callee, A.MemberExpr)
        member = node.callee.member
        if node.type_args:
            raise CompileError("call-site type args are not supported in VM",
                               node)
        receiver_class = self._infer_instance_class(node.callee.obj)
        if receiver_class is None and member == "push":
            if len(node.args) != 1:
                raise CompileError("push() accepts exactly one argument", node)
            self._compile_expr(node.callee.obj)      # [obj]
            self._compile_expr(node.args[0])         # [obj, val]
            b.emit("CALL_NATIVE", _NATIVE_INDEX["push"], 2, node=node)
            return
        # Bound metot çağrısı: obj; GET_MEMBER; args; CALL_BOUND.
        self._compile_expr(node.callee.obj)          # [obj]
        b.emit("GET_MEMBER", self._name_const(member), node=node)  # [bound]
        for arg in node.args:
            self._compile_expr(arg)                  # [bound, a1..an]
        b.emit("CALL_BOUND", 0, len(node.args), node=node)   # [ret]

    # -- sınıf yardımcıları ----------------------------------------------------
    def _ctor_class_of(self, value: Optional[A.Node]) -> Optional[str]:
        """Değer bir sınıf örnekleme çağrısı mı? → sınıf adı."""
        if not isinstance(value, A.CallExpr):
            return None
        if not isinstance(value.callee, A.Ident):
            return None
        name = value.callee.name
        if name not in self._class_index:
            return None
        if self._builder.resolve(name) is not None:
            return None
        return name

    def _infer_instance_class(self, expr: A.Node) -> Optional[str]:
        """(statik ipucu) expr bir sınıf örneği mi? → sınıf adı.

        Yalnız kanıtlanabilir durumlar: `self` (metot gövdesinde), bir
        sınıf örneklemesine bağlanmış yerel, sınıf tipi parametre ve alan
        tipi bildirimi bir sınıfı gösteren üye erişimi."""
        if isinstance(expr, A.Ident):
            if expr.name == "self":
                return self._current_class
            slot = self._builder.resolve(expr.name)
            if slot is None:
                return None
            return self._local_class.get(slot)
        if isinstance(expr, A.MemberExpr):
            owner = self._infer_instance_class(expr.obj)
            if owner is None:
                return None
            decl = self._class_decls.get(owner)
            if decl is None:
                return None
            for f in decl.fields:
                if f.name == expr.member:
                    ta = f.type_ann
                    if isinstance(ta, A.TypeName) and ta.name in self._class_index:
                        return ta.name
                    return None
            return None
        return None

    def _compile_member_read(self, node: A.MemberExpr) -> None:
        """`obj.field` → obj; GET_MEMBER (alan → değer, metot → bound)."""
        b = self._builder
        self._compile_expr(node.obj)
        b.emit("GET_MEMBER", self._name_const(node.member), node=node)

    def _compile_member_assign(self, node: A.AssignExpr) -> None:
        """`obj.field = v` → obj; v; SET_MEMBER.

        Yalnız bildirilmiş alanlar yazılabilir (kilit karar §6c): alıcının
        sınıfı statik olarak biliniyorsa derleme zamanında reddedilir;
        bilinmiyorsa runtime reddi (VM) geçerlidir. Atama ifadesi değeri
        yığında bırakılır (tree-walker `set_attr` dönüşü ile aynı)."""
        b = self._builder
        target = node.target
        assert isinstance(target, A.MemberExpr)
        cls_name = self._infer_instance_class(target.obj)
        if cls_name is not None:
            decl = self._class_decls[cls_name]
            if target.member not in {f.name for f in decl.fields}:
                if self._class_has_method(cls_name, target.member):
                    raise CompileError(
                        f"cannot assign to method '{target.member}' of class "
                        f"'{cls_name}'", node)
                raise CompileError(
                    f"'{cls_name}' has no field '{target.member}'", node)
        tmp = b.new_tmp(node)
        self._compile_expr(node.value)          # [v]
        b.emit("DUP", node=node)                # [v, v]
        b.emit("STORE_LOCAL", tmp, node=node)   # [v]
        self._compile_expr(target.obj)          # [v, obj]
        b.emit("LOAD_LOCAL", tmp, node=node)    # [v, obj, v]
        b.emit("SET_MEMBER", self._name_const(target.member), node=node)  # [v]

    def _compile_constructor_call(self, node: A.CallExpr,
                                  class_name: str) -> None:
        """`Foo(args...)` → NEW_INSTANCE + varsayılanlar + `init` çağrısı.

        - NEW_INSTANCE `<cls>`; STORE_LOCAL tmp (geçici TEMP slot).
        - Bildirim sırasındaki alan varsayılanları SET_MEMBER ile yazılır
          (yalnız basit literal'ler; varsayılanı olmayan alan None kalır).
        - `init` varsa: bound `init` çağrılır (self + args); init yoksa
          argümanlar değerlendirilip yan etkileri için atılır (tree-walker
          semantiği: argümanlar yine de değerlendirilir, göze alınmaz).
        - Yığında örnek bırakılır."""
        b = self._builder
        cls_idx = self._class_index[class_name]
        cls_decl = self._class_decls[class_name]
        b.emit("NEW_INSTANCE", self._const(TAG_CLASS_REF, cls_idx), node=node)
        tmp = b.new_tmp(node)
        b.emit("STORE_LOCAL", tmp, node=node)
        for f in cls_decl.fields:
            if f.default is None:
                continue
            b.emit("LOAD_LOCAL", tmp, node=node)          # [obj]
            self._compile_expr(f.default)                 # [obj, val]
            b.emit("SET_MEMBER", self._name_const(f.name), node=node)
        if self._class_init.get(class_name):
            b.emit("LOAD_LOCAL", tmp, node=node)          # [obj]
            b.emit("GET_MEMBER", self._name_const("init"), node=node)  # [bnd]
            for arg in node.args:
                self._compile_expr(arg)                  # [bnd, a1..an]
            b.emit("CALL_BOUND", 0, len(node.args), node=node)  # [ret]
            b.emit("POP", node=node)                     # []
            b.emit("LOAD_LOCAL", tmp, node=node)         # [obj]
        else:
            for arg in node.args:
                self._compile_expr(arg)                  # [a1..an]
                b.emit("POP", node=node)                 # []
            b.emit("LOAD_LOCAL", tmp, node=node)         # [obj]

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
