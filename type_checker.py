"""
Nex Language — Type Checker
AST'yi dolaşarak statik tip analizi yapar.
Visitor pattern + scope stack + inference.
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import Optional
from ast_nodes import *
from nex_types import *


# ─── Hata / Uyarı ─────────────────────────────────────────────────────────────

@dataclass
class TypeDiag:
    kind:    str        # "error" | "warning" | "info"
    message: str
    line:    int
    col:     int
    file:    str = ""

    def __str__(self):
        icon = {"error": "✗", "warning": "⚠", "info": "ℹ"}[self.kind]
        loc = f"{self.file}:" if self.file else ""
        return f"  {icon} [{self.kind.upper()}] {loc}{self.line}:{self.col} — {self.message}"


# ─── Sembol Tablosu ───────────────────────────────────────────────────────────

class Scope:
    def __init__(self, parent: Optional[Scope] = None, name: str = "<scope>"):
        self.parent  = parent
        self.name    = name
        self.symbols: dict[str, NexType] = {}
        self.mutability: dict[str, bool] = {}  # False = const
        self.mem_modes: dict[str, Optional[str]] = {}  # None=GC, "own"/"raw"/"stack"
        # Taşınmış (moved) @own değişken adları. Akış-duyarsız/lineer izleme:
        # bir move sonrası aynı blokta ad yeniden tanımlanana kadar "moved"
        # kalır; dallar (if/loop) arası akış analizi bu adımda yok (bkz. §5).
        self.moved: set[str] = set()

    def define(self, name: str, typ: NexType, mutable: bool = True,
               mem_mode: Optional[str] = None):
        self.symbols[name]    = typ
        self.mutability[name] = mutable
        self.mem_modes[name]  = mem_mode
        self.moved.discard(name)   # yeniden tanımlama moved durumunu sıfırlar

    def lookup(self, name: str) -> Optional[NexType]:
        if name in self.symbols:
            return self.symbols[name]
        if self.parent:
            return self.parent.lookup(name)
        return None

    def lookup_local(self, name: str) -> Optional[NexType]:
        return self.symbols.get(name)

    def is_mutable(self, name: str) -> bool:
        if name in self.mutability:
            return self.mutability[name]
        if self.parent:
            return self.parent.is_mutable(name)
        return True

    def update(self, name: str, typ: NexType) -> bool:
        if name in self.symbols:
            self.symbols[name] = typ
            return True
        if self.parent:
            return self.parent.update(name, typ)
        return False

    def lookup_mem_mode(self, name: str) -> Optional[str]:
        if name in self.mem_modes:
            return self.mem_modes[name]
        if self.parent:
            return self.parent.lookup_mem_mode(name)
        return None

    def mark_moved(self, name: str) -> None:
        """Adı tanımlayan scope'ta moved işaretle (gölgeleme/scope çıkışı
        dış scope'taki sahiplik durumunu bozmasın)."""
        if name in self.symbols:
            self.moved.add(name)
        elif self.parent:
            self.parent.mark_moved(name)

    def is_moved(self, name: str) -> bool:
        """Adı tanımlayan scope'a kadar bak; dış scope'ta aynı isimli
        gölgelenmiş değişkenin moved durumu içtekini etkilemez."""
        if name in self.symbols:
            return name in self.moved
        if self.parent:
            return self.parent.is_moved(name)
        return False

    def clear_moved(self, name: str) -> None:
        """Başarılı @own yeniden-ataması (to_own sonucu) moved durumunu
        temizler: sahiplik yaşam döngüsü sıfırlanır (Iceman incelemesi,
        bug B1)."""
        if name in self.symbols:
            self.moved.discard(name)
        elif self.parent:
            self.parent.clear_moved(name)


# ─── Type Checker ─────────────────────────────────────────────────────────────

