"""
Nex Language — Interpreter
AST'yi çalıştırarak programı yorumlar.
Visitor pattern + environment stack.
"""

from __future__ import annotations

import copy
from typing import Any, Callable, Dict, List, Optional

from ast_nodes import *
from nex_types import *

# ─── Environment (Değişken Ortamı) ─────────────────────────────────────────────────────


class StackArray:
    """@stack sabit boyutlu dizi. Scope kapanınca canlıyken değeri ölür.

    Python katmanında sabit boyutlu bir liste olarak yaşar; sahiplik veya
    GC etkileşimi yoktur. Ölen bir StackArray'e erişim StackUseAfterScope
    fırlatır (ileride suspend/resume gelirse capture kaçışını yakalamak için).
    """

    def __init__(self, items: list, declared_size: int, elem_type=None):
        if len(items) != declared_size:
            raise ValueError(
                f"Stack array size mismatch: declared size {declared_size}, "
                f"got {len(items)} element(s)")
        self._items = list(items)
        self.size = declared_size
        self.elem_type = elem_type
        self.alive = True

    def _check(self):
        if not self.alive:
            raise StackPanic(
                "Stack value used after its scope ended (stack-use-after-scope)")

    def kill(self):
        self.alive = False
        self._items = []

    def __getitem__(self, idx):
        self._check()
        return self._items[idx]

    def __setitem__(self, idx, value):
        self._check()
        self._items[idx] = value

    def __len__(self):
        self._check()
        return len(self._items)

    def __iter__(self):
        self._check()
        return iter(list(self._items))

    def to_list(self):
        """Kaçış kuralı: scope dışına çıkan stack değeri bağımsız heap kopyasıdır.

        Sadece üst düzey kopya yetmez — iç içe stack/list değerleri de
        kopyalanmalı; aksi halde inner aliasing oluşur (kopya, ölmüş stack
        dizisinin iç elemanlarını paylaşır)."""
        self._check()
        return self._deep_heap_copy(self._items)

    @staticmethod
    def _deep_heap_copy(items) -> list:
        out = []
        for x in items:
            if isinstance(x, StackArray):
                out.append(x.to_list())
            elif isinstance(x, list):
                out.append(StackArray._deep_heap_copy(x))
            else:
                out.append(x)
        return out

    def __repr__(self):
        if not self.alive:
            return "<dead stack array>"
        return f"[{', '.join(repr(x) for x in self._items)}]"


class OwnValue:
    """@own sahiplik wrapper'ı. Değer tek sahibindir; free/scope sonrası ölüdür.

    Normal bağlamlarda opak tutulur (checker sıradan okumayı reddeder);
    yalnızca to_gc/free builtin'leri wrapper'a erişir.
    """

    __slots__ = ("_value", "name", "alive", "shared", "mut_borrowed")

    def __init__(self, value, name: str = "<own>"):
        self._value = value
        self.name = name
        self.alive = True
        self.shared = 0        # aktif & paylaşımlı borrow sayısı
        self.mut_borrowed = 0  # aktif &mut borrow sayısı (0 veya 1)

    def kill(self):
        self.alive = False
        self._value = None  # Python GC içeriği geri alabilsin

    def _check(self):
        if not self.alive:
            raise OwnPanic(
                f"@own value '{self.name}' used after free/scope end (use-after-free)")

    def acquire_shared(self, node=None):
        if self.mut_borrowed > 0:
            raise OwnPanic(
                f"borrow conflict: shared borrow of '{self.name}' while mutably borrowed",
                node)
        self.shared += 1

    def acquire_mut(self, node=None):
        if self.shared > 0 or self.mut_borrowed > 0:
            raise OwnPanic(
                f"borrow conflict: exclusive &mut borrow of '{self.name}' while borrowed",
                node)
        self.mut_borrowed = 1

    def release_shared(self):
        if self.shared > 0:
            self.shared -= 1

    def release_mut(self):
        if self.mut_borrowed > 0:
            self.mut_borrowed -= 1

    def __repr__(self):
        if not self.alive:
            return f"<freed own '{self.name}'>"
        return f"<own '{self.name}'>"


class BorrowRef:
    """&x / &mut x çalışma zamanı referansı (v1: yerel, scope ile serbest)."""

    __slots__ = ("own", "mutable", "name", "released")

    def __init__(self, own: OwnValue, mutable: bool, name: str, node=None):
        self.own = own
        self.mutable = mutable
        self.name = name
        self.released = False
        if mutable:
            own.acquire_mut(node)
        else:
            own.acquire_shared(node)

    def _check(self, node=None):
        if self.released:
            raise OwnPanic(f"use of released borrow '{self.name}'", node)
        if not self.own.alive:
            raise OwnPanic(
                f"@own value '{self.name}' used after free/scope end (use-after-free)",
                node)

    def release(self):
        if self.released:
            return
        self.released = True
        if self.mutable:
            self.own.release_mut()
        else:
            self.own.release_shared()

    def __repr__(self):
        kind = "&mut" if self.mutable else "&"
        return f"<borrow {kind} '{self.name}'>"


class Environment:
    """Değişken değerlerini saklayan ortam. Scope yönetimi yapar."""

    def __init__(self, parent: Optional[Environment] = None, name: str = "<env>"):
        self.parent = parent
        self.name = name
        self.values: Dict[str, Any] = {}
        self.constants: set[str] = set()  # const değişkenler
        self.functions: Dict[str, FnDecl] = {}  # fonksiyon tanımları
        self.stack_vars: set[str] = set()  # @stack ile tanımlanan değişkenler
        self.own_vars: set[str] = set()  # @own ile tanımlanan değişkenler
        self.borrows: list = []  # bu scope'ta üretilen BorrowRef'ler (v1, §4)

    def define(self, name: str, value: Any, is_const: bool = False) -> None:
        """Yeni değişken tanımla."""
        self.values[name] = value
        if is_const:
            self.constants.add(name)

    def define_function(self, name: str, func_node: FnDecl) -> None:
        """Yeni fonksiyon tanımla."""
        self.functions[name] = func_node

    def get(self, name: str) -> Any:
        """Değişken değeri al. Scope hierarchy'de arama yap.
        StackArray ölü ise panic (mevcut davranış). OwnValue wrapper olarak
        döner; canlılık denetimi eval_Ident / to_gc / free tarafından yapılır."""
        if name in self.values:
            v = self.values[name]
            if isinstance(v, StackArray) and not v.alive:
                raise StackPanic(
                    f"Stack variable '{name}' used after its scope ended "
                    f"(stack-use-after-scope)")
            return v
        if self.parent:
            return self.parent.get(name)
        raise NameError(f"Undefined variable: {name}")

    def get_function(self, name: str) -> Optional[FnDecl]:
        """Fonksiyon tanımı al. Scope hierarchy'de arama yap."""
        if name in self.functions:
            return self.functions[name]
        if self.parent:
            return self.parent.get_function(name)
        return None

    def set(self, name: str, value: Any) -> None:
        """Değişken değerini güncelle."""
        if name in self.constants:
            raise TypeError(f"Cannot assign to constant: {name}")
        # @stack değişken ölen scope'un değeriyle değiştirilemez / kaçırılamaz
        if name in self.stack_vars and name in self.values:
            old = self.values[name]
            if isinstance(old, StackArray) and not old.alive:
                raise StackPanic(
                    f"Stack variable '{name}' used after its scope ended "
                    f"(stack-use-after-scope)")
            if isinstance(value, StackArray) and not value.alive:
                raise StackPanic(
                    "Dead stack value assigned (stack-use-after-scope)")
            if isinstance(old, StackArray) and not isinstance(value, StackArray):
                raise TypeError(
                    f"Cannot assign non-stack value to stack array '{name}'")
            if (isinstance(old, StackArray) and isinstance(value, StackArray)
                    and old.size != value.size):
                raise TypeError(
                    f"Stack array size mismatch for '{name}': size {old.size}, "
                    f"got size {value.size}")

        # Mevcut scope'da ara
        if name in self.values:
            self.values[name] = value
            return

        # Parent scope'larda ara
        if self.parent:
            try:
                self.parent.get(name)  # var mı diye kontrol et
                self.parent.set(name, value)
                return
            except NameError:
                pass

        # Yoksa yeni oluştur
        self.values[name] = value

    def exists(self, name: str) -> bool:
        """Değişken var mı kontrol et."""
        if name in self.values:
            return True
        if self.parent:
            return self.parent.exists(name)
        return False

    def close(self) -> None:
        """Scope kapanışı: borrow → own → stack sırası (release-before-kill, v1)."""
        for b in self.borrows:
            if isinstance(b, BorrowRef):
                b.release()
        self.borrows.clear()
        for name in list(self.own_vars):
            v = self.values.get(name)
            if isinstance(v, OwnValue) and v.alive:
                v.kill()
        for name in list(self.stack_vars):
            v = self.values.get(name)
            if isinstance(v, StackArray) and v.alive:
                v.kill()


