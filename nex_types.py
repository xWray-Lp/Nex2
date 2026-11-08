"""
Nex Language — Type System
Tüm tip nesnelerini tanımlar. Stringlere değil, gerçek objelere dayalı.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional, Any


# ─── Temel Tip Sınıfı ────────────────────────────────────────────────────────

class NexType:
    def __eq__(self, other): return type(self) == type(other)
    def __hash__(self): return hash(repr(self))
    def is_nullable(self): return False
    def unwrap(self): return self          # nullable'ı açar

# ─── Primitive Tipler ─────────────────────────────────────────────────────────

class IntType(NexType):
    def __repr__(self): return "int"

class FloatType(NexType):
    def __repr__(self): return "float"

class StrType(NexType):
    def __repr__(self): return "str"

class BoolType(NexType):
    def __repr__(self): return "bool"

class ByteType(NexType):
    def __repr__(self): return "byte"

class NullType(NexType):
    def __repr__(self): return "null"

class VoidType(NexType):
    def __repr__(self): return "void"

class UnknownType(NexType):
    """Hata durumunda veya henüz çözülmemiş tipler için."""
    def __repr__(self): return "unknown"

# ─── Singletonlar ─────────────────────────────────────────────────────────────

INT     = IntType()
FLOAT   = FloatType()
STR     = StrType()
BOOL    = BoolType()
BYTE    = ByteType()
NULL    = NullType()
VOID    = VoidType()
UNKNOWN = UnknownType()


# ─── Bileşik Tipler ───────────────────────────────────────────────────────────

@dataclass
class NullableType(NexType):
    inner: NexType
    def __repr__(self): return f"{self.inner}?"
    def is_nullable(self): return True
    def unwrap(self): return self.inner
    def __eq__(self, other):
        return isinstance(other, NullableType) and self.inner == other.inner
    def __hash__(self): return hash(("nullable", self.inner))

@dataclass
class ListType(NexType):
    element: NexType
    def __repr__(self): return f"[{self.element}]"
    def __eq__(self, other):
        return isinstance(other, ListType) and self.element == other.element
    def __hash__(self): return hash(("list", self.element))

@dataclass
class DictType(NexType):
    key: NexType
    value: NexType
    def __repr__(self): return f"{{{self.key}: {self.value}}}"
    def __eq__(self, other):
        return isinstance(other, DictType) and self.key == other.key and self.value == other.value
    def __hash__(self): return hash(("dict", self.key, self.value))

@dataclass
class SetType(NexType):
    element: NexType
    def __repr__(self): return f"{{{self.element}}}"
    def __eq__(self, other):
        return isinstance(other, SetType) and self.element == other.element
    def __hash__(self): return hash(("set", self.element))

PRIMITIVES: dict[str, NexType] = {
    "int": INT, "float": FLOAT, "str": STR,
    "bool": BOOL, "byte": BYTE, "void": VOID,
    "null": NULL,
    "list": ListType(UNKNOWN), "dict": DictType(UNKNOWN, UNKNOWN),
    "set":  SetType(UNKNOWN),
}

@dataclass
class TupleType(NexType):
    elements: list[NexType]
    def __repr__(self): return f"({', '.join(str(e) for e in self.elements)})"
    def __eq__(self, other):
        return isinstance(other, TupleType) and self.elements == other.elements
    def __hash__(self): return hash(("tuple", tuple(self.elements)))

@dataclass
class FnType(NexType):
    params: list[NexType]
    ret:    NexType
    def __repr__(self):
        p = ", ".join(str(x) for x in self.params)
        return f"fn({p}) -> {self.ret}"
    def __eq__(self, other):
        return isinstance(other, FnType) and self.params == other.params and self.ret == other.ret
    def __hash__(self): return hash(("fn", tuple(self.params), self.ret))

@dataclass
class RefType(NexType):
    inner:   NexType
    mutable: bool = False
    def __repr__(self):
        return f"&{'mut ' if self.mutable else ''}{self.inner}"
    def __eq__(self, other):
        return isinstance(other, RefType) and self.inner == other.inner and self.mutable == other.mutable
    def __hash__(self): return hash(("ref", self.inner, self.mutable))

@dataclass
class ResultType(NexType):
    ok: NexType
    def __repr__(self): return f"Result[{self.ok}]"
    def __eq__(self, other):
        return isinstance(other, ResultType) and self.ok == other.ok
    def __hash__(self): return hash(("result", self.ok))

@dataclass
class StackArrayType(NexType):
    """@stack sabit boyutlu dizi: [int; 4]"""
    element: NexType
    size: int
    def __repr__(self): return f"[{self.element}; {self.size}]"
    def __eq__(self, other):
        return isinstance(other, StackArrayType) and self.element == other.element and self.size == other.size
    def __hash__(self): return hash(("stack_array", self.element, self.size))

# ─── Class / Interface Tipler ─────────────────────────────────────────────────

@dataclass
class ClassType(NexType):
    name:       str
    fields:     dict[str, NexType]         = field(default_factory=dict)
    methods:    dict[str, FnType]          = field(default_factory=dict)
    base:       Optional[ClassType]        = None
    interfaces: list[InterfaceType]        = field(default_factory=list)
    type_params: list[str]                 = field(default_factory=list)
    decl:       Any                        = field(default=None, repr=False, compare=False)

    def __repr__(self): return self.name
    def __eq__(self, other): return isinstance(other, ClassType) and self.name == other.name
    def __hash__(self): return hash(("class", self.name))

    def lookup_field(self, name: str) -> Optional[NexType]:
        if name in self.fields:
            return self.fields[name]
        if self.base:
            return self.base.lookup_field(name)
        return None

    def lookup_method(self, name: str) -> Optional[FnType]:
        if name in self.methods:
            return self.methods[name]
        if self.base:
            return self.base.lookup_method(name)
        return None

@dataclass
class InterfaceType(NexType):
    name:    str
    methods: dict[str, FnType] = field(default_factory=dict)
    def __repr__(self): return self.name
    def __eq__(self, other): return isinstance(other, InterfaceType) and self.name == other.name
    def __hash__(self): return hash(("iface", self.name))

@dataclass
class GenericType(NexType):
    """Henüz instantiate edilmemiş generic tip: Stack[T]"""
    name:        str
    type_params: list[str]
    def __repr__(self): return f"{self.name}[{', '.join(self.type_params)}]"
    def __eq__(self, other):
        return isinstance(other, GenericType) and self.name == other.name
    def __hash__(self): return hash(("generic", self.name))

@dataclass
class TypeVar(NexType):
    """Generic fonksiyon/sınıf type parametresi: T, U, K"""
    name: str
    constraint: Optional[NexType] = None
    def __repr__(self): return self.name
    def __eq__(self, other): return isinstance(other, TypeVar) and self.name == other.name
    def __hash__(self): return hash(("typevar", self.name))


# ─── Tip Yardımcıları ─────────────────────────────────────────────────────────

def is_numeric(t: NexType) -> bool:
    return isinstance(t, (IntType, FloatType, ByteType, UnknownType))

def is_comparable(t: NexType) -> bool:
    """Sıralama operatörleri (<, >, <=, >=) için."""
    return isinstance(t, (IntType, FloatType, StrType, ByteType, UnknownType))

def numeric_result(a: NexType, b: NexType) -> NexType:
    """int op float → float, int op int → int"""
    if isinstance(a, FloatType) or isinstance(b, FloatType):
        return FLOAT
    return INT

def is_subtype(actual: NexType, expected: NexType) -> bool:
    """
    Tip uyumluluk kontrolü.
    actual, expected'e atanabilir mi?
    """
    if actual == expected:
        return True
    if isinstance(actual, UnknownType) or isinstance(expected, UnknownType):
        return True   # hata recovery için

    # int → float widening
    if isinstance(actual, IntType) and isinstance(expected, FloatType):
        return True

    # null → T?  (bu NULL check, generic nullable'dan önce gelmeli)
    if isinstance(actual, NullType) and isinstance(expected, NullableType):
        return True

    # null → non-nullable: yasak
    if isinstance(actual, NullType):
        return False

    # T → T?  (nullable'a non-null atanabilir)
    if isinstance(expected, NullableType):
        return is_subtype(actual, expected.inner)

    # Kalıtım zinciri
    if isinstance(actual, ClassType) and isinstance(expected, ClassType):
        cur = actual
        while cur:
            if cur == expected:
                return True
            cur = cur.base
        return False

    # Interface implementasyonu
    if isinstance(actual, ClassType) and isinstance(expected, InterfaceType):
        for iface in actual.interfaces:
            if iface == expected:
                return True
        # Base sınıfların interface'lerini de kontrol et
        if actual.base:
            return is_subtype(actual.base, expected)
        return False

    # List kovaryans (sadece okuma varsa, basit model)
    if isinstance(actual, ListType) and isinstance(expected, ListType):
        return is_subtype(actual.element, expected.element)

    # @stack dizi GC listesine okuma amaçlı geçebilir (elemanlar paylaşılır, kopya yok)
    if isinstance(actual, StackArrayType) and isinstance(expected, ListType):
        return is_subtype(actual.element, expected.element)

    return False
