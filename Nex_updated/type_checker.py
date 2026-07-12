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

    def __str__(self):
        icon = {"error": "✗", "warning": "⚠", "info": "ℹ"}[self.kind]
        return f"  {icon} [{self.kind.upper()}] Line {self.line}:{self.col} — {self.message}"


# ─── Sembol Tablosu ───────────────────────────────────────────────────────────

class Scope:
    def __init__(self, parent: Optional[Scope] = None, name: str = "<scope>"):
        self.parent  = parent
        self.name    = name
        self.symbols: dict[str, NexType] = {}
        self.mutability: dict[str, bool] = {}  # False = const

    def define(self, name: str, typ: NexType, mutable: bool = True):
        self.symbols[name]    = typ
        self.mutability[name] = mutable

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


# ─── Type Checker ─────────────────────────────────────────────────────────────

class TypeChecker:

    def __init__(self):
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
        self.diagnostics.append(TypeDiag("error", msg, node.line, node.col))

    def warn(self, msg: str, node: Node):
        self.diagnostics.append(TypeDiag("warning", msg, node.line, node.col))

    def info(self, msg: str, node: Node):
        self.diagnostics.append(TypeDiag("info", msg, node.line, node.col))

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
                                              node.line, node.col))
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

        return UNKNOWN

    # ── Tip uyumluluğu ────────────────────────────────────────────

    def check_assign(self, actual: NexType, expected: NexType, node: Node, ctx: str = ""):
        if not is_subtype(actual, expected):
            msg = f"Type mismatch{' in ' + ctx if ctx else ''}: expected '{expected}', got '{actual}'"
            self.error(msg, node)

    # ── Ana Ziyaretçi ─────────────────────────────────────────────

    def visit(self, node: Node) -> NexType:
        if node is None:
            return VOID
        method = f"visit_{type(node).__name__}"
        if hasattr(self, method):
            return getattr(self, method)(node)
        # Bilinmeyen node'u sessizce geç
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
        last = VOID
        for stmt in node.stmts:
            last = self.visit(stmt)
        return last

    # ── Declarations ──────────────────────────────────────────────

    def visit_VarDecl(self, node: VarDecl) -> NexType:
        declared = self.resolve_type_node(node.type_ann) if node.type_ann else None
        inferred = self.visit(node.value) if node.value else None

        if declared and inferred:
            self.check_assign(inferred, declared, node, f"declaration of '{node.name}'")
            var_type = declared
        elif declared:
            var_type = declared
        elif inferred:
            var_type = inferred
        else:
            self.warn(f"Variable '{node.name}' has no type annotation and no initializer", node)
            var_type = UNKNOWN

        if self.current_scope.lookup_local(node.name):
            self.warn(f"Variable '{node.name}' shadows outer declaration", node)

        self.current_scope.define(node.name, var_type, mutable=node.mutable)
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
            self.current_scope.define(name, vt, mutable=node.mutable)
        return VOID

    def visit_FnDecl(self, node: FnDecl) -> NexType:
        # İmza zaten pre-declare'da kaydedildi (top-level için)
        # Ama iç fonksiyonlar için burada da kaydedelim
        fn_type = self._fn_signature(node)
        if not self.current_scope.lookup_local(node.name):
            self.current_scope.define(node.name, fn_type)

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

        # Gövdeyi kontrol et
        self.visit_Block(node.body)

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

        actual = self.visit(node.value) if node.value else VOID

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
            val_t = self.visit(node.value)
            op = node.op
            if op == "ASSIGN":
                self.check_assign(val_t, var_t, node, f"assignment to '{var_name}'")
            elif op in ("PLUS_EQ", "MINUS_EQ", "STAR_EQ", "SLASH_EQ"):
                if not is_numeric(var_t):
                    self.error(f"Compound assignment requires numeric type, got '{var_t}'", node)
            return var_t
        elif isinstance(node.target, MemberExpr):
            obj_t = self.visit(node.target.obj)
            val_t = self.visit(node.value)
            # Field tip kontrolü
            if isinstance(obj_t, ClassType):
                field_t = obj_t.lookup_field(node.target.member)
                if field_t:
                    self.check_assign(val_t, field_t, node,
                                      f"assignment to '{node.target.member}'")
            return val_t
        elif isinstance(node.target, IndexExpr):
            self.visit(node.target)
            self.visit(node.value)
            return UNKNOWN
        return UNKNOWN

    def visit_CallExpr(self, node: CallExpr) -> NexType:
        callee_t = self.visit(node.callee)

        # Metod çağrısı: obj.method(...)
        if isinstance(node.callee, MemberExpr):
            obj_t = self.visit(node.callee.obj)
            method_name = node.callee.member

            # Builtin metodlar
            bt = self._builtin_method(obj_t, method_name, node)
            if bt is not None:
                return bt

            if isinstance(obj_t, ClassType):
                m = obj_t.lookup_method(method_name)
                if m is None:
                    self.error(f"'{obj_t}' has no method '{method_name}'", node)
                    return UNKNOWN
                callee_t = m

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
                if not isinstance(param_t, UnknownType):
                    self.check_assign(arg_t, param_t, arg,
                                      f"argument {i+1}")
            return callee_t.ret

        if isinstance(callee_t, ClassType):
            # Constructor çağrısı: MyClass(...)
            init_decl = next(
                (m for m in callee_t.decl.methods if m.name in ("init", "__init__")),
                None) if hasattr(callee_t, 'decl') else None
            init = callee_t.lookup_method("init")
            if init and init_decl:
                # self parametresini isimle atla
                non_self = [(p, t) for p, t in zip(init_decl.params, init.params)
                            if p.name != "self"]
                for i, (arg, (_, param_t)) in enumerate(zip(node.args, non_self)):
                    arg_t = self.visit(arg)
                    if not isinstance(param_t, UnknownType):
                        self.check_assign(arg_t, param_t, arg,
                                          f"constructor argument {i+1}")
            return callee_t

        if not isinstance(callee_t, UnknownType):
            self.error(f"'{callee_t}' is not callable", node)
        return UNKNOWN

    def _builtin_method(self, obj_t: NexType, method: str, node: Node) -> Optional[NexType]:
        """Liste, dict, str gibi builtin tiplerin metodları."""
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
        obj_t = self.visit(node.obj)

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

    def visit_ListLit(self, node: ListLit) -> NexType:
        if not node.elements:
            return ListType(UNKNOWN)
        types = [self.visit(e) for e in node.elements]
        # Tüm elemanlar aynı tipte mi?
        first = types[0]
        for i, t in enumerate(types[1:], 1):
            if t != first and not isinstance(t, UnknownType) and not isinstance(first, UnknownType):
                self.error(
                    f"List element {i} has type '{t}', expected '{first}'", node)
        return ListType(first)

    def visit_DictLit(self, node: DictLit) -> NexType:
        if not node.pairs:
            return DictType(UNKNOWN, UNKNOWN)
        kt = self.visit(node.pairs[0][0])
        vt = self.visit(node.pairs[0][1])
        for k, v in node.pairs[1:]:
            kti = self.visit(k)
            vti = self.visit(v)
            if not isinstance(kti, UnknownType) and kti != kt:
                kt = UNKNOWN  # heterogeneous keys → unknown
            if not isinstance(vti, UnknownType) and vti != vt:
                vt = UNKNOWN  # heterogeneous values → unknown (normal use case)
        return DictType(kt, vt)

    def visit_TupleLit(self, node: TupleLit) -> NexType:
        return TupleType([self.visit(e) for e in node.elements])

    def visit_SetLit(self, node: SetLit) -> NexType:
        if not node.elements:
            return SetType(UNKNOWN)
        types = [self.visit(e) for e in node.elements]
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