# ─── Built-in Fonksiyonlar ───────────────────────────────────────────────────────


class BuiltinFunction:
    """Built-in fonksiyon wrapper'ı."""

    def __init__(self, name: str, func: Callable, param_count: int = -1):
        self.name = name
        self.func = func
        self.param_count = param_count  # -1 = değişken sayıda parametre

    def __call__(self, *args):
        if self.param_count >= 0 and len(args) != self.param_count:
            raise TypeError(
                f"{self.name} expects {self.param_count} arguments, got {len(args)}"
            )
        return self.func(*args)

    def __repr__(self):
        return f"<builtin {self.name}>"


def builtin_print(*args):
    """print built-in fonksiyonu."""
    output = " ".join(str(arg) for arg in args)
    print(output)
    return None


def builtin_len(obj):
    """len built-in fonksiyonu."""
    if isinstance(obj, StackArray):
        return len(obj)
    if isinstance(obj, (list, str)):
        return len(obj)
    raise TypeError(f"len() expects a list or string, got {type(obj).__name__}")


def builtin_type(obj):
    """type built-in fonksiyonu."""
    if isinstance(obj, bool):
        return "bool"
    elif isinstance(obj, int):
        return "int"
    elif isinstance(obj, float):
        return "float"
    elif isinstance(obj, str):
        return "str"
    elif obj is None:
        return "null"
    elif isinstance(obj, StackArray):
        return "stack_array"
    elif isinstance(obj, list):
        return "list"
    elif isinstance(obj, dict):
        return "dict"
    elif isinstance(obj, BuiltinFunction):
        return "function"
    else:
        return "object"


def builtin_range(*args):
    """range built-in fonksiyonu."""
    if len(args) == 1:
        return list(range(int(args[0])))
    elif len(args) == 2:
        return list(range(int(args[0]), int(args[1])))
    elif len(args) == 3:
        return list(range(int(args[0]), int(args[1]), int(args[2])))
    raise TypeError("range() takes 1-3 arguments")


def builtin_input(prompt=""):
    """input built-in fonksiyonu."""
    return input(prompt)


def builtin_str(obj):
    if obj is None:
        return "null"
    if isinstance(obj, bool):
        return "true" if obj else "false"
    if isinstance(obj, StackArray):
        obj._check()
        return "[" + ", ".join(builtin_str(x) for x in obj._items) + "]"
    return str(obj)


def builtin_int(obj):
    return int(obj)


def builtin_float(obj):
    return float(obj)


def builtin_bool(obj):
    if isinstance(obj, str):
        return obj.lower() not in ("false", "null", "0", "")
    return bool(obj)


def builtin_min(*args):
    if len(args) == 1 and isinstance(args[0], list):
        return min(args[0])
    return min(args)


def builtin_max(*args):
    if len(args) == 1 and isinstance(args[0], list):
        return max(args[0])
    return max(args)


def builtin_abs(obj):
    return abs(obj)


def builtin_sqrt(obj):
    import math

    return math.sqrt(float(obj))


def builtin_println(*args):
    """println built-in fonksiyonu."""
    output = " ".join(str(arg) for arg in args)
    print(output)
    return None


def builtin_push(lst, val):
    """list.push(val) built-in."""
    if not isinstance(lst, list):
        raise TypeError("push() expects a list")
    lst.append(val)
    return None


def builtin_pop(lst):
    """list.pop() built-in."""
    if not isinstance(lst, list):
        raise TypeError("pop() expects a list")
    return lst.pop()


def builtin_keys(d):
    """dict.keys() built-in."""
    return list(d.keys())


def builtin_values(d):
    """dict.values() built-in."""
    return list(d.values())


# ─── Runtime Hataları ─────────────────────────────────────────────────────────────


class NexRuntimeError(Exception):
    """Interpreter çalışma zamanı hatası."""

    def __init__(self, message: str, node: Optional[Node] = None):
        super().__init__(message)
        self.node = node
        self.line = getattr(node, "line", 0) if node is not None else 0
        self.col = getattr(node, "col", 0) if node is not None else 0


class StackPanic(NexRuntimeError):
    """@stack kural ihlali (boyut, scope-dışı kullanım, mutasyon ihlali)."""
    pass


class OwnPanic(NexRuntimeError):
    """@own sahiplik ihlali (use-after-free / scope sonrası kullanım)."""
    pass


class ReturnException(Exception):
    """Return statement'i için özel exception."""

    def __init__(self, value: Any):
        super().__init__()
        self.value = value


class BreakException(Exception):
    """Break statement'i için özel exception."""

    pass


class ContinueException(Exception):
    """Continue statement'i için özel exception."""

    pass


# ─── Nex Function (kullanıcı tanımlı) ───────────────────────────────────────────


