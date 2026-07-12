"""
Nex Language — Interpreter
AST'yi çalıştırarak programı yorumlar.
Visitor pattern + environment stack.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from ast_nodes import *
from nex_types import *

# ─── Environment (Değişken Ortamı) ─────────────────────────────────────────────────────


class Environment:
    """Değişken değerlerini saklayan ortam. Scope yönetimi yapar."""

    def __init__(self, parent: Optional[Environment] = None, name: str = "<env>"):
        self.parent = parent
        self.name = name
        self.values: Dict[str, Any] = {}
        self.constants: set[str] = set()  # const değişkenler
        self.functions: Dict[str, FnDecl] = {}  # fonksiyon tanımları

    def define(self, name: str, value: Any, is_const: bool = False) -> None:
        """Yeni değişken tanımla."""
        self.values[name] = value
        if is_const:
            self.constants.add(name)

    def define_function(self, name: str, func_node: FnDecl) -> None:
        """Yeni fonksiyon tanımla."""
        self.functions[name] = func_node

    def get(self, name: str) -> Any:
        """Değişken değeri al. Scope hierarchy'de arama yap."""
        if name in self.values:
            return self.values[name]
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

    def __init__(self, message: str, node: Node):
        super().__init__(message)
        self.node = node
        self.line = node.line
        self.col = node.col


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

        for i, param in enumerate(self.decl.params):
            if i < len(args):
                func_env.define(param.name, args[i])
            elif param.default is not None:
                func_env.define(param.name, interpreter.evaluate(param.default))
            else:
                raise TypeError(f"Missing argument for parameter '{param.name}'")

        old_env = interpreter.current_env
        interpreter.current_env = func_env
        try:
            if self.decl.body:
                interpreter.evaluate(self.decl.body)
            return None
        except ReturnException as ret:
            return ret.value
        finally:
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
        """Mevcut scope'dan çık."""
        if self.current_env.parent:
            self.current_env = self.current_env.parent

    def error(self, message: str, node: Node) -> None:
        """Runtime hatası fırlat."""
        raise NexRuntimeError(message, node)

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

    def eval_Ident(self, node: Ident) -> Any:
        return self.current_env.get(node.name)

    def eval_BinaryExpr(self, node: BinaryExpr) -> Any:
        left = self.evaluate(node.left)
        right = self.evaluate(node.right)

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

    # ── Statements ───────────────────────────────────────────────────────────────

    def eval_ExprStmt(self, node: ExprStmt) -> Any:
        return self.evaluate(node.expr)

    def eval_VarDecl(self, node: VarDecl) -> Any:
        value = None
        if node.value:
            value = self.evaluate(node.value)

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

        if isinstance(iterable, NexRange):
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
        args = [self.evaluate(arg) for arg in node.args]

        if isinstance(callee, BuiltinFunction):
            return callee(*args)
        elif hasattr(callee, "call"):
            return callee.call(self, args)
        elif callable(callee):
            return callee(*args)
        else:
            self.error(f"'{callee}' is not callable", node)

    def eval_ListLit(self, node: ListLit) -> List[Any]:
        return [self.evaluate(elem) for elem in node.elements]

    def eval_DictLit(self, node: DictLit) -> Dict[Any, Any]:
        result = {}
        for key_node, value_node in node.pairs:
            key = self.evaluate(key_node)
            value = self.evaluate(value_node)
            result[key] = value
        return result

    def eval_TupleLit(self, node: TupleLit) -> list:
        return [self.evaluate(e) for e in node.elements]

    def eval_SetLit(self, node: SetLit) -> list:
        return list({self.evaluate(e) for e in node.elements})

    def eval_RangeExpr(self, node: RangeExpr) -> NexRange:
        start = self.evaluate(node.start)
        end = self.evaluate(node.end)
        return NexRange(int(start), int(end))

    def eval_FnDecl(self, node: FnDecl) -> None:
        fn = Function(node, self.current_env)
        self.current_env.define(node.name, fn, is_const=False)
        return None

    def eval_Program(self, node: Program) -> Any:
        result = None
        for stmt in node.stmts:
            result = self.evaluate(stmt)
        return result

    def eval_AssignExpr(self, node: AssignExpr) -> Any:
        value = self.evaluate(node.value)
        op = node.op

        if isinstance(node.target, Ident):
            name = node.target.name
            if op == "ASSIGN":
                self.current_env.set(name, value)
            else:
                current = self.current_env.get(name)
                if op == "PLUS_EQ":
                    value = current + value
                elif op == "MINUS_EQ":
                    value = current - value
                elif op == "STAR_EQ":
                    value = current * value
                elif op == "SLASH_EQ":
                    value = current / value
                self.current_env.set(name, value)
            return value
        elif isinstance(node.target, IndexExpr):
            obj = self.evaluate(node.target.obj)
            idx = self.evaluate(node.target.index)
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
            attr = node.target.member
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
        try:
            return obj[idx]
        except (IndexError, KeyError) as e:
            self.error(f"Index error: {e}", node)

    def eval_MemberExpr(self, node: MemberExpr) -> Any:
        obj = self.evaluate(node.obj)
        attr = node.member

        if isinstance(obj, NexInstance):
            if attr in obj.fields:
                return obj.fields[attr]
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
            self.error(f"'{{obj.klass.name}}' has no attribute '{{attr}}'", node)

        if isinstance(obj, dict):
            if attr in obj:
                return obj[attr]
            self.error(f"Key '{{attr}}' not found in dict", node)

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
            self.error(f"List has no attribute '{{attr}}'", node)

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
            self.error(f"String has no attribute '{{attr}}'", node)

        self.error(
            f"Cannot access attribute '{{attr}}' on {{type(obj).__name__}}", node
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
        val = self.evaluate(node.left)
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
                if arm.exc_type is None or True:
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
        subject = self.evaluate(node.subject)
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

    def eval_MatchExpr(self, node: MatchExpr) -> Any:
        subject = self.evaluate(node.subject)
        for arm in node.arms:
            if self._match_pattern(arm.pattern, subject):
                if arm.guard is not None:
                    if not self.is_truthy(self.evaluate(arm.guard)):
                        continue
                return self.evaluate(arm.body)
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