class TypeChecker:

    def __init__(self, filename: str = ""):
        self.filename = filename
        self.diagnostics: list[TypeDiag] = []
        self.global_scope = Scope(name="<global>")
        self.current_scope = self.global_scope

        # Fonksiyon bağlamı
        self.fn_return_type: Optional[NexType] = None
        self.fn_is_async: bool = False

        # Sınıf bağlamı
        self.current_class: Optional[ClassType] = None

        # Döngü derinliği (break/continue kontrolü)
        self.loop_depth: int = 0

        # Tip parametreleri (generic bağlamı)
        self.type_env: dict[str, NexType] = {}

        # Tip kayıtları (class / interface tanımları)
        self.type_registry: dict[str, NexType] = {}

        self._setup_builtins()

    # ── Builtins ──────────────────────────────────────────────────

    def _setup_builtins(self):
        # Temel fonksiyonlar
        print_fn  = FnType([UNKNOWN], VOID)
        input_fn  = FnType([STR], STR)
        len_fn    = FnType([UNKNOWN], INT)
        range_fn  = FnType([INT, INT], ListType(INT))
        min_fn    = FnType([UNKNOWN], UNKNOWN)
        max_fn    = FnType([UNKNOWN], UNKNOWN)
        sqrt_fn   = FnType([FLOAT], FLOAT)
        str_fn    = FnType([UNKNOWN], STR)
        int_fn    = FnType([UNKNOWN], INT)
        float_fn  = FnType([UNKNOWN], FLOAT)
        bool_fn   = FnType([UNKNOWN], BOOL)
        alloc_fn  = FnType([INT], UNKNOWN)
        free_fn   = FnType([UNKNOWN], VOID)
        sizeof_fn = FnType([UNKNOWN], INT)
        # GC ⇄ @own geçişleri (§2): gerçek imzalar visit_CallExpr'te özel
        # ele alınır; buradaki FnType yalnızca ismin tanımlı olması için.
        to_own_fn = FnType([UNKNOWN], UNKNOWN)
        to_gc_fn  = FnType([UNKNOWN], UNKNOWN)

        builtins = {
            "print": print_fn, "println": print_fn,
            "input": input_fn,
            "len": len_fn, "length": len_fn,
            "range": range_fn,
            "min": min_fn, "max": max_fn,
            "sqrt": sqrt_fn,
            "str": str_fn, "int": int_fn,
            "float": float_fn, "bool": bool_fn,
            "alloc": alloc_fn, "free": free_fn, "sizeof": sizeof_fn,
            "to_own": to_own_fn, "to_gc": to_gc_fn,
            # Logical operators - built-in olarak tanımla
            "and": FnType([BOOL, BOOL], BOOL),
            "or": FnType([BOOL, BOOL], BOOL),
            "not": FnType([BOOL], BOOL),
        }
        for name, typ in builtins.items():
            self.global_scope.define(name, typ)

    # ── Scope yönetimi ────────────────────────────────────────────

    def push_scope(self, name: str = "<block>") -> Scope:
        self.current_scope = Scope(self.current_scope, name)
        return self.current_scope

    def pop_scope(self):
        self.current_scope = self.current_scope.parent

    # ── Hata / Uyarı ─────────────────────────────────────────────

    def error(self, msg: str, node: Node):
        self.diagnostics.append(TypeDiag("error", msg, node.line, node.col, self.filename))

    def warn(self, msg: str, node: Node):
        self.diagnostics.append(TypeDiag("warning", msg, node.line, node.col, self.filename))

    def info(self, msg: str, node: Node):
        self.diagnostics.append(TypeDiag("info", msg, node.line, node.col, self.filename))

    @property
    def errors(self) -> list[TypeDiag]:
        return [d for d in self.diagnostics if d.kind == "error"]

    @property
    def warnings(self) -> list[TypeDiag]:
        return [d for d in self.diagnostics if d.kind == "warning"]

    # ── Tip Çözümleme (AST node → NexType) ───────────────────────

    def resolve_type_node(self, node: Node) -> NexType:
        if node is None:
            return UNKNOWN

        if isinstance(node, TypeName):
            # Primitive?
            if node.name in PRIMITIVES:
                return PRIMITIVES[node.name]
            # Kayıtlı tip?
            if node.name in self.type_registry:
                return self.type_registry[node.name]
            # Generic type parameter?
            if node.name in self.type_env:
                return self.type_env[node.name]
            self.diagnostics.append(TypeDiag("error", f"Unknown type '{node.name}'",
                                              node.line, node.col, self.filename))
            return UNKNOWN

        if isinstance(node, TypeNullable):
            inner = self.resolve_type_node(node.inner)
            return NullableType(inner)

        if isinstance(node, TypeGeneric):
            params = [self.resolve_type_node(p) for p in node.params]
            if node.base == "List":
                return ListType(params[0]) if params else ListType(UNKNOWN)
            if node.base == "Dict":
                return DictType(params[0], params[1]) if len(params) >= 2 else DictType(UNKNOWN, UNKNOWN)
            if node.base == "Set":
                return SetType(params[0]) if params else SetType(UNKNOWN)
            if node.base == "Result":
                return ResultType(params[0]) if params else ResultType(UNKNOWN)
            if node.base in self.type_registry:
                return self.type_registry[node.base]
            return UNKNOWN

        if isinstance(node, TypeTuple):
            return TupleType([self.resolve_type_node(e) for e in node.elements])

        if isinstance(node, TypeFn):
            p = [self.resolve_type_node(x) for x in node.params]
            r = self.resolve_type_node(node.returns) if node.returns else VOID
            return FnType(p, r)

        if isinstance(node, TypeRef):
            return RefType(self.resolve_type_node(node.inner), node.mutable)

        if isinstance(node, TypeStackArray):
            elem = self.resolve_type_node(node.elem)
            if not isinstance(node.size, IntLit):
                self.error("Stack array size must be an integer literal", node)
                return StackArrayType(elem, 0)
            n = int(node.size.value)
            if n < 1:
                self.error("Stack array size must be at least 1", node)
                return StackArrayType(elem, 0)
            return StackArrayType(elem, n)

        return UNKNOWN

    # ── Tip uyumluluğu ────────────────────────────────────────────

    def check_assign(self, actual: NexType, expected: NexType, node: Node, ctx: str = ""):
        if not is_subtype(actual, expected):
            msg = f"Type mismatch{' in ' + ctx if ctx else ''}: expected '{expected}', got '{actual}'"
            self.error(msg, node)

    def _reject_own_value(self, t: NexType, node: Node, ctx: str) -> None:
        """OwnType üreten ifade yalnızca @own bildirim/atama RHS'inde meşrudur.
        Diğer bağlamlarda own değer GC tarafına sızar ve free edilemez; bu
        adımda taşıma yalnızca to_gc(x) ile mümkündür (NEX_MEMORY_MODEL.md §2)."""
        if isinstance(t, OwnType):
            self.error(
                f"cannot use @own value as {ctx}; move it with to_gc(x) first",
                node)

    # ── Ana Ziyaretçi ─────────────────────────────────────────────

    def visit(self, node: Node) -> NexType:
        if node is None:
            return VOID
        prev_hint = getattr(self, "_list_hint", None)
        self._list_hint = None
        try:
            method = f"visit_{type(node).__name__}"
            if hasattr(self, method):
                return getattr(self, method)(node)
            # Bilinmeyen node'u sessizce geç
            return UNKNOWN
        finally:
            self._list_hint = prev_hint

    def visit_PipeExpr(self, node: PipeExpr) -> NexType:
        """buf |> f — sol taraf (@own/borrow) fonksiyona sessizce sızmamalı.

        Pipe, sol işleneni doğrudan argüman gibi geçirdiği için fonksiyon
        sınırı kuralları (v1) burada da uygulanır:
        - @own değer taşınmadan geçemez (to_gc ile açıkça taşınmalı)
        - borrow referansı sınırı geçemez (§4/§5 v1 kapsamı)"""
        left_t = self.visit(node.left)
        self._reject_own_value(left_t, node.left, "pipe operand")
        if isinstance(node.left, BorrowExpr):
            self.error(
                "borrowed references cannot cross function boundaries yet (v1)",
                node.left)
        self.visit(node.right)
        return UNKNOWN

    # ── Program & Block ───────────────────────────────────────────

    def visit_Program(self, node: Program) -> NexType:
        # İki geçiş: önce tüm üst-düzey sınıf/fonksiyon isimlerini kaydet,
        # sonra gövdelerini kontrol et. Forward-reference desteği.
        self._pre_declare(node.stmts)
        for stmt in node.stmts:
            self.visit(stmt)
        return VOID

    def _pre_declare(self, stmts: list[Node]):
        """Fonksiyon ve class isimlerini kapsamı ziyaret etmeden önce kaydet."""
        for stmt in stmts:
            if isinstance(stmt, FnDecl):
                fn_type = self._fn_signature(stmt)
                self.current_scope.define(stmt.name, fn_type)
            elif isinstance(stmt, ClassDecl):
                ct = ClassType(stmt.name, type_params=stmt.type_params)
                self.type_registry[stmt.name] = ct
                # Forward-reference uyumu: field ve method imzalarını ön-geçişte
                # de kaydet ki sınıf tanımından ÖNCE kullanım --check'i geçsin.
                # visit_ClassDecl sonradan aynı değerleri tekrar doldurur
                # (idempotent); interface listesi ise tek geçişte eklenir.
                if stmt.base:
                    bt = self.type_registry.get(stmt.base)
                    if bt is not None:
                        ct.base = bt
                for f in stmt.fields:
                    ct.fields[f.name] = self.resolve_type_node(f.type_ann)
                for m in stmt.methods:
                    ct.methods[m.name] = self._fn_signature(m)
                self.current_scope.define(stmt.name, ct)
            elif isinstance(stmt, InterfaceDecl):
                it = InterfaceType(stmt.name)
                self.type_registry[stmt.name] = it
                self.current_scope.define(stmt.name, it)

    def _fn_signature(self, node: FnDecl) -> FnType:
        """Fonksiyon imzasını gövde kontrol etmeden çıkar."""
        saved_env = dict(self.type_env)
        for tp in node.type_params:
            self.type_env[tp] = TypeVar(tp)

        param_types = []
        for p in node.params:
            if p.type_ann:
                pt = self.resolve_type_node(p.type_ann)
            else:
                pt = UNKNOWN
            param_types.append(pt)

        ret = self.resolve_type_node(node.return_type) if node.return_type else UNKNOWN
        if node.is_async and not isinstance(ret, (VoidType, UnknownType)):
            ret = ret  # async fonksiyon aynı tipi döner (await açar)

        self.type_env = saved_env
        return FnType(param_types, ret)

    def visit_Block(self, node: Block) -> NexType:
        # Interpreter her Block için yeni Environment açıyor; buradaki scope da
        # onu aynalasın, yoksa if/for gövdesinde tanımlanan değişken dışarı sızar.
        self.push_scope("block")
        try:
            last = VOID
            for stmt in node.stmts:
                last = self.visit(stmt)
            return last
        finally:
            self.pop_scope()

    # ── Declarations ──────────────────────────────────────────────

    def _escape_to_heap(self, t: Optional[NexType]) -> Optional[NexType]:
        """Kaçış kuralı statik yansıması: StackArray değeri bir GC bağlamına
        taşındığında çalışma zamanında heap listesidir; statik tip de bu
        yüzden StackArrayType yerine ListType olmalı. İç içe yapılarda da
        iç stack boyutları ortadan kalkar (heap kopyası serbesttir)."""
        if t is None:
            return None
        if isinstance(t, StackArrayType):
            return ListType(self._escape_to_heap(t.element))
        if isinstance(t, ListType):
            return ListType(self._escape_to_heap(t.element))
        if isinstance(t, TupleType):
            return TupleType([self._escape_to_heap(e) for e in t.elements])
        if isinstance(t, SetType):
            return SetType(self._escape_to_heap(t.element))
        return t

    def _check_stack_literal(self, lit: ListLit, declared: StackArrayType,
                             node: Node, ctx: str) -> None:
        """@stack literal'i boyut + eleman tipi + iç boyut tek geçişte denetler."""
        if len(lit.elements) != declared.size:
            self.error(
                f"Stack array size mismatch for {ctx}: declared "
                f"[{declared.element}; {declared.size}], got "
                f"{len(lit.elements)} element(s)", node)
            return
        for i, elem in enumerate(lit.elements):
            if isinstance(declared.element, StackArrayType) \
                    and isinstance(elem, ListLit):
                # İç içe stack dizisi: iç satır da kendi boyutuyla denetlenir
                self._check_stack_literal(
                    elem, declared.element, node, f"{ctx}[{i}]")
                continue
            elem_t = self.visit(elem)
            self.check_assign(
                elem_t, declared.element, node, f"{ctx}[{i}] element")

    def _own_decl_type(self, node: VarDecl, declared: Optional[NexType]) -> NexType:
        """@own bildirim tipi: OwnType(inner). Yalnızca to_own(...) sonucu
        (ve ileride başka @own değerinin move'u) başlatabilir; düz GC değeri
        yasaktır (NEX_MEMORY_MODEL.md §2). @stack ile birleşemez (§1)."""
        if isinstance(declared, StackArrayType):
            self.error(
                f"@own cannot be combined with @stack array type for '{node.name}'",
                node)
            if node.value is not None:
                self.visit(node.value)
            return OwnType(UNKNOWN)
        inferred = self.visit(node.value) if node.value is not None else None
        if inferred is None:
            if declared is None:
                self.error(
                    f"@own variable '{node.name}' requires a type annotation or a "
                    f"to_own(...) initializer", node)
                return OwnType(UNKNOWN)
            return OwnType(declared)
        if not isinstance(inferred, OwnType):
            # GC → own yalnızca to_own(x) ile (deep copy); düz atama yasak.
            self.error(
                f"cannot assign GC value to @own variable '{node.name}' "
                f"(use to_own(x))", node)
            return OwnType(declared if declared is not None else inferred)
        if declared is not None:
            self.check_assign(inferred.inner, declared, node,
                              f"declaration of '{node.name}'")
            return OwnType(declared)
        return OwnType(inferred.inner)

    def _define_var(self, node: VarDecl, var_type: NexType) -> None:
        if self.current_scope.lookup_local(node.name):
            self.warn(f"Variable '{node.name}' shadows outer declaration", node)
        self.current_scope.define(node.name, var_type, mutable=node.mutable,
                                  mem_mode=node.mem_mode)

    def visit_VarDecl(self, node: VarDecl) -> NexType:
        declared = self.resolve_type_node(node.type_ann) if node.type_ann else None
        if node.mem_mode == "own":
            self._define_var(node, self._own_decl_type(node, declared))
            return VOID
        if node.mem_mode == "stack":
            # @stack yalnızca literal kabul eder; boyut/eleman tip/iç boyut
            # denetimi tek geçişte _check_stack_literal'da yapılır (aşağıda).
            # Literal zaten visit edilmeden burada denetlenir; çift hata mesajı
            # olmasın diye inferred ön hesaplanmaz.
            inferred = None
            if node.value is not None and not isinstance(node.value, ListLit):
                inferred = self.visit(node.value)
        else:
            # visit() ipucunu sıfırlar; doğrudan literal için burada aç
            if isinstance(node.value, ListLit) and isinstance(declared, StackArrayType):
                prev_hint = getattr(self, "_list_hint", None)
                self._list_hint = declared
                try:
                    inferred = self.visit_ListLit(node.value)
                finally:
                    self._list_hint = prev_hint
            else:
                inferred = self.visit(node.value) if node.value else None
            # Kaçış kuralı: non-stack bağlamda StackArray heap listesine
            # kopyalanır; değişkenin statik tipi de o yüzden ListType olmalı.
            # Aksi halde `let xs = buf; xs.push(3)` checker'ın doğru çalışırdı.
            # (declared zaten [T; N] ise eski davranış korunur.)
            if not isinstance(declared, StackArrayType):
                inferred = self._escape_to_heap(inferred)

        if node.mem_mode == "stack":
            if declared is None or not isinstance(declared, StackArrayType):
                self.error(
                    f"@stack declaration '{node.name}' requires a fixed-size array "
                    f"type such as [int; 4]", node)
                var_type = declared if declared is not None else UNKNOWN
            elif isinstance(node.value, ListLit):
                # Boyut + eleman tipi + iç boyutlar, tek geçişte:
                self._check_stack_literal(node.value, declared, node, f"'{node.name}'")
                var_type = declared
            elif inferred is not None:
                self.error(
                    f"@stack declaration '{node.name}' requires an array literal of "
                    f"{declared.size} element(s)", node)
                var_type = declared
            else:
                var_type = declared
        elif declared and inferred:
            self.check_assign(inferred, declared, node, f"declaration of '{node.name}'")
            var_type = declared
        elif declared:
            var_type = declared
        elif inferred:
            var_type = inferred
        else:
            self.warn(f"Variable '{node.name}' has no type annotation and no initializer", node)
            var_type = UNKNOWN

        # @own olmayan değişkene OwnType bağlanamaz; aksi halde own değer
        # moved takibinden kaçar (mem_mode None). `let x = to_own(...)` yasak.
        self._reject_own_value(
            var_type, node, f"initializer of non-@own variable '{node.name}'")

        self._define_var(node, var_type)
        return VOID

    def visit_MultiVarDecl(self, node: MultiVarDecl) -> NexType:
        value_types = [self.visit(v) for v in node.values]
        # Eşit sayıda olmalı
        if len(node.names) != len(value_types):
            # Tek değer tuple dönüyorsa unpack et
            if len(value_types) == 1 and isinstance(value_types[0], TupleType):
                tup = value_types[0]
                if len(tup.elements) != len(node.names):
                    self.error(
                        f"Cannot unpack {len(tup.elements)} values into {len(node.names)} variables",
                        node)
                    return VOID
                value_types = tup.elements
            else:
                self.error(
                    f"Expected {len(node.names)} values, got {len(value_types)}", node)
                return VOID

        for name, vt in zip(node.names, value_types):
            self._reject_own_value(vt, node, f"value bound to '{name}'")
            self.current_scope.define(name, vt, mutable=node.mutable)
        return VOID

    def visit_FnDecl(self, node: FnDecl) -> NexType:
        # İmza zaten pre-declare'da kaydedildi (top-level için)
        # Ama iç fonksiyonlar için burada da kaydedelim
        fn_type = self._fn_signature(node)
        if not self.current_scope.lookup_local(node.name):
            self.current_scope.define(node.name, fn_type)
        # @stack değer fonksiyona ne değer ne referansla geçemez: kapsamı stack'te kalır.
        # resolve_type_node('&[int; 2]') zaten RefType üretir; o yüzden iç tip RefType da olabilir.
        for p in node.params:
            if p.type_ann is None:
                continue
            resolved = self.resolve_type_node(p.type_ann)
            inner = resolved.inner if isinstance(resolved, RefType) else resolved
            if isinstance(inner, StackArrayType):
                if p.is_ref or isinstance(resolved, RefType):
                    self.error(
                        f"Stack array parameter '{p.name}' in function '{node.name}' "
                        f"cannot be passed by reference", node)
                else:
                    self.error(
                        f"Stack array parameter '{p.name}' in function '{node.name}': "
                        f"@stack arrays are fixed to their declaring scope; pass a "
                        f"list instead", node)

        if node.body is None:
            return VOID   # interface method

        # Generic tip parametrelerini scope'a ekle
        saved_env = dict(self.type_env)
        for tp in node.type_params:
            self.type_env[tp] = TypeVar(tp)

        # Fonksiyon scope'u
        self.push_scope(f"fn:{node.name}")

        # self / super — class içindeyse otomatik ekle
        if self.current_class is not None:
            self.current_scope.define("self", self.current_class)
            if self.current_class.base:
                self.current_scope.define("super", self.current_class.base)

        # Parametreleri ekle — self zaten ClassType olarak tanımlı, ezme
        for i, p in enumerate(node.params):
            if p.name == "self" and self.current_class is not None:
                continue  # ClassType korunuyor
            pt = fn_type.params[i] if i < len(fn_type.params) else UNKNOWN
            self.current_scope.define(p.name, pt, mutable=True)

        # Return tipi bağlamını ayarla
        saved_ret  = self.fn_return_type
        saved_async = self.fn_is_async
        self.fn_return_type = fn_type.ret
        self.fn_is_async = node.is_async

        # Gövdeyi kontrol et — param'lar zaten bu scope'da; fazladan Block scope'u açma
        for _stmt in node.body.stmts:
            self.visit(_stmt)

        self.fn_return_type = saved_ret
        self.fn_is_async    = saved_async
        self.pop_scope()
        self.type_env = saved_env
        return VOID

    def visit_ClassDecl(self, node: ClassDecl) -> NexType:
        ct: ClassType = self.type_registry.get(node.name, ClassType(node.name))
        ct.decl = node  # param isimlerine erişim için

        # Base sınıf
        if node.base:
            base_t = self.type_registry.get(node.base)
            if base_t is None:
                self.error(f"Unknown base class '{node.base}'", node)
            else:
                ct.base = base_t

        # Interface'ler
        for iface_name in node.interfaces:
            it = self.type_registry.get(iface_name)
            if it is None or not isinstance(it, InterfaceType):
                self.error(f"Unknown interface '{iface_name}'", node)
            else:
                ct.interfaces.append(it)

        # Generic parametreler
        saved_env = dict(self.type_env)
        for tp in node.type_params:
            self.type_env[tp] = TypeVar(tp)

        # Field'ları kaydet
        for f in node.fields:
            ft = self.resolve_type_node(f.type_ann)
            ct.fields[f.name] = ft

        # Method imzalarını kaydet (önce)
        for m in node.methods:
            mt = self._fn_signature(m)
            ct.methods[m.name] = mt

        # Interface uyumluluk kontrolü
        for iface in ct.interfaces:
            for mname, msig in iface.methods.items():
                if mname not in ct.methods:
                    self.error(
                        f"Class '{node.name}' does not implement '{mname}' from '{iface.name}'",
                        node)

        # Method gövdelerini kontrol et
        saved_class = self.current_class
        self.current_class = ct

        for m in node.methods:
            self.visit_FnDecl(m)

        self.current_class = saved_class
        self.type_env = saved_env
        self.type_registry[node.name] = ct
        return VOID

    def visit_InterfaceDecl(self, node: InterfaceDecl) -> NexType:
        it: InterfaceType = self.type_registry.get(node.name, InterfaceType(node.name))
        for m in node.methods:
            it.methods[m.name] = self._fn_signature(m)
        self.type_registry[node.name] = it
        return VOID

    def visit_ImportStmt(self, node: ImportStmt) -> NexType:
        return VOID

    def visit_ModuleStmt(self, node: ModuleStmt) -> NexType:
        return VOID

    # ── Statements ────────────────────────────────────────────────

    def visit_ExprStmt(self, node: ExprStmt) -> NexType:
        return self.visit(node.expr)

    def visit_ReturnStmt(self, node: ReturnStmt) -> NexType:
        if self.fn_return_type is None:
            self.error("'return' outside function", node)
            return VOID

        # Borrow ref'ler fonksiyon sınırını geçemez (v1, §4).
        if isinstance(node.value, BorrowExpr):
            self.error(
                "borrowed references cannot cross function boundaries yet (v1)",
                node.value)
        actual = self.visit(node.value) if node.value else VOID
        # @own dönüş tipi bu adımda parse edilemiyor; own değer GC'ye sızmasın.
        self._reject_own_value(actual, node, "return value")
        if isinstance(actual, RefType):
            self.error(
                "borrowed references cannot cross function boundaries yet (v1)",
                node)

        if isinstance(self.fn_return_type, VoidType):
            if node.value:
                self.warn("Returning value from void function", node)
        elif not isinstance(self.fn_return_type, UnknownType):
            self.check_assign(actual, self.fn_return_type, node, "return statement")
        return VOID

    def visit_BreakStmt(self, node: BreakStmt) -> NexType:
        if self.loop_depth == 0:
            self.error("'break' outside loop", node)
        return VOID

    def visit_ContinueStmt(self, node: ContinueStmt) -> NexType:
        if self.loop_depth == 0:
            self.error("'continue' outside loop", node)
        return VOID

    def visit_IfStmt(self, node: IfStmt) -> NexType:
        cond_t = self.visit(node.condition)
        if not isinstance(cond_t, (BoolType, UnknownType)):
            self.error(f"If condition must be 'bool', got '{cond_t}'", node)

        self.push_scope("if:then")
        self.visit_Block(node.then_block)
        self.pop_scope()

        for ec, eb in node.elif_arms:
            et = self.visit(ec)
            if not isinstance(et, (BoolType, UnknownType)):
                self.error(f"Elif condition must be 'bool', got '{et}'", node)
            self.push_scope("elif")
            self.visit_Block(eb)
            self.pop_scope()

        if node.else_block:
            self.push_scope("if:else")
            self.visit_Block(node.else_block)
            self.pop_scope()
        return VOID

    def visit_ForStmt(self, node: ForStmt) -> NexType:
        iter_t = self.visit(node.iterable)
        elem_t = self._iter_element_type(iter_t, node)

        self.push_scope("for")
        self.loop_depth += 1

        # Hedef değişkeni ekle
        if isinstance(node.target, Ident):
            self.current_scope.define(node.target.name, elem_t)
        elif isinstance(node.target, TupleLit):
            if isinstance(elem_t, TupleType):
                for i, el in enumerate(node.target.elements):
                    if isinstance(el, Ident):
                        et = elem_t.elements[i] if i < len(elem_t.elements) else UNKNOWN
                        self.current_scope.define(el.name, et)
            else:
                for el in node.target.elements:
                    if isinstance(el, Ident):
                        self.current_scope.define(el.name, UNKNOWN)

        self.visit_Block(node.body)
        self.loop_depth -= 1
        self.pop_scope()
        return VOID

    def _iter_element_type(self, t: NexType, node: Node) -> NexType:
        if isinstance(t, ListType):  return t.element
        if isinstance(t, SetType):   return t.element
        if isinstance(t, StrType):   return STR
        if isinstance(t, DictType):  return t.key
        if isinstance(t, RangeExpr): return INT
        # RangeExpr'ı tip olarak gönderebiliriz — aşağıda özel case
        if isinstance(t, UnknownType): return UNKNOWN
        self.warn(f"Iterating over non-iterable type '{t}'", node)
        return UNKNOWN

    def visit_WhileStmt(self, node: WhileStmt) -> NexType:
        ct = self.visit(node.condition)
        if not isinstance(ct, (BoolType, UnknownType)):
            self.error(f"While condition must be 'bool', got '{ct}'", node)
        self.push_scope("while")
        self.loop_depth += 1
        self.visit_Block(node.body)
        self.loop_depth -= 1
        self.pop_scope()
        return VOID

    def visit_MatchStmt(self, node: MatchStmt) -> NexType:
        subject_t = self.visit(node.subject)
        for arm in node.arms:
            self.push_scope("match:arm")
            pat_t = self._check_pattern(arm.pattern, subject_t, arm)
            if isinstance(arm.body, Block):
                self.visit_Block(arm.body)
            else:
                self.visit(arm.body)
            self.pop_scope()
        return VOID

    def _check_pattern(self, pattern: Node, subject_t: NexType, node: Node) -> NexType:
        if isinstance(pattern, Ident) and pattern.name == "_":
            return subject_t  # wildcard

        pat_t = self.visit(pattern)
        if not isinstance(subject_t, UnknownType) and not isinstance(pat_t, UnknownType):
            if not is_subtype(pat_t, subject_t) and not is_subtype(subject_t, pat_t):
                self.warn(
                    f"Pattern type '{pat_t}' may not match subject type '{subject_t}'",
                    node)
        return pat_t

    def visit_TryCatchStmt(self, node: TryCatchStmt) -> NexType:
        self.push_scope("try")
        self.visit_Block(node.try_block)
        self.pop_scope()
        for arm in node.catch_arms:
            self.push_scope("catch")
            if arm.alias:
                # Exception tipi — basit model: ClassType veya UNKNOWN
                exc_t = self.type_registry.get(arm.exc_type, UNKNOWN) if arm.exc_type else UNKNOWN
                self.current_scope.define(arm.alias, exc_t)
            self.visit_Block(arm.body)
            self.pop_scope()
        if node.finally_block:
            self.push_scope("finally")
            self.visit_Block(node.finally_block)
            self.pop_scope()
        return VOID

    # ── Expressions ───────────────────────────────────────────────

    def visit_IntLit(self, node: IntLit)     -> NexType: return INT
    def visit_FloatLit(self, node: FloatLit) -> NexType: return FLOAT
    def visit_StrLit(self, node: StrLit)     -> NexType: return STR
    def visit_BoolLit(self, node: BoolLit)   -> NexType: return BOOL
    def visit_NullLit(self, node: NullLit)   -> NexType: return NULL

    def visit_Ident(self, node: Ident) -> NexType:
        t = self.current_scope.lookup(node.name)
        if t is None:
            self.error(f"Undefined variable '{node.name}'", node)
            return UNKNOWN
        # @own değer yalnızca to_gc(x) argümanı olarak okunabilir; borrow
        # ifadeleri bu adımda kapsam dışı (§4). Diğer her okuma kopyalama
        # veya sahiplik ihlalidir (§2).
        if self.current_scope.lookup_mem_mode(node.name) == "own":
            if self.current_scope.is_moved(node.name):
                self.error(f"use of moved @own value '{node.name}'", node)
            else:
                self.error(
                    f"cannot copy @own value '{node.name}' "
                    f"(move it with to_gc(x))", node)
        return t

    def visit_BinaryExpr(self, node: BinaryExpr) -> NexType:
        lt = self.visit(node.left)
        rt = self.visit(node.right)

        op = node.op

        # Aritmetik
        if op in ("+", "-", "*", "/", "%", "**"):
            # String birleştirme
            if op == "+" and isinstance(lt, StrType) and isinstance(rt, StrType):
                return STR
            if op == "+" and isinstance(lt, StrType):
                self.warn(f"String concatenation with '{rt}' — implicit conversion may be needed", node)
                return STR
            if not is_numeric(lt):
                self.error(f"Operator '{op}' not supported on type '{lt}'", node)
                return UNKNOWN
            if not is_numeric(rt):
                self.error(f"Operator '{op}' not supported on type '{rt}'", node)
                return UNKNOWN
            if op == "/":
                return FLOAT   # bölme her zaman float
            return numeric_result(lt, rt)

        # Karşılaştırma
        if op in ("==", "!="):
            if not is_subtype(lt, rt) and not is_subtype(rt, lt):
                self.warn(f"Comparing incompatible types '{lt}' and '{rt}'", node)
            return BOOL

        if op in ("<", ">", "<=", ">="):
            if not is_comparable(lt):
                self.error(f"Type '{lt}' is not comparable", node)
            if not is_comparable(rt):
                self.error(f"Type '{rt}' is not comparable", node)
            return BOOL

        # Mantıksal
        if op in ("and", "or"):
            if not isinstance(lt, (BoolType, UnknownType)):
                self.error(f"Left operand of '{op}' must be 'bool', got '{lt}'", node)
            if not isinstance(rt, (BoolType, UnknownType)):
                self.error(f"Right operand of '{op}' must be 'bool', got '{rt}'", node)
            return BOOL

        # Null coalescing
        if op == "??":
            inner = lt.unwrap() if lt.is_nullable() else lt
            rt_inner = rt.unwrap() if rt.is_nullable() else rt
            if not isinstance(rt_inner, UnknownType) and not isinstance(inner, UnknownType):
                if not is_subtype(rt_inner, inner) and not is_subtype(inner, rt_inner):
                    self.warn(f"Null coalescing: left is '{lt}', right is '{rt}'", node)
            return inner if not isinstance(inner, UnknownType) else rt_inner

        return UNKNOWN

    def visit_UnaryExpr(self, node: UnaryExpr) -> NexType:
        t = self.visit(node.operand)
        if node.op == "-":
            if not is_numeric(t):
                self.error(f"Unary '-' requires numeric type, got '{t}'", node)
            return t
        if node.op == "not":
            if not isinstance(t, (BoolType, UnknownType)):
                self.error(f"'not' requires 'bool', got '{t}'", node)
            return BOOL
        return UNKNOWN

    def visit_BorrowExpr(self, node: BorrowExpr) -> NexType:
        op = node.operand
        if not isinstance(op, Ident):
            self.error("borrow operand must be a variable (v1)", node)
            return UNKNOWN
        name = op.name
        # visit_Ident çağrılmaz: borrow ≠ move, moved işaretlenmez.
        if self.current_scope.lookup(name) is None:
            self.error(f"Undefined variable '{name}'", op)
            return UNKNOWN
        if self.current_scope.lookup_mem_mode(name) != "own":
            self.error("only @own values can be borrowed (v1)", op)
            return UNKNOWN
        if self.current_scope.is_moved(name):
            self.error(f"use of moved @own value '{name}'", op)
            return UNKNOWN
        t = self.current_scope.lookup(name)
        if not isinstance(t, OwnType):
            self.error("only @own values can be borrowed (v1)", op)
            return UNKNOWN
        return RefType(t.inner, node.mutable)

    def _own_target_inner(self, node: Node) -> Optional[NexType]:
        """Üye/indeks atama hedefinin kökü @own değişken ise inner tipini
        döndür. Üye/indeks yazımı sahiplik aktarımı değil mutasyondur; bu
        yüzden visit_Ident'in "own okunamaz" kuralı burada bilinçli olarak
        atlanır (NEX_MEMORY_MODEL.md §2: move yalnızca tam değer içindir).
        @own kök değilse None döner ve normal visit yolu kullanılır."""
        if isinstance(node, Ident) \
                and self.current_scope.lookup_mem_mode(node.name) == "own":
            if self.current_scope.is_moved(node.name):
                self.error(f"use of moved @own value '{node.name}'", node)
            t = self.current_scope.lookup(node.name)
            return t.inner if isinstance(t, OwnType) else UNKNOWN
        return None

    def visit_AssignExpr(self, node: AssignExpr) -> NexType:
        # Sol taraf atanabilir mi?
        if isinstance(node.target, Ident):
            var_name = node.target.name
            var_t    = self.current_scope.lookup(var_name)
            if var_t is None:
                self.error(f"Undefined variable '{var_name}'", node)
                return UNKNOWN
            if not self.current_scope.is_mutable(var_name):
                self.error(f"Cannot assign to constant '{var_name}'", node)
                return UNKNOWN
            # visit() ipucunu sıfırlar; doğrudan literal için burada aç
            if isinstance(node.value, ListLit) and isinstance(var_t, StackArrayType):
                prev_hint = getattr(self, "_list_hint", None)
                self._list_hint = var_t
                try:
                    val_t = self.visit_ListLit(node.value)
                finally:
                    self._list_hint = prev_hint
            else:
                val_t = self.visit(node.value)
            op = node.op
            if self.current_scope.lookup_mem_mode(var_name) == "own":
                # @own değişkene yalnızca OwnType (to_own sonucu) atanabilir;
                # düz GC değeri yasak (§2). Diğer @own değişkeni kopyalamak da
                # yasak — move semantiği bu adımda yok, visit_Ident hata verir.
                # Moved durumu önce raporlanır; sağlıklı yeniden atama
                # (ASSIGN + OwnType) ise döngüyü sıfırlar.
                if self.current_scope.is_moved(var_name) and op != "ASSIGN":
                    self.error(
                        f"use of moved @own value '{var_name}'", node)
                elif op != "ASSIGN":
                    self.error(
                        f"compound assignment is not supported on @own variable "
                        f"'{var_name}'", node)
                elif not isinstance(val_t, OwnType):
                    self.error(
                        f"cannot assign GC value to @own variable '{var_name}' "
                        f"(use to_own(x))", node)
                else:
                    self.current_scope.clear_moved(var_name)
                return var_t
            # Borrow değişken yeniden ataması (v1): yalnızca RefType, runtime release eder.
            if isinstance(var_t, RefType):
                if op != "ASSIGN":
                    self.error(
                        f"compound assignment is not supported on borrow variable "
                        f"'{var_name}'", node)
                elif not isinstance(val_t, RefType):
                    self.error(
                        f"cannot assign non-reference value to borrow variable "
                        f"'{var_name}' (v1)", node)
                return var_t
            if op == "ASSIGN" and self.current_scope.lookup_mem_mode(var_name) == "stack":
                if (isinstance(val_t, ListType) and isinstance(node.value, ListLit)
                        and len(node.value.elements)
                            != getattr(var_t, 'size', len(node.value.elements))):
                    self.error(
                        f"Stack array size mismatch: '{var_name}' has size "
                        f"{getattr(var_t, 'size', '?')}, got "
                        f"{len(node.value.elements)} element(s)", node)
                elif not isinstance(val_t, StackArrayType):
                    self.error(
                        f"Cannot assign value of type '{val_t}' to stack array "
                        f"'{var_name}' of type '{var_t}'", node)
                elif (not isinstance(var_t, UnknownType)
                        and val_t.size != getattr(var_t, 'size', val_t.size)):
                    self.error(
                        f"Stack array size mismatch: '{var_name}' has size "
                        f"{getattr(var_t, 'size', '?')}, got size {val_t.size}", node)
                elif not isinstance(var_t, UnknownType):
                    self.check_assign(val_t.element, var_t.element, node,
                                      f"assignment to '{var_name}'")
            elif op == "ASSIGN":
                self._reject_own_value(
                    val_t, node, f"assignment to '{var_name}'")
                self.check_assign(val_t, var_t, node, f"assignment to '{var_name}'")
            elif op in ("PLUS_EQ", "MINUS_EQ", "STAR_EQ", "SLASH_EQ"):
                if not is_numeric(var_t):
                    self.error(f"Compound assignment requires numeric type, got '{var_t}'", node)
            return var_t
        elif isinstance(node.target, MemberExpr):
            own_inner = self._own_target_inner(node.target.obj)
            obj_t = own_inner if own_inner is not None else self.visit(node.target.obj)
            # Auto-deref: borrow ref üzerinden üye yazımı.
            if isinstance(obj_t, RefType):
                obj_t = obj_t.inner
            val_t = self.visit(node.value)
            self._reject_own_value(val_t, node.value,
                                   f"assignment to '{node.target.member}'")
            # Field tip kontrolü
            if isinstance(obj_t, ClassType):
                field_t = obj_t.lookup_field(node.target.member)
                if field_t:
                    self.check_assign(val_t, field_t, node,
                                      f"assignment to '{node.target.member}'")
            return val_t
        elif isinstance(node.target, IndexExpr):
            own_inner = self._own_target_inner(node.target.obj)
            obj_t = own_inner if own_inner is not None else self.visit(node.target.obj)
            # Auto-deref: borrow ref üzerinden yazma (mutability runtime'da denetlenir).
            if isinstance(obj_t, RefType):
                obj_t = obj_t.inner
            self.visit(node.target.index)
            val_t = self.visit(node.value)
            self._reject_own_value(val_t, node.value, "element assignment")
            # Sabit indeks aralık denetimi (okuma tarafı visit_IndexExpr'de
            # yapılır; atama hedefi olarak IndexExpr'i tam ziyaret etmediğimiz
            # için yazma tarafındaki denetim burada tekrar edilir)
            if isinstance(obj_t, StackArrayType) and isinstance(node.target.index, IntLit):
                const_idx = int(node.target.index.value)
                if const_idx < 0 or const_idx >= obj_t.size:
                    self.error(
                        f"Index {const_idx} out of bounds for stack array of "
                        f"size {obj_t.size}", node)
            # Eleman atama tip kontrolü — stack dizi elemanına (ve GC listesine)
            # yanlış tipte değer atanması, ayrıca iç stack boyut uyuşmazlığı.
            if isinstance(obj_t, (StackArrayType, ListType)):
                elem_t = obj_t.element
                if isinstance(elem_t, StackArrayType) and isinstance(node.value, ListLit):
                    self._check_stack_literal(
                        node.value, elem_t, node, "target element")
                else:
                    self.check_assign(val_t, elem_t, node, "element assignment")
            elif isinstance(obj_t, DictType):
                self.check_assign(val_t, obj_t.value, node, "dict value assignment")
            return val_t
        return UNKNOWN

    def _check_to_own(self, node: CallExpr) -> NexType:
        """to_own(x): GC değerinden bağımsız @own değer (deep copy, §2)."""
        if len(node.args) != 1:
            self.error("to_own requires exactly 1 argument", node)
            return OwnType(UNKNOWN)
        arg = node.args[0]
        if isinstance(arg, Ident) \
                and self.current_scope.lookup_mem_mode(arg.name) == "own":
            self.error(
                f"to_own requires a GC value; '{arg.name}' is already @own", arg)
            t = self.current_scope.lookup(arg.name)
            return t if isinstance(t, OwnType) else OwnType(UNKNOWN)
        arg_t = self.visit(arg)
        if isinstance(arg_t, OwnType):
            self.error(
                "to_own requires a GC value; argument is already @own", arg)
            return arg_t
        # to_own argümanı GC bağlamındadır; stack dizi kaçış kuralı gereği
        # heap listesine kopyalanır, own tipi de onun üzerine kurulur.
        return OwnType(self._escape_to_heap(arg_t))

    def _check_to_gc(self, node: CallExpr) -> NexType:
        """to_gc(x): @own değeri GC'ye taşır (move, §2). x moved işaretlenir."""
        if len(node.args) != 1:
            self.error("to_gc requires exactly 1 argument", node)
            return UNKNOWN
        arg = node.args[0]
        if not isinstance(arg, Ident):
            # Yan etkili alt ifadeleri yine denetle; ama taşınacak değişken yok.
            self.visit(arg)
            self.error("to_gc requires a variable reference", arg)
            return UNKNOWN
        name = arg.name
        t = self.current_scope.lookup(name)
        if t is None:
            self.error(f"Undefined variable '{name}'", arg)
            return UNKNOWN
        if self.current_scope.lookup_mem_mode(name) != "own" \
                or not isinstance(t, OwnType):
            self.error(
                f"to_gc requires an @own variable; '{name}' is not @own", arg)
            return t
        if self.current_scope.is_moved(name):
            self.error(f"use of moved @own value '{name}'", arg)
            return t.inner
        self.current_scope.mark_moved(name)
        return t.inner

    def _check_free(self, node: CallExpr) -> NexType:
        """free(x): @own değişkenin sahipliğini tüketir (§6 adım 4 civarı).
        GC değişken üzerinde free yasaktır — free yalnızca @own içindir."""
        if len(node.args) != 1:
            self.error("free requires exactly 1 argument", node)
            return VOID
        arg = node.args[0]
        if not isinstance(arg, Ident):
            self.visit(arg)
            self.error("free requires an @own variable reference", arg)
            return VOID
        name = arg.name
        t = self.current_scope.lookup(name)
        if t is None:
            self.error(f"Undefined variable '{name}'", arg)
            return VOID
        if self.current_scope.lookup_mem_mode(name) != "own":
            self.error(
                f"free requires an @own variable; '{name}' is not @own", arg)
            return VOID
        if self.current_scope.is_moved(name):
            self.error(f"use of moved @own value '{name}'", arg)
            return VOID
        self.current_scope.mark_moved(name)
        return VOID

    def visit_CallExpr(self, node: CallExpr) -> NexType:
        # GC ⇄ @own geçişleri ve free: gerçek imzalar argümana bağlı olduğu
        # için FnType tablosu yerine burada özel ele alınır (§2).
        if isinstance(node.callee, Ident):
            if node.callee.name == "to_own":
                return self._check_to_own(node)
            if node.callee.name == "to_gc":
                return self._check_to_gc(node)
            if node.callee.name == "free":
                return self._check_free(node)
        # Borrow ifadeleri fonksiyon sınırını geçemez (v1, §4).
        for i, arg in enumerate(node.args):
            if isinstance(arg, BorrowExpr):
                self.error(
                    "borrowed references cannot cross function boundaries yet (v1)",
                    arg)
        # Metod çağrısı: callee tipini doğrudan kullan — obj'yi tekrar ziyaret
        # etme (eski kod MemberExpr dalında çift ziyarete yol açıyordu).
        if isinstance(node.callee, MemberExpr):
            obj_t = self.visit(node.callee.obj)
            if isinstance(obj_t, RefType):
                obj_t = obj_t.inner
            method_name = node.callee.member

            # Builtin metodlar
            bt = self._builtin_method(obj_t, method_name, node)
            if bt is not None:
                # Builtin metod argümanları da borrow taşıyamaz (sınır korunsun).
                for arg in node.args:
                    at = self.visit(arg)
                    if isinstance(at, RefType):
                        self.error(
                            "borrowed references cannot cross function boundaries yet (v1)",
                            arg)
                return bt

            if isinstance(obj_t, ClassType):
                m = obj_t.lookup_method(method_name)
                if m is None:
                    self.error(f"'{obj_t}' has no method '{method_name}'", node)
                    return UNKNOWN
                callee_t = m
            elif isinstance(obj_t, UnknownType):
                callee_t = UNKNOWN
            else:
                self.error(f"'{obj_t}' has no method '{method_name}'", node)
                return UNKNOWN
        else:
            callee_t = self.visit(node.callee)

        if isinstance(callee_t, FnType):
            # Argüman sayısı
            n_params = len(callee_t.params)
            n_args   = len(node.args)
            if n_params != n_args and not any(isinstance(p, UnknownType) for p in callee_t.params):
                self.error(
                    f"Function expects {n_params} arguments, got {n_args}", node)
            # Argüman tipleri
            for i, (arg, param_t) in enumerate(zip(node.args, callee_t.params)):
                arg_t = self.visit(arg)
                self._reject_own_value(arg_t, arg, f"argument {i+1}")
                if isinstance(arg_t, RefType):
                    self.error(
                        "borrowed references cannot cross function boundaries yet (v1)",
                        arg)
                if not isinstance(param_t, UnknownType):
                    self.check_assign(arg_t, param_t, arg,
                                      f"argument {i+1}")
            return callee_t.ret

        if isinstance(callee_t, ClassType):
            # Constructor çağrısı: MyClass(...)
            # DEĞİŞİKLİK NOTU: 'hasattr' guard'ı etkisizdi — ClassType.decl
            # var ama None olabilir (class, main'den sonra tanımlandığında).
            # AttributeError yerine None kontrolü yapılır.
            init_decl = None
            if callee_t.decl is not None:
                init_decl = next(
                    (m for m in callee_t.decl.methods
                     if m.name in ("init", "__init__")), None)
            init = callee_t.lookup_method("init")
            if init and init_decl:
                # self parametresini isimle atla
                non_self = [(p, t) for p, t in zip(init_decl.params, init.params)
                            if p.name != "self"]
                for i, (arg, (_, param_t)) in enumerate(zip(node.args, non_self)):
                    arg_t = self.visit(arg)
                    self._reject_own_value(arg_t, arg, f"constructor argument {i+1}")
                    if isinstance(arg_t, RefType):
                        self.error(
                            "borrowed references cannot cross function boundaries yet (v1)",
                            arg)
                    if not isinstance(param_t, UnknownType):
                        self.check_assign(arg_t, param_t, arg,
                                          f"constructor argument {i+1}")
            return callee_t

        if not isinstance(callee_t, UnknownType):
            self.error(f"'{callee_t}' is not callable", node)
        return UNKNOWN

    def _builtin_method(self, obj_t: NexType, method: str, node: Node) -> Optional[NexType]:
        """Liste, dict, str gibi builtin tiplerin metodları."""
        if isinstance(obj_t, StackArrayType):
            elem = obj_t.element
            methods = {
                "length":     INT,    "len":        INT,
                "isEmpty":    BOOL,
                "first":      NullableType(elem),
                "last":       NullableType(elem),
                "contains":   BOOL,   "has":        BOOL,
                "indexOf":    NullableType(INT),
                "join":       STR,
            }
            return methods.get(method)
        if isinstance(obj_t, ListType):
            elem = obj_t.element
            methods = {
                "append":     VOID,   "push":       VOID,
                "pop":        NullableType(elem),
                "removeLast": elem,
                "length":     INT,    "len":        INT,
                "isEmpty":    BOOL,
                "first":      NullableType(elem),
                "last":       NullableType(elem),
                "filter":     obj_t,
                "map":        ListType(UNKNOWN),
                "reduce":     UNKNOWN,
                "sort":       VOID,   "reverse":    VOID,
                "contains":   BOOL,   "has":        BOOL,
                "indexOf":    NullableType(INT),
                "join":       STR,
            }
            return methods.get(method)
        if isinstance(obj_t, StrType):
            methods = {
                "length":      INT,    "len":         INT,
                "upper":       STR,    "lower":       STR,
                "trim":        STR,    "strip":       STR,
                "split":       ListType(STR),
                "contains":    BOOL,   "has":         BOOL,
                "starts_with": BOOL,   "startswith":  BOOL,
                "ends_with":   BOOL,   "endswith":    BOOL,
                "replace":     STR,
                "find":        INT,    "indexOf":     NullableType(INT),
                "toString":    STR,
            }
            return methods.get(method)
        if isinstance(obj_t, DictType):
            methods = {
                "keys":     ListType(obj_t.key),
                "values":   ListType(obj_t.value),
                "get":      NullableType(obj_t.value),
                "set":      VOID,
                "remove":   BOOL,
                "contains": BOOL,   "has":     BOOL,
                "length":   INT,    "len":     INT,
                "isEmpty":  BOOL,
            }
            return methods.get(method)
        if isinstance(obj_t, (IntType, FloatType)):
            methods = {"toString": STR, "abs": obj_t}
            return methods.get(method)
        return None

    def visit_MemberExpr(self, node: MemberExpr) -> NexType:
        if isinstance(node.obj, Ident) and node.obj.name in (
                "alloc", "free", "sizeof", "to_own", "to_gc"):
            self.error(
                f"'{node.obj.name}' is a builtin function, not a value", node.obj)
            return UNKNOWN
        obj_t = self.visit(node.obj)
        # Auto-deref: borrow ref üzerinden üye/metot erişimi (v1, §4)
        if isinstance(obj_t, RefType):
            obj_t = obj_t.inner

        if isinstance(obj_t, ClassType):
            # Field?
            f = obj_t.lookup_field(node.member)
            if f: return f
            # Method reference?
            m = obj_t.lookup_method(node.member)
            if m: return m
            self.error(f"'{obj_t}' has no member '{node.member}'", node)
            return UNKNOWN

        # Builtin özellikler
        bt = self._builtin_method(obj_t, node.member, node)
        if bt is not None:
            return bt

        if not isinstance(obj_t, UnknownType):
            self.error(f"'{obj_t}' has no member '{node.member}'", node)
        return UNKNOWN

    def visit_IndexExpr(self, node: IndexExpr) -> NexType:
        obj_t   = self.visit(node.obj)
        idx_t   = self.visit(node.index)
        # Auto-deref: borrow ref üzerinden erişim (v1, §4)
        if isinstance(obj_t, RefType):
            obj_t = obj_t.inner

        if isinstance(obj_t, StackArrayType):
            if not isinstance(idx_t, (IntType, UnknownType)):
                self.error(f"Stack array index must be 'int', got '{idx_t}'", node)
            # Sabit indeks derleme zamanında denetlenebilir
            if isinstance(node.index, IntLit):
                idx = int(node.index.value)
                if idx < 0 or idx >= obj_t.size:
                    self.error(
                        f"Index {idx} out of bounds for stack array of size "
                        f"{obj_t.size}", node)
            return obj_t.element

        if isinstance(obj_t, ListType):
            if not isinstance(idx_t, (IntType, UnknownType)):
                self.error(f"List index must be 'int', got '{idx_t}'", node)
            return obj_t.element

        if isinstance(obj_t, DictType):
            if not is_subtype(idx_t, obj_t.key):
                self.error(
                    f"Dict key must be '{obj_t.key}', got '{idx_t}'", node)
            return NullableType(obj_t.value)

        if isinstance(obj_t, StrType):
            if not isinstance(idx_t, (IntType, UnknownType)):
                self.error(f"String index must be 'int', got '{idx_t}'", node)
            return STR

        if isinstance(obj_t, TupleType):
            if isinstance(idx_t, IntType):
                # Compile-time sabit index olursa tip çıkarımı yapılabilir
                pass
            return UNKNOWN

        if not isinstance(obj_t, UnknownType):
            self.error(f"Type '{obj_t}' is not indexable", node)
        return UNKNOWN

    def visit_IfExpr(self, node: IfExpr) -> NexType:
        cond_t = self.visit(node.condition)
        if not isinstance(cond_t, (BoolType, UnknownType)):
            self.error(f"If expression condition must be 'bool', got '{cond_t}'", node)
        then_t = self.visit(node.then_val)
        else_t = self.visit(node.else_val)
        if then_t != else_t and not isinstance(then_t, UnknownType) and not isinstance(else_t, UnknownType):
            self.warn(
                f"If expression branches have different types: '{then_t}' vs '{else_t}'", node)
        return then_t

    def visit_RangeExpr(self, node: RangeExpr) -> NexType:
        start_t = self.visit(node.start)
        end_t   = self.visit(node.end)
        if not isinstance(start_t, (IntType, UnknownType)):
            self.error(f"Range start must be 'int', got '{start_t}'", node)
        if not isinstance(end_t, (IntType, UnknownType)):
            self.error(f"Range end must be 'int', got '{end_t}'", node)
        return ListType(INT)   # range → iterable over int

    def visit_AwaitExpr(self, node: AwaitExpr) -> NexType:
        if not self.fn_is_async:
            self.error("'await' used outside async function", node)
        t = self.visit(node.expr)
        return t   # async fn zaten içeriği döner

    def visit_SpawnExpr(self, node: SpawnExpr) -> NexType:
        self.visit(node.expr)
        return VOID

    def visit_NullCoalExpr(self, node: NullCoalExpr) -> NexType:
        # BinaryExpr "??" ile aynı mantık — ama ayrı node varsa
        lt = self.visit(node.left)
        rt = self.visit(node.right)
        inner = lt.unwrap() if lt.is_nullable() else lt
        return inner

    def _expect_list_lit(self, node: ListLit) -> bool:
        """Literal'in [T; N] stack bağlamında mı beklendiğini söyler.

        Sadece üst düğümün (VarDecl/AssignExpr/Argüman/Return...) analizi
        sırasında ayarlanan ipucu ile çalışır; global durum yoktur.
        """
        hint = getattr(self, "_list_hint", None)
        if isinstance(hint, StackArrayType) and len(node.elements) == hint.size:
            return True
        return False

    def visit_ListLit(self, node: ListLit) -> NexType:
        if not node.elements:
            return ListType(UNKNOWN)
        types = [self.visit(e) for e in node.elements]
        for e, t in zip(node.elements, types):
            self._reject_own_value(t, e, "list element")
        # Tüm elemanlar aynı tipte mi?
        first = types[0]
        for i, t in enumerate(types[1:], 1):
            if t != first and not isinstance(t, UnknownType) and not isinstance(first, UnknownType):
                self.error(
                    f"List element {i} has type '{t}', expected '{first}'", node)
        # Stack atanma bağlamında (boyut da uyuyorsa) literal stack dizisi
        # sayılır; diğer her yerde literal heap listesidir.
        if self._expect_list_lit(node):
            return StackArrayType(first, len(node.elements))
        return ListType(first)

    def visit_DictLit(self, node: DictLit) -> NexType:
        if not node.pairs:
            return DictType(UNKNOWN, UNKNOWN)
        kt = self.visit(node.pairs[0][0])
        vt = self.visit(node.pairs[0][1])
        self._reject_own_value(kt, node.pairs[0][0], "dict key")
        self._reject_own_value(vt, node.pairs[0][1], "dict value")
        for k, v in node.pairs[1:]:
            kti = self.visit(k)
            vti = self.visit(v)
            self._reject_own_value(kti, k, "dict key")
            self._reject_own_value(vti, v, "dict value")
            if not isinstance(kti, UnknownType) and kti != kt:
                kt = UNKNOWN  # heterogeneous keys → unknown
            if not isinstance(vti, UnknownType) and vti != vt:
                vt = UNKNOWN  # heterogeneous values → unknown (normal use case)
        return DictType(kt, vt)

    def visit_TupleLit(self, node: TupleLit) -> NexType:
        types = [self.visit(e) for e in node.elements]
        for e, t in zip(node.elements, types):
            self._reject_own_value(t, e, "tuple element")
        return TupleType(types)

    def visit_SetLit(self, node: SetLit) -> NexType:
        if not node.elements:
            return SetType(UNKNOWN)
        types = [self.visit(e) for e in node.elements]
        for e, t in zip(node.elements, types):
            self._reject_own_value(t, e, "set element")
        return SetType(types[0])

    def visit_MatchExpr(self, node: MatchExpr) -> NexType:
        subject_t = self.visit(node.subject)
        result_types = []
        for arm in node.arms:
            self.push_scope("match:arm")
            self._check_pattern(arm.pattern, subject_t, arm)
            bt = self.visit(arm.body)
            result_types.append(bt)
            self.pop_scope()
        if result_types:
            first = result_types[0]
            for rt in result_types[1:]:
                if rt != first and not isinstance(rt, UnknownType):
                    self.warn(f"Match arms have inconsistent types: '{rt}' vs '{first}'", node)
            return first
        return UNKNOWN

    def visit_LambdaExpr(self, node: LambdaExpr) -> NexType:
        self.push_scope("lambda")
        param_types = []
        for p in node.params:
            pt = self.resolve_type_node(p.type_ann) if p.type_ann else UNKNOWN
            self.current_scope.define(p.name, pt)
            param_types.append(pt)
        body_t = self.visit(node.body)
        self.pop_scope()
        return FnType(param_types, body_t)

    # ── Yardımcılar ───────────────────────────────────────────────

    def visit_MatchArm(self, node: MatchArm) -> NexType:
        return self.visit(node.body)

    def check(self, tree: Program) -> bool:
        """Ana giriş noktası. True = hata yok."""
        self.visit(tree)
        return len(self.errors) == 0

    def report(self) -> str:
        if not self.diagnostics:
            return "  ✓ No issues found."
        lines = []
        errors   = [d for d in self.diagnostics if d.kind == "error"]
        warnings = [d for d in self.diagnostics if d.kind == "warning"]
        if errors:
            lines.append(f"  Errors ({len(errors)}):")
            lines.extend(str(d) for d in errors)
        if warnings:
            lines.append(f"  Warnings ({len(warnings)}):")
            lines.extend(str(d) for d in warnings)
        return "\n".join(lines)
