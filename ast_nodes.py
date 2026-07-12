"""
Nex Language — AST (Abstract Syntax Tree)
Tüm node türlerini tanımlar.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional


# ─── Temel Sınıf ─────────────────────────────────────────────────────────────

@dataclass
class Node:
    line: int = field(default=0, repr=False, kw_only=True)
    col:  int = field(default=0, repr=False, kw_only=True)

    def accept(self, visitor):
        """Visitor pattern için."""
        method = "visit_" + type(self).__name__
        return getattr(visitor, method)(self)


# ─── Tip İfadeleri ───────────────────────────────────────────────────────────

@dataclass
class TypeName(Node):
    """Basit tip: int, str, float"""
    name: str

@dataclass
class TypeGeneric(Node):
    """Generic tip: [int], Stack[str], {str: int}"""
    base:   str
    params: list[Node]

@dataclass
class TypeNullable(Node):
    """Nullable tip: int?"""
    inner: Node

@dataclass
class TypeFn(Node):
    """Fonksiyon tipi: fn(int, str) -> bool"""
    params:  list[Node]
    returns: Optional[Node]

@dataclass
class TypeRef(Node):
    """Referans tipi: &T veya &mut T"""
    inner:   Node
    mutable: bool = False

@dataclass
class TypeTuple(Node):
    """Tuple tipi: (int, str, bool)"""
    elements: list[Node]


# ─── Literal Expressionlar ───────────────────────────────────────────────────

@dataclass
class IntLit(Node):
    value: int

@dataclass
class FloatLit(Node):
    value: float

@dataclass
class StrLit(Node):
    value: str

@dataclass
class BoolLit(Node):
    value: bool

@dataclass
class NullLit(Node):
    pass

@dataclass
class ListLit(Node):
    elements: list[Node]

@dataclass
class DictLit(Node):
    pairs: list[tuple[Node, Node]]

@dataclass
class TupleLit(Node):
    elements: list[Node]

@dataclass
class SetLit(Node):
    elements: list[Node]


# ─── Temel Expressionlar ─────────────────────────────────────────────────────

@dataclass
class Ident(Node):
    """Değişken / fonksiyon ismi referansı."""
    name: str

@dataclass
class BinaryExpr(Node):
    """a + b, x == y, ..."""
    left:  Node
    op:    str
    right: Node

@dataclass
class UnaryExpr(Node):
    """-x, not y, !z"""
    op:      str
    operand: Node

@dataclass
class CallExpr(Node):
    """foo(a, b, c)"""
    callee:    Node
    args:      list[Node]
    type_args: list[Node] = field(default_factory=list)  # foo[T](...)

@dataclass
class IndexExpr(Node):
    """arr[i]"""
    obj:   Node
    index: Node

@dataclass
class MemberExpr(Node):
    """obj.field"""
    obj:    Node
    member: str

@dataclass
class AwaitExpr(Node):
    """await expr"""
    expr: Node

@dataclass
class SpawnExpr(Node):
    """spawn fn ..."""
    expr: Node

@dataclass
class NullCoalExpr(Node):
    """a ?? b"""
    left:  Node
    right: Node

@dataclass
class IfExpr(Node):
    """if cond then a else b  (inline expression)"""
    condition: Node
    then_val:  Node
    else_val:  Node

@dataclass
class RangeExpr(Node):
    """0..10"""
    start: Node
    end:   Node

@dataclass
class LambdaExpr(Node):
    """(x: int) -> x * 2"""
    params:  list[Param]
    body:    Node           # expression veya Block

@dataclass
class AssignExpr(Node):
    """x = val, x += val, ..."""
    target: Node
    op:     str             # "=", "+=", "-=", "*=", "/="
    value:  Node

@dataclass
class MatchExpr(Node):
    """match x: ..."""
    subject: Node
    arms:    list[MatchArm]

@dataclass
class MatchArm(Node):
    pattern: Node           # literal, ident, wildcard, destructure, range
    body:    Node           # expression (inline) veya Block
    guard:   "Optional[Node]" = None  # if condition


# ─── Statements ───────────────────────────────────────────────────────────────

@dataclass
class Block(Node):
    """Indented blok: birden fazla statement."""
    stmts: list[Node]

@dataclass
class ExprStmt(Node):
    """Expression statement: foo() gibi."""
    expr: Node

@dataclass
class ReturnStmt(Node):
    value: Optional[Node]

@dataclass
class BreakStmt(Node):
    pass

@dataclass
class ContinueStmt(Node):
    pass

@dataclass
class ImportStmt(Node):
    """import math.geometry.{A, B} as alias"""
    path:    list[str]
    names:   list[str]        # seçici import; boşsa tümünü import et
    alias:   Optional[str]

@dataclass
class ModuleStmt(Node):
    path: list[str]

@dataclass
class IfStmt(Node):
    condition:   Node
    then_block:  Block
    elif_arms:   list[tuple[Node, Block]]
    else_block:  Optional[Block]

@dataclass
class ForStmt(Node):
    """for item in iterable:"""
    target:   Node           # Ident veya TupleLit (destructuring)
    iterable: Node
    body:     Block

@dataclass
class WhileStmt(Node):
    condition: Node
    body:      Block

@dataclass
class TryCatchStmt(Node):
    try_block:     Block
    catch_arms:    list[CatchArm]
    finally_block: Optional[Block]

@dataclass
class CatchArm(Node):
    exc_type: Optional[str]   # None = catch-all
    alias:    Optional[str]
    body:     Block

@dataclass
class MatchStmt(Node):
    subject: Node
    arms:    list[MatchArm]


# ─── Declarations ─────────────────────────────────────────────────────────────

@dataclass
class Param(Node):
    """Fonksiyon parametresi."""
    name:         str
    type_ann:     Optional[Node]
    default:      Optional[Node]
    is_ref:       bool = False
    is_mut_ref:   bool = False

@dataclass
class VarDecl(Node):
    """let x: int = 42  /  const MAX = 1000"""
    name:       str
    type_ann:   Optional[Node]
    value:      Optional[Node]
    mutable:    bool            # let → True, const → False
    mem_mode:   Optional[str]   # "own" | "raw" | "stack" | None

@dataclass
class MultiVarDecl(Node):
    """let a, b, c = 1, 2, 3"""
    names:  list[str]
    values: list[Node]
    mutable: bool

@dataclass
class FnDecl(Node):
    name:       str
    type_params: list[str]          # generic: [T, U]
    params:     list[Param]
    return_type: Optional[Node]
    body:       Optional[Block]     # None = interface method
    is_async:   bool = False
    is_export:  bool = False

@dataclass
class ClassDecl(Node):
    name:        str
    type_params: list[str]
    base:        Optional[str]       # extends
    interfaces:  list[str]           # implements
    fields:      list[FieldDecl]
    methods:     list[FnDecl]

@dataclass
class FieldDecl(Node):
    name:     str
    type_ann: Node
    default:  Optional[Node] = None

@dataclass
class InterfaceDecl(Node):
    name:    str
    methods: list[FnDecl]

@dataclass
class Program(Node):
    """Kök node — tüm program."""
    stmts: list[Node]

@dataclass
class FStringExpr(Node):
    """f"hello {name}!" — parts: list of str (literal) or Node (expr)"""
    parts: list  # str | Node

@dataclass
class PipeExpr(Node):
    """expr |> fn  — sol taraf fn'nin ilk argümanı olur"""
    left:  Node
    right: Node