class Function:
    """Kullanıcı tanımlı fonksiyon wrapper'ı."""

    def __init__(self, decl: FnDecl, closure: Environment):
        self.decl = decl
        self.closure = closure
        self.name = decl.name or "<lambda>"

    def call(self, interpreter: Interpreter, args: list) -> Any:
        """Fonksiyonu çalıştır."""
        func_env = Environment(self.closure, f"fn:{self.name}")

        # Constructor (init.call(self, [self_val] + args)) ve bound method
        # (BoundMethod.call) argümanların başına instance'ı ekler; decl
        # parametrelerinde 'self' tanımlı olmadığından burada örtük bağlanır.
        # Checker arg sayısı uyuşmazlığını zaten reddettiği için +1 şekli
        # yalnızca instance-binding durumunda oluşabilir (ilk arg bir
        # NexInstance değilse normal fonksiyon gibi davran).
        call_args = args
        if (len(args) == len(self.decl.params) + 1
                and isinstance(args[0], NexInstance)):
            func_env.define("self", args[0])
            call_args = args[1:]

        for i, param in enumerate(self.decl.params):
            if i < len(call_args):
                func_env.define(param.name, call_args[i])
            elif param.default is not None:
                dval = interpreter.evaluate(param.default)
                # Default değer de kaçış bağlamıdır: stack dizisi referanssız kopyalanır
                dval = interpreter._conv(dval)
                func_env.define(param.name, dval)
            else:
                raise TypeError(f"Missing argument for parameter '{param.name}'")

        old_env = interpreter.current_env
        interpreter.current_env = func_env
        try:
            if self.decl.body:
                interpreter.evaluate(self.decl.body)
            return None
        except ReturnException as ret:
            if isinstance(ret.value, StackArray):
                return ret.value.to_list()  # heap kopyası
            return ret.value
        finally:
            func_env.close()
            interpreter.current_env = old_env

    def __repr__(self):
        return f"<function {self.name}>"


class NexRange:
    """Nex range tipi (0..10 gibi)."""

    def __init__(self, start: int, end: int):
        self.start = start
        self.end = end

    def __iter__(self):
        return iter(range(self.start, self.end))

    def to_list(self):
        return list(range(self.start, self.end))

    def __repr__(self):
        return f"{self.start}..{self.end}"


class NexClass:
    """Kullanıcı tanımlı sınıf wrapper'ı."""

    def __init__(self, decl: ClassDecl, closure: Environment):
        self.decl = decl
        self.name = decl.name
        self.closure = closure

    def call(self, interpreter: Interpreter, args: list) -> NexInstance:
        instance = NexInstance(self)
        for field in self.decl.fields:
            default = interpreter.evaluate(field.default) if field.default else None
            instance.fields[field.name] = default
        for method in self.decl.methods:
            instance.methods[method.name] = Function(method, interpreter.current_env)
        init = instance.methods.get("init") or instance.methods.get("__init__")
        if init:
            init.call(interpreter, [instance] + args)
        return instance

    def __repr__(self):
        return f"<class {self.name}>"


class NexInstance:
    """Sınıf örneği."""

    def __init__(self, klass: NexClass):
        self.klass = klass
        self.fields: Dict[str, Any] = {}
        self.methods: Dict[str, Any] = {}

    def get_attr(self, name: str) -> Any:
        if name in self.fields:
            return self.fields[name]
        if name in self.methods:
            return BoundMethod(self, self.methods[name])
        raise AttributeError(f"'{self.klass.name}' has no attribute '{name}'")

    def set_attr(self, name: str, value: Any) -> None:
        self.fields[name] = value

    def __repr__(self):
        fields = ", ".join(f"{k}={v!r}" for k, v in self.fields.items())
        return f"<{self.klass.name} {{{fields}}}>"


class BoundMethod:
    """Instance'a bağlı metod."""

    def __init__(self, instance: NexInstance, fn: Function):
        self.instance = instance
        self.fn = fn

    def call(self, interpreter: Interpreter, args: list) -> Any:
        return self.fn.call(interpreter, [self.instance] + args)

    def __repr__(self):
        return f"<bound method {self.fn.name}>"


# ─── Interpreter ─────────────────────────────────────────────────────────────────


class Interpreter:
    """Ana interpreter sınıfı. AST'yi çalıştırır."""

    def __init__(self):
        self.global_env = Environment(name="global")
        self.current_env = self.global_env
        self.setup_builtins()

    def setup_builtins(self):
        """Built-in fonksiyonları tanımla."""
        builtins = {
            "print": BuiltinFunction("print", builtin_print, -1),
            "println": BuiltinFunction("println", builtin_println, -1),
            "len": BuiltinFunction("len", builtin_len, 1),
            "type": BuiltinFunction("type", builtin_type, 1),
            "range": BuiltinFunction("range", builtin_range, -1),
            "input": BuiltinFunction("input", builtin_input, -1),
            "str": BuiltinFunction("str", builtin_str, 1),
            "int": BuiltinFunction("int", builtin_int, 1),
            "float": BuiltinFunction("float", builtin_float, 1),
            "bool": BuiltinFunction("bool", builtin_bool, 1),
            "min": BuiltinFunction("min", builtin_min, -1),
            "max": BuiltinFunction("max", builtin_max, -1),
            "abs": BuiltinFunction("abs", builtin_abs, 1),
            "sqrt": BuiltinFunction("sqrt", builtin_sqrt, 1),
            "push": BuiltinFunction("push", builtin_push, 2),
            "pop": BuiltinFunction("pop", builtin_pop, 1),
            "keys": BuiltinFunction("keys", builtin_keys, 1),
            "values": BuiltinFunction("values", builtin_values, 1),
        }
        for name, fn in builtins.items():
            self.global_env.define(name, fn, is_const=True)

    def push_env(self, name: str = "<scope>") -> Environment:
        """Yeni scope oluştur."""
        env = Environment(self.current_env, name)
        self.current_env = env
        return env

    def pop_env(self) -> None:
        """Mevcut scope'dan çık. @stack değerleri scope ile birlikte ölür."""
        env = self.current_env
        if env.parent:
            self.current_env = env.parent
        env.close()

    def error(self, message: str, node: Node) -> None:
        """Runtime hatası fırlat."""
        raise NexRuntimeError(message, node)

    def _conv(self, value: Any, node: Optional[Node] = None) -> Any:
        """Heap bağlamında depolanacak değer: StackArray -> bağımsız heap kopyası.

        Kaçış kuralı stack dizilere sınırlı DEĞİLDİR — stack dizinin sızdığı
        her kapalı kap (list/dict/set/tuple literal'i, sınıf alanı, dict
        anahtarı, pipe argümanı, fonksiyon default'u, match konusu) aynı
        kurala tabi; aksi halde aliasing + stack-use-after-scope olur."""
        # Wrapper'lar kapların içine sızmamalı: sahiplik proxy'si kopyalanamaz
        # (audit B — güvenlik ağı; checker bu yolları zaten reddeder).
        if isinstance(value, OwnValue):
            raise OwnPanic(
                "cannot copy an @own value into a GC context "
                "(move it with to_gc(x) first)", node)
        if isinstance(value, BorrowRef):
            raise OwnPanic(
                "borrowed references cannot cross context boundaries yet (v1)",
                node)
        if isinstance(value, StackArray):
            return value.to_list()
        if isinstance(value, list):
            return [self._conv(x, node) for x in value]
        if isinstance(value, tuple):
            return tuple(self._conv(x, node) for x in value)
        if isinstance(value, dict):
            return {self._conv(k, node): self._conv(v, node)
                    for k, v in value.items()}
        return value

    def _nex_deepcopy(self, value: Any, node: Optional[Node] = None) -> Any:
        """to_own için wrapper-bilinçli derin kopya (bağımsız @own nesne)."""
        # Referans/sahiplik wrapper'ları kopyalanamaz
        if isinstance(value, OwnValue):
            raise OwnPanic("cannot deepcopy an @own value", node)
        # BorrowRef/RawRef ileride gelebilir — wrapper ise reddet
        tname = type(value).__name__
        if tname in ("BorrowRef", "RawRef"):
            raise OwnPanic(f"cannot deepcopy a {tname}", node)
        if isinstance(value, StackArray):
            # stack dizisi @own içine kaçarken düz heap verisine dönüşür
            return [self._nex_deepcopy(x, node) for x in value.to_list()]
        if isinstance(value, list):
            return [self._nex_deepcopy(x, node) for x in value]
        if isinstance(value, tuple):
            return tuple(self._nex_deepcopy(x, node) for x in value)
        if isinstance(value, set):
            return {self._nex_deepcopy(x, node) for x in value}
        if isinstance(value, dict):
            return {self._nex_deepcopy(k, node): self._nex_deepcopy(v, node)
                    for k, v in value.items()}
        if isinstance(value, NexInstance):
            inst = NexInstance(value.klass)
            inst.methods = value.methods  # metodlar paylaşımlı (sınıfa ait)
            inst.fields = {k: self._nex_deepcopy(v, node)
                           for k, v in value.fields.items()}
            return inst
        # Primitifler ve diğer Python nesneleri
        try:
            return copy.deepcopy(value)
        except Exception:
            return value

    def eval_MatchExpr(self, node: MatchExpr) -> Any:
        subject = self.evaluate(node.subject)
        if isinstance(subject, StackArray):
            subject = subject.to_list()
        for arm in node.arms:
            if self._match_pattern(arm.pattern, subject):
                if arm.guard is not None:
                    if not self.is_truthy(self.evaluate(arm.guard)):
                        continue
                return self.evaluate(arm.body)
        return None

    # ── @stack yardımcıları ──────────────────────────────────────

    def _stack_elem_default(self, type_node) -> Any:
        """[T; N] tipindeki T için varsayılan eleman değeri."""
        if isinstance(type_node, TypeName):
            return {"int": 0, "float": 0.0, "bool": False,
                    "str": "", "byte": 0}.get(type_node.name)
        return None

    def _stack_expected(self, node: VarDecl):
        """@stack bildirimi için (boyut, eleman tipi adı) döndür; yoksa (None, None)."""
        ta = node.type_ann
        if isinstance(ta, TypeStackArray) and isinstance(ta.size, IntLit):
            elem = ta.elem.name if isinstance(ta.elem, TypeName) else None
            return int(ta.size.value), elem
        return None, None

    def evaluate(self, node: Node) -> Any:
        """AST düğümünü değerlendir (Visitor pattern)."""
        method_name = f"eval_{type(node).__name__}"
        method = getattr(self, method_name, None)
        if method:
            return method(node)
        raise NotImplementedError(f"No eval method for {type(node).__name__}")

    def interpret(self, program: Program) -> Any:
        """Tüm programı çalıştır."""
        try:
            result = None
            main_fn = None
            main_was_called_explicitly = False

            for stmt in program.stmts:
                result = self.evaluate(stmt)

                # Top-level'de main(...) çağrısı yapıldı mı takip et
                if isinstance(stmt, ExprStmt) and isinstance(stmt.expr, CallExpr):
                    callee = stmt.expr.callee
                    if isinstance(callee, Ident) and callee.name == "main":
                        main_was_called_explicitly = True

                # Top-level main fonksiyon tanımını yakala
                if isinstance(stmt, FnDecl) and stmt.name == "main":
                    try:
                        maybe_main = self.current_env.get("main")
                        if hasattr(maybe_main, "call"):
                            main_fn = maybe_main
                    except NameError:
                        pass

            # Eğer kullanıcı explicit olarak main() çağırmadıysa auto-call yap
            if main_fn is not None and not main_was_called_explicitly:
                result = main_fn.call(self, [])

            return result
        except NexRuntimeError as e:
            print(f"Runtime Error at line {e.line}:{e.col} — {e}")
            return None
        except Exception as e:
            print(f"Internal Error: {e}")
            return None

    # ── Literal Değerlendirmeleri ─────────────────────────────────────────────

    def eval_IntLit(self, node: IntLit) -> int:
        return int(node.value)

    def eval_FloatLit(self, node: FloatLit) -> float:
        return float(node.value)

    def eval_StrLit(self, node: StrLit) -> str:
        return node.value

    def eval_BoolLit(self, node: BoolLit) -> bool:
        return node.value == "true" if isinstance(node.value, str) else bool(node.value)

    def eval_NullLit(self, node: NullLit) -> None:
        return None

    # ── İfadeler ────────────────────────────────────────────────────────────────

    def _env_get(self, name: str, node: Node) -> Any:
        """env.get + node'suz StackPanic/OwnPanic'e konum ekle."""
        try:
            return self.current_env.get(name)
        except (StackPanic, OwnPanic) as e:
            if e.node is None:
                raise type(e)(str(e), node) from e
            raise

    def _env_get_raw(self, name: str, node: Node) -> Any:
        """@own/to_gc/free için ham wrapper döndürür (_env_get ile aynı)."""
        return self._env_get(name, node)

    def _env_set(self, name: str, value: Any, node: Node) -> None:
        try:
            self.current_env.set(name, value)
        except (StackPanic, OwnPanic) as e:
            if e.node is None:
                raise type(e)(str(e), node) from e
            raise

    def eval_Ident(self, node: Ident) -> Any:
        v = self._env_get(node.name, node)
        if isinstance(v, StackArray):
            v._check()
            return v
        if isinstance(v, OwnValue):
            v._check()
            return v._value  # opak: normal bağlamlar checker tarafından engellenir
        return v

    def eval_BinaryExpr(self, node: BinaryExpr) -> Any:
        left = self.evaluate(node.left)
        right = self.evaluate(node.right)
        if isinstance(left, StackArray):
            left = left.to_list()
        if isinstance(right, StackArray):
            right = right.to_list()

        if node.op == "+":
            if isinstance(left, str) or isinstance(right, str):
                return str(left) + str(right)
            return left + right
        elif node.op == "-":
            return left - right
        elif node.op == "*":
            return left * right
        elif node.op == "/":
            if right == 0:
                self.error("Division by zero", node)
            return left / right
        elif node.op == "//":
            if right == 0:
                self.error("Division by zero", node)
            return int(left // right)
        elif node.op == "%":
            return left % right
        elif node.op == "==":
            return left == right
        elif node.op == "!=":
            return left != right
        elif node.op == "<":
            return left < right
        elif node.op == "<=":
            return left <= right
        elif node.op == ">":
            return left > right
        elif node.op == ">=":
            return left >= right
        elif node.op == "and":
            return left and right
        elif node.op == "or":
            return left or right
        elif node.op == "??":
            return left if left is not None else right
        else:
            self.error(f"Unknown binary operator: {node.op}", node)

    def eval_UnaryExpr(self, node: UnaryExpr) -> Any:
        operand = self.evaluate(node.operand)

        if node.op == "-":
            return -operand
        elif node.op == "not":
            return not operand
        else:
            self.error(f"Unknown unary operator: {node.op}", node)

    def eval_BorrowExpr(self, node: BorrowExpr) -> Any:
        name = node.operand.name  # parser v1: yalnızca Ident
        wrapper = self._env_get_raw(name, node)
        if not isinstance(wrapper, OwnValue):
            self.error("only @own values can be borrowed (v1)", node)
            ref = BorrowRef(wrapper, node.mutable, name, node)
            self.current_env.borrows.append(ref)
            return ref
        ref = BorrowRef(wrapper, node.mutable, name, node)
        self.current_env.borrows.append(ref)
        return ref

    # ── Statements ───────────────────────────────────────────────────────────────

    def eval_ExprStmt(self, node: ExprStmt) -> Any:
        return self.evaluate(node.expr)

    def eval_VarDecl(self, node: VarDecl) -> Any:
        if node.mem_mode == "own":
            if node.value is None:
                self.error(
                    f"@own declaration '{node.name}' must be initialized from "
                    f"to_own(...)", node)
            value = self.evaluate(node.value)
            if not isinstance(value, OwnValue):
                self.error(
                    f"@own declaration '{node.name}' must be initialized from "
                    f"to_own(...)", node)
            self.current_env.define(node.name, value, is_const=not node.mutable)
            self.current_env.own_vars.add(node.name)
            return value

        if node.mem_mode == "stack":
            size, _elem = self._stack_expected(node)
            if size is None:
                self.error(
                    f"@stack declaration '{node.name}' requires a fixed-size "
                    f"array type such as [int; 4]", node)
            raw = None
            if node.value is not None:
                raw = self.evaluate(node.value)
                if isinstance(raw, StackArray):
                    raw = raw._items  # canlı listenin kopyası aşağıda alınır
                if not isinstance(raw, list):
                    self.error(
                        f"@stack declaration '{node.name}' requires an array "
                        f"literal of {size} element(s)", node)
                if len(raw) != size:
                    raise StackPanic(
                        f"Stack array size mismatch for '{node.name}': declared "
                        f"size {size}, got {len(raw)} element(s)", node)
                items = list(raw)
            else:
                default = self._stack_elem_default(node.type_ann.elem)
                items = [default] * size
            arr = StackArray(items, size)
            self.current_env.define(node.name, arr, is_const=not node.mutable)
            self.current_env.stack_vars.add(node.name)
            return arr

        value = None
        if node.value:
            value = self.evaluate(node.value)
            if isinstance(value, StackArray):
                value = value.to_list()  # heap kopyası; stack ömrü devretmez

        self.current_env.define(node.name, value, is_const=not node.mutable)
        return value

    def eval_Block(self, node: Block) -> Any:
        self.push_env("<block>")
        try:
            result = None
            for stmt in node.stmts:
                result = self.evaluate(stmt)
            return result
        finally:
            self.pop_env()

    def eval_ReturnStmt(self, node: ReturnStmt) -> Any:
        # Bu basit implementasyonda return değerini fırlatıyoruz
        # Daha gelişmiş versiyonda call stack yönetimi gerekir
        value = None
        if node.value:
            value = self.evaluate(node.value)
            # Sahiplik wrapper'ı fonksiyon sınırını geçemez (§4 v1) —
            # checker reddediyor; burada konumlu güvenlik ağı.
            if isinstance(value, OwnValue):
                raise OwnPanic(
                    "cannot return an @own value from a function "
                    "(move it with to_gc(x) first)", node)
            if isinstance(value, BorrowRef):
                raise OwnPanic(
                    "borrowed references cannot cross function boundaries "
                    "yet (v1)", node)
            # Derin kaçış kuralı: return değeri stack dizisi içerebilir
            # ([stackBuf] gibi) — kopyasız dönmesin (audit B).
            value = self._conv(value, node)
        raise ReturnException(value)

    def eval_IfStmt(self, node: IfStmt) -> Any:
        condition = self.evaluate(node.condition)
        if self.is_truthy(condition):
            return self.evaluate(node.then_block)

        # elif blokları
        for elif_condition, elif_block in node.elif_arms:
            elif_cond = self.evaluate(elif_condition)
            if self.is_truthy(elif_cond):
                return self.evaluate(elif_block)

        # else bloğu
        if node.else_block:
            return self.evaluate(node.else_block)

    def eval_WhileStmt(self, node: WhileStmt) -> Any:
        result = None
        while True:
            condition = self.evaluate(node.condition)
            if not self.is_truthy(condition):
                break
            try:
                result = self.evaluate(node.body)
            except BreakException:
                break
            except ContinueException:
                continue
        return result

    def eval_ForStmt(self, node: ForStmt) -> Any:
        iterable = self.evaluate(node.iterable)
        result = None

        if isinstance(iterable, StackArray):
            sequence = iterable.to_list()
        elif isinstance(iterable, NexRange):
            sequence = iterable
        elif isinstance(iterable, (list, range)):
            sequence = iterable
        elif isinstance(iterable, str):
            sequence = list(iterable)
        elif isinstance(iterable, dict):
            sequence = list(iterable.keys())
        else:
            self.error(f"Cannot iterate over {type(iterable).__name__}", node)
            return None

        self.push_env("<for>")
        try:
            for item in sequence:
                if isinstance(node.target, Ident):
                    self.current_env.define(node.target.name, item)
                elif isinstance(node.target, TupleLit):
                    if not isinstance(item, (list, tuple)):
                        item = [item]
                    for idx, ident in enumerate(node.target.elements):
                        if isinstance(ident, Ident) and idx < len(item):
                            self.current_env.define(ident.name, item[idx])

                try:
                    result = self.evaluate(node.body)
                except BreakException:
                    break
                except ContinueException:
                    continue
        finally:
            self.pop_env()

        return result

    def eval_CallExpr(self, node: CallExpr) -> Any:
        # @own sahiplik builtin'leri (checker şekil/kuralları doğrular)
        if isinstance(node.callee, Ident) and node.callee.name in ("to_own", "to_gc", "free"):
            name = node.callee.name
            if not node.args:
                self.error(f"{name} requires exactly 1 argument", node)
            arg = node.args[0]
            if name == "to_own":
                # Arg herhangi bir ifade olabilir (to_own([1,2]), to_own(f(x))).
                val = self.evaluate(arg)
                # @own kaynak zaten sızmamalı (checker engeller); güvenlik ağı
                if isinstance(val, OwnValue):
                    self.error("to_own requires a GC/non-own value", node)
                copied = self._nex_deepcopy(val, node)
                oname = arg.name if isinstance(arg, Ident) else "<own>"
                return OwnValue(copied, name=oname)
            # to_gc / free: değişken referansı zorunlu (move)
            if not isinstance(arg, Ident):
                self.error(f"{name} requires a variable reference", node)
            wrapper = self._env_get_raw(arg.name, node)
            if not isinstance(wrapper, OwnValue):
                self.error(f"{name} requires an @own variable", node)
            wrapper._check()
            if wrapper.shared > 0 or wrapper.mut_borrowed > 0:
                raise OwnPanic(
                    f"cannot move/free '{arg.name}' while borrowed", node)
            if name == "to_gc":
                val = wrapper._value
                wrapper.kill()
                return val
            # free
            wrapper.kill()
            return None

        # super(args) — base class init cagirisi
        if isinstance(node.callee, Ident) and node.callee.name == "super":
            try:
                self_val = self.current_env.get("self")
            except NameError:
                self.error("super() called outside of class method", node)
                return None
            base_name = self_val.klass.decl.base
            if not base_name:
                self.error("super() called in class with no base", node)
                return None
            try:
                base_cls = self.current_env.get(base_name)
            except NameError:
                self.error(f"Base class '{base_name}' not found", node)
                return None
            args = [self.evaluate(a) for a in node.args]
            init = next(
                (
                    Function(m, self.current_env)
                    for m in base_cls.decl.methods
                    if m.name in ("init", "__init__")
                ),
                None,
            )
            if init:
                init.call(self, [self_val] + args)
            return None

        callee = self.evaluate(node.callee)
        args = [
            a.to_list() if isinstance(a, StackArray) else a
            for a in (self.evaluate(arg) for arg in node.args)
        ]

        if isinstance(callee, BuiltinFunction):
            # Wrapper temizliği: borrow/sahiplik proxy'leri fonksiyon
            # sınırını (builtin dahil) geçemez (§4 v1); konumsuz
            # 'Internal Error' yerine konumlu hata ver.
            for i, av in enumerate(args):
                if isinstance(av, BorrowRef):
                    at = node.args[i] if i < len(node.args) else node
                    self.error(
                        "borrowed references cannot cross function boundaries yet (v1)",
                        at)
                if isinstance(av, OwnValue) and av.alive:
                    at = node.args[i] if i < len(node.args) else node
                    self.error(
                        f"cannot pass @own value as argument {i + 1}; "
                        f"move it with to_gc(x) first", at)
            return callee(*args)
        elif hasattr(callee, "call"):
            return callee.call(self, args)
        elif callable(callee):
            return callee(*args)
        else:
            self.error(f"'{callee}' is not callable", node)

    def eval_ListLit(self, node: ListLit) -> List[Any]:
        return [self._conv(self.evaluate(elem)) for elem in node.elements]

    def eval_DictLit(self, node: DictLit) -> Dict[Any, Any]:
        result = {}
        for key_node, value_node in node.pairs:
            key = self._conv(self.evaluate(key_node), node)
            value = self._conv(self.evaluate(value_node), node)
            try:
                result[key] = value
            except TypeError:
                # Konumlu hata: audit B — 'Internal Error: unhashable' yerine
                self.error(
                    f"Dictionary key must be hashable, got unhashable "
                    f"'{type(key).__name__}' (list/dict/set cannot be "
                    f"dict keys)", node)
        return result

    def eval_TupleLit(self, node: TupleLit) -> list:
        return [self._conv(self.evaluate(e), node) for e in node.elements]

    def eval_SetLit(self, node: SetLit) -> list:
        result = set()
        for e in node.elements:
            v = self._conv(self.evaluate(e), node)
            try:
                result.add(v)
            except TypeError:
                self.error(
                    f"Set element must be hashable, got unhashable "
                    f"'{type(v).__name__}' (list/dict/set cannot be "
                    f"set elements)", node)
        return list(result)

    def eval_RangeExpr(self, node: RangeExpr) -> NexRange:
        start = self.evaluate(node.start)
        end = self.evaluate(node.end)
        return NexRange(int(start), int(end))

    def eval_FnDecl(self, node: FnDecl) -> None:
        fn = Function(node, self.current_env)
        self.current_env.define(node.name, fn, is_const=False)
        return None

    def eval_Program(self, node: Program) -> Any:
        # Checker'daki _pre_declare ile tutarlılık: üst-düzey sınıflar önce
        # tanımlanır. Böylece tanımdan önce kullanım (forward reference) hem
        # --check'te hem runtime'da aynı sonucu verir.
        for stmt in node.stmts:
            if isinstance(stmt, ClassDecl):
                self.evaluate(stmt)
        result = None
        for stmt in node.stmts:
            if isinstance(stmt, ClassDecl):
                continue  # zaten tanımlandı
            result = self.evaluate(stmt)
        return result

    def eval_AssignExpr(self, node: AssignExpr) -> Any:
        value = self.evaluate(node.value)
        op = node.op

        if isinstance(node.target, Ident):
            name = node.target.name
            if op == "ASSIGN":
                cur = None
                try:
                    cur = self._env_get(name, node)
                except (NameError, RuntimeError):
                    cur = None
                # Borrow değişken yeniden ataması: eski borrow otomatik serbest (v1).
                if isinstance(cur, BorrowRef):
                    cur.release()
                if isinstance(cur, OwnValue):
                    if isinstance(value, OwnValue):
                        # to_own ile yeniden atama: yaşam döngüsü sıfırlanır
                        # (eski wrapper ölü/moved olsa bile geçerli — checker onaylı)
                        if cur.alive:
                            cur.kill()
                        self._env_set(name, value, node)
                        return value
                    cur._check()
                    raise OwnPanic(
                        f"@own variable '{name}' can only hold to_own(...) results",
                        node)
                if isinstance(cur, StackArray):
                    cur._check()
                    if isinstance(value, StackArray):
                        value._check()
                        if value.size != cur.size:
                            raise StackPanic(
                                f"Stack array size mismatch for '{name}': size "
                                f"{cur.size}, got size {value.size}", node)
                        cur._items = list(value._items)
                    elif isinstance(value, list):
                        if len(value) != cur.size:
                            raise StackPanic(
                                f"Stack array size mismatch for '{name}': size "
                                f"{cur.size}, got {len(value)} element(s)", node)
                        cur._items = list(value)
                    else:
                        raise StackPanic(
                            f"Cannot assign value of type "
                            f"'{type(value).__name__}' to stack array '{name}'",
                            node)
                    return cur
                if isinstance(value, StackArray):
                    # GC bağlamına kaçış heap kopyasıdır; stack ömrü devretmez.
                    value = value.to_list()
                self._env_set(name, value, node)
            else:
                current = self._env_get(name, node)
                if isinstance(current, OwnValue):
                    current._check()
                    raise OwnPanic(
                        f"compound assignment is not supported on @own variable '{name}'",
                        node)
                if op == "PLUS_EQ":
                    value = current + value
                elif op == "MINUS_EQ":
                    value = current - value
                elif op == "STAR_EQ":
                    value = current * value
                elif op == "SLASH_EQ":
                    value = current / value
                self._env_set(name, value, node)
            return value
        elif isinstance(node.target, IndexExpr):
            obj = self.evaluate(node.target.obj)
            idx = self.evaluate(node.target.index)
            # BorrowRef üzerinden yazma: mutability + per-access denetim.
            if isinstance(obj, BorrowRef):
                obj._check(node)
                if not obj.mutable:
                    raise OwnPanic(
                        f"cannot write through shared borrow '{obj.name}'", node)
                obj = obj.own._value
            # Depolama noktası da kaçış bağlamıdır: stack dizisi/dict anahtarı
            # buraya sızarken bağımsız kopya olmalı (audit B — a[0] = buf UAF).
            value = self._conv(value, node)
            idx = self._conv(idx, node)
            if isinstance(obj, StackArray):
                obj._check()
                if not isinstance(idx, int) or isinstance(idx, bool):
                    raise StackPanic(
                        f"Stack array index must be 'int', got "
                        f"'{type(idx).__name__}'", node)
                if idx < 0 or idx >= obj.size:
                    raise StackPanic(
                        f"Index {idx} out of bounds for stack array of size "
                        f"{obj.size}", node)
                if op == "ASSIGN":
                    obj[idx] = value
                else:
                    current = obj[idx]
                    if op == "PLUS_EQ":
                        obj[idx] = current + value
                    elif op == "MINUS_EQ":
                        obj[idx] = current - value
                    elif op == "STAR_EQ":
                        obj[idx] = current * value
                    elif op == "SLASH_EQ":
                        obj[idx] = current / value
                return value
            if not (isinstance(obj, dict) or isinstance(obj, list)):
                self.error(
                    f"Cannot index-assign into '{type(obj).__name__}'", node)
            if isinstance(obj, dict):
                try:
                    if op == "ASSIGN":
                        obj[idx] = value
                        return value
                    current = obj[idx]
                except TypeError:
                    # Konumlu hata: 'Internal Error: unhashable' yerine (audit B)
                    self.error(
                        f"Dictionary key must be hashable, got unhashable "
                        f"'{type(idx).__name__}' (list/dict/set cannot be "
                        f"dict keys)", node)
                if op == "PLUS_EQ":
                    obj[idx] = current + value
                elif op == "MINUS_EQ":
                    obj[idx] = current - value
                elif op == "STAR_EQ":
                    obj[idx] = current * value
                elif op == "SLASH_EQ":
                    obj[idx] = current / value
                return value
            if op == "ASSIGN":
                obj[idx] = value
            else:
                current = obj[idx]
                if op == "PLUS_EQ":
                    obj[idx] = current + value
                elif op == "MINUS_EQ":
                    obj[idx] = current - value
                elif op == "STAR_EQ":
                    obj[idx] = current * value
                elif op == "SLASH_EQ":
                    obj[idx] = current / value
            return value
        elif isinstance(node.target, MemberExpr):
            obj = self.evaluate(node.target.obj)
            if isinstance(obj, BorrowRef):
                obj._check(node)
                if not obj.mutable:
                    raise OwnPanic(
                        f"cannot write through shared borrow '{obj.name}'", node)
                obj = obj.own._value
            attr = node.target.member
            # Kaçış kuralı: stack dizisi sınıf alanına/dict'e atanırken bağımsız
            # heap kopyasına dönüşür; referans sızması aliasing üretir.
            value = self._conv(value)
            if isinstance(obj, NexInstance):
                if op == "ASSIGN":
                    obj.set_attr(attr, value)
                else:
                    current = obj.get_attr(attr)
                    if op == "PLUS_EQ":
                        obj.set_attr(attr, current + value)
                    elif op == "MINUS_EQ":
                        obj.set_attr(attr, current - value)
                    elif op == "STAR_EQ":
                        obj.set_attr(attr, current * value)
                    elif op == "SLASH_EQ":
                        obj.set_attr(attr, current / value)
            elif isinstance(obj, dict):
                if op == "ASSIGN":
                    obj[attr] = value
                else:
                    current = obj.get(attr, 0)
                    if op == "PLUS_EQ":
                        obj[attr] = current + value
                    elif op == "MINUS_EQ":
                        obj[attr] = current - value
                    elif op == "STAR_EQ":
                        obj[attr] = current * value
                    elif op == "SLASH_EQ":
                        obj[attr] = current / value
            return value
        else:
            self.error("Invalid assignment target", node)

    def eval_IndexExpr(self, node: IndexExpr) -> Any:
        obj = self.evaluate(node.obj)
        idx = self.evaluate(node.index)
        if isinstance(obj, BorrowRef):
            obj._check(node)
            obj = obj.own._value  # per-access çözümleme (snapshot yok)
        if isinstance(obj, StackArray):
            obj._check()
            if not isinstance(idx, int) or isinstance(idx, bool):
                raise StackPanic(
                    f"Stack array index must be 'int', got "
                    f"'{type(idx).__name__}'", node)
            if idx < 0 or idx >= obj.size:
                raise StackPanic(
                    f"Index {idx} out of bounds for stack array of size "
                    f"{obj.size}", node)
            return obj[idx]
        try:
            return obj[idx]
        except (IndexError, KeyError) as e:
            self.error(f"Index error: {e}", node)

    def eval_MemberExpr(self, node: MemberExpr) -> Any:
        obj = self.evaluate(node.obj)
        if isinstance(obj, BorrowRef):
            obj._check(node)
            obj = obj.own._value
        attr = node.member

        if isinstance(obj, NexInstance):
            if attr in obj.fields:
                fv = obj.fields[attr]
                if isinstance(fv, StackArray):
                    fv._check()
                return fv
            if attr in obj.methods:
                return BoundMethod(obj, obj.methods[attr])
            # Inheritance: base class metodlarini ara
            klass = obj.klass
            while klass.decl.base:
                base_name = klass.decl.base
                try:
                    base_cls = self.current_env.get(base_name)
                except NameError:
                    break
                if isinstance(base_cls, NexClass):
                    for m in base_cls.decl.methods:
                        if m.name == attr:
                            fn = Function(m, self.current_env)
                            return BoundMethod(obj, fn)
                    klass = base_cls
                else:
                    break
            self.error(f"'{obj.klass.name}' has no attribute '{attr}'", node)

        if isinstance(obj, dict):
            if attr in obj:
                return obj[attr]
            self.error(f"Key '{attr}' not found in dict", node)

        if isinstance(obj, StackArray):
            obj._check()
            _arr = obj
            if attr in ("length", "len"):
                return len(obj)
            if attr == "first":
                return obj._items[0] if obj._items else None
            if attr == "last":
                return obj._items[-1] if obj._items else None
            if attr in ("contains", "has"):
                def _contains(v, o=_arr):
                    o._check()
                    return v in o._items
                return BuiltinFunction("contains", _contains, 1)
            if attr == "join":
                def _join(sep, o=_arr):
                    o._check()
                    return sep.join(str(x) for x in o._items)
                return BuiltinFunction("join", _join, 1)
            if attr in ("isEmpty", "is_empty"):
                return len(obj) == 0
            if attr == "indexOf":
                def _indexOf(v, o=_arr):
                    o._check()
                    for i, item in enumerate(o._items):
                        if item == v:
                            return i
                    return None
                return BuiltinFunction("indexOf", _indexOf, 1)
            self.error(f"Stack array has no attribute '{attr}'", node)

        if isinstance(obj, list):
            _obj = obj
            if attr in ("push", "append"):
                return BuiltinFunction("push", lambda v, o=_obj: o.append(v), 1)
            if attr == "pop":
                return BuiltinFunction("pop", lambda o=_obj: o.pop(), 0)
            if attr == "reverse":
                return BuiltinFunction("reverse", lambda o=_obj: o.reverse(), 0)
            if attr == "sort":
                return BuiltinFunction("sort", lambda o=_obj: o.sort(), 0)
            if attr in ("contains", "has"):
                return BuiltinFunction("contains", lambda v, o=_obj: v in o, 1)
            if attr == "join":
                return BuiltinFunction(
                    "join", lambda sep, o=_obj: sep.join(str(x) for x in o), 1
                )
            props = {
                "length": len(obj),
                "len": len(obj),
                "first": obj[0] if obj else None,
                "last": obj[-1] if obj else None,
            }
            if attr in props:
                return props[attr]
            self.error(f"List has no attribute '{attr}'", node)

        if isinstance(obj, str):
            _obj = obj
            if attr == "upper":
                return BuiltinFunction("upper", lambda o=_obj: o.upper(), 0)
            if attr == "lower":
                return BuiltinFunction("lower", lambda o=_obj: o.lower(), 0)
            if attr in ("trim", "strip"):
                return BuiltinFunction("trim", lambda o=_obj: o.strip(), 0)
            if attr == "split":
                return BuiltinFunction(
                    "split", lambda sep=" ", o=_obj: o.split(sep), -1
                )
            if attr in ("starts_with", "startswith"):
                return BuiltinFunction(
                    "starts_with", lambda s, o=_obj: o.startswith(s), 1
                )
            if attr in ("ends_with", "endswith"):
                return BuiltinFunction("ends_with", lambda s, o=_obj: o.endswith(s), 1)
            if attr in ("contains", "has"):
                return BuiltinFunction("contains", lambda s, o=_obj: s in o, 1)
            if attr == "replace":
                return BuiltinFunction(
                    "replace", lambda a, b, o=_obj: o.replace(a, b), 2
                )
            if attr == "find":
                return BuiltinFunction("find", lambda s, o=_obj: o.find(s), 1)
            if attr in ("length", "len"):
                return len(obj)
            self.error(f"String has no attribute '{attr}'", node)

        self.error(
            f"Cannot access attribute '{attr}' on {type(obj).__name__}", node
        )

    def eval_NullCoalExpr(self, node: NullCoalExpr) -> Any:
        left = self.evaluate(node.left)
        if left is None:
            return self.evaluate(node.right)
        return left

    def eval_FStringExpr(self, node) -> str:
        result = ""
        for part in node.parts:
            if isinstance(part, str):
                result += part
            else:
                val = self.evaluate(part)
                result += builtin_str(val)
        return result

    def eval_PipeExpr(self, node) -> Any:
        val = self._conv(self.evaluate(node.left))
        fn = self.evaluate(node.right)
        if isinstance(fn, BuiltinFunction):
            return fn(val)
        if hasattr(fn, "call"):
            return fn.call(self, [val])
        self.error("Pipe sağ tarafı callable değil", node)

    def eval_BreakStmt(self, node: BreakStmt) -> Any:
        raise BreakException()

    def eval_ContinueStmt(self, node: ContinueStmt) -> Any:
        raise ContinueException()

    def eval_IfExpr(self, node: IfExpr) -> Any:
        cond = self.evaluate(node.condition)
        if self.is_truthy(cond):
            return self.evaluate(node.then_val)
        return self.evaluate(node.else_val)

    def eval_LambdaExpr(self, node: LambdaExpr) -> Any:
        closure = self.current_env

        class LambdaFunction:
            def __init__(self, params, body, closure_env):
                self.params = params
                self.body = body
                self.closure_env = closure_env
                self.name = "<lambda>"

            def call(self, interp, args):
                fn_env = Environment(self.closure_env, "<lambda>")
                for i, param in enumerate(self.params):
                    fn_env.define(param.name, args[i] if i < len(args) else None)
                old = interp.current_env
                interp.current_env = fn_env
                try:
                    result = interp.evaluate(self.body)
                    return result
                except ReturnException as ret:
                    return ret.value
                finally:
                    interp.current_env = old
                    # Lambda gövdesindeki declare edilen stack/own/borrow
                    # kaynakları için scope kapanışı (audit B — leak).
                    fn_env.close()

            def __repr__(self):
                return "<lambda>"

        return LambdaFunction(node.params, node.body, closure)

    def eval_TryCatchStmt(self, node: TryCatchStmt) -> Any:
        result = None
        try:
            result = self.evaluate(node.try_block)
        except NexRuntimeError as e:
            caught = False
            for arm in node.catch_arms:
                if arm.exc_type is None or arm.exc_type in ("NexRuntimeError", "Error"):
                    self.push_env("<catch>")
                    if arm.alias:
                        self.current_env.define(arm.alias, str(e))
                    try:
                        result = self.evaluate(arm.body)
                    finally:
                        self.pop_env()
                    caught = True
                    break
        finally:
            if node.finally_block:
                self.evaluate(node.finally_block)
        return result

    def eval_MatchStmt(self, node: MatchStmt) -> Any:
        subject = self._conv(self.evaluate(node.subject))
        for arm in node.arms:
            if self._match_pattern(arm.pattern, subject):
                return self.evaluate(arm.body)
        return None

    def _match_pattern(self, pattern: Node, value: Any) -> bool:
        if isinstance(pattern, Ident) and pattern.name == "_":
            return True
        if isinstance(pattern, (IntLit, FloatLit, StrLit, BoolLit, NullLit)):
            return self.evaluate(pattern) == value
        if isinstance(pattern, RangeExpr):
            lo = self.evaluate(pattern.start)
            hi = self.evaluate(pattern.end)
            try:
                return lo <= value <= hi
            except TypeError:
                return False
        if isinstance(pattern, Ident):
            return str(value) == pattern.name
        return False

    def eval_ImportStmt(self, node: ImportStmt) -> None:
        return None

    def eval_ModuleStmt(self, node: ModuleStmt) -> None:
        return None

    def eval_MultiVarDecl(self, node: MultiVarDecl) -> None:
        values = [self.evaluate(v) for v in node.values]
        for i, name in enumerate(node.names):
            val = values[i] if i < len(values) else None
            self.current_env.define(name, val, is_const=not node.mutable)
        return None

    def eval_ClassDecl(self, node: ClassDecl) -> None:
        klass = NexClass(node, self.current_env)
        self.current_env.define(node.name, klass, is_const=False)
        return None

    def eval_InterfaceDecl(self, node: InterfaceDecl) -> None:
        return None

    def eval_AwaitExpr(self, node: AwaitExpr) -> Any:
        return self.evaluate(node.expr)

    def eval_SpawnExpr(self, node: SpawnExpr) -> Any:
        return self.evaluate(node.expr)

    def eval_PrintStmt(self, node) -> None:
        value = self.evaluate(node.value)
        print(builtin_str(value))
        return None

    # ── Yardımcı Metodlar ───────────────────────────────────────────────────────

    def is_truthy(self, value: Any) -> bool:
        """Değerin truthy olup olmadığını kontrol et."""
        if value is None:
            return False
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        if isinstance(value, str):
            return len(value) > 0
        if isinstance(value, list):
            return len(value) > 0
        if isinstance(value, dict):
            return len(value) > 0
        return True
