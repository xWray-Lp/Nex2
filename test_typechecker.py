"""
Nex Language — Type Checker Test Suite
Her test ya temiz geçmeli, ya da beklenen hataları üretmeli.
"""

import sys
from lexer import Lexer
from parser import Parser
from type_checker import TypeChecker

GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
RESET  = "\033[0m"

passed = failed = 0


def run(name: str, source: str,
        expect_errors: list[str] = None,
        expect_clean:  bool      = False):
    """
    expect_errors: bu string'leri içeren hataların üretilmesini bekle
    expect_clean:  hiç hata olmamalı
    """
    global passed, failed
    print(f"\n{CYAN}{BOLD}━━━ {name} ━━━{RESET}")

    tokens = Lexer(source.strip() + "\n").tokenize()
    tree   = Parser(tokens).parse()
    tc     = TypeChecker()
    ok     = tc.check(tree)

    report = tc.report()
    errs   = [d.message for d in tc.errors]
    warns  = [d.message for d in tc.warnings]

    if expect_clean:
        if ok:
            print(f"  {GREEN}✓ Clean — no errors{RESET}")
            passed += 1
        else:
            print(f"  {RED}✗ Expected clean, but got errors:{RESET}")
            print(report)
            failed += 1
        return

    if expect_errors:
        missing = []
        for pat in expect_errors:
            if not any(pat.lower() in e.lower() for e in errs):
                missing.append(pat)
        if missing:
            print(f"  {RED}✗ Expected errors not found:{RESET}")
            for m in missing:
                print(f"    – '{m}'")
            if errs:
                print(f"  {DIM}Actual errors:{RESET}")
                for e in errs: print(f"    {e}")
            failed += 1
        else:
            print(f"  {GREEN}✓ Caught expected errors{RESET}")
            for e in errs:
                print(f"    {DIM}↳ {e}{RESET}")
            passed += 1
        return

    # Bilgi amaçlı çalıştır
    print(report)
    if ok:
        print(f"  {GREEN}✓ OK{RESET}")
        passed += 1
    else:
        print(f"  {YELLOW}⚠ Has errors (informational){RESET}")
        passed += 1  # bilgilendirici test


# ═══════════════════════════════════════════════════════════════════
#  BÖLÜM 1 — TEMİZ PROGRAMLAR (Hata üretmemeli)
# ═══════════════════════════════════════════════════════════════════

def section(title):
    print(f"\n{BOLD}{YELLOW}{'═'*50}\n  {title}\n{'═'*50}{RESET}")


section("1 · Değişken Tanımlamaları")

run("Literal inference", """
let x = 42
let pi = 3.14
let msg = "hello"
let flag = true
""", expect_clean=True)

run("Explicit types", """
let a: int = 10
let b: float = 2.5
let c: str = "nex"
let d: bool = false
""", expect_clean=True)

run("Nullable + null coalescing", """
let maybe: int? = null
let val = maybe ?? 0
""", expect_clean=True)

run("Const declaration", """
const MAX: int = 1000
const PI = 3.14159
""", expect_clean=True)


section("2 · Aritmetik & İfadeler")

run("Arithmetic ops", """
let a = 2 + 3
let b = 10 - 4
let c = 3 * 7
let d = 10 / 4
let e = 17 % 5
let f = 2 ** 8
""", expect_clean=True)

run("String concat", """
let s = "hello" + " " + "world"
""", expect_clean=True)

run("Comparison ops", """
let a = 5 > 3
let b = 10 == 10
let c = "a" != "b"
""", expect_clean=True)

run("Logical ops", """
let x = true and false
let y = true or false
let z = not true
""", expect_clean=True)

run("Range expression", """
for i in 0..10:
    let x = i
""", expect_clean=True)


section("3 · Fonksiyonlar")

run("Simple function", """
fn add(a: int, b: int) -> int:
    return a + b
""", expect_clean=True)

run("Void function", """
fn greet(name: str):
    let msg = "Hello"
""", expect_clean=True)

run("Recursive function", """
fn fib(n: int) -> int:
    if n == 0:
        return 0
    elif n == 1:
        return 1
    else:
        return fib(n - 1) + fib(n - 2)
""", expect_clean=True)

run("Function call", """
fn square(x: int) -> int:
    return x * x

fn main():
    let result = square(5)
""", expect_clean=True)

run("Generic function", """
fn first[T](list: [int]) -> int:
    return list[0]
""", expect_clean=True)

run("Async function", """
async fn fetchData(url: str) -> str:
    return "data"
""", expect_clean=True)


section("4 · Kontrol Akışı")

run("If-elif-else", """
fn classify(x: int) -> str:
    if x > 0:
        return "positive"
    elif x == 0:
        return "zero"
    else:
        return "negative"
""", expect_clean=True)

run("For loop over list", """
fn sumList(nums: [int]) -> int:
    let total = 0
    for n in nums:
        total = total + n
    return total
""", expect_clean=True)

run("While loop", """
fn countdown(n: int):
    while n > 0:
        n = n - 1
""", expect_clean=True)

run("Match statement", """
fn describe(code: int) -> str:
    match code:
        200 -> return "OK"
        404 -> return "Not Found"
        _ -> return "Unknown"
""", expect_clean=True)

run("Break and continue", """
fn findFirst(nums: [int]) -> int:
    for n in nums:
        if n > 10:
            break
    return 0
""", expect_clean=True)


section("5 · Koleksiyonlar")

run("List literal", """
let nums: [int] = [1, 2, 3, 4, 5]
""", expect_clean=True)

run("Dict literal", """
let scores: {str: int} = {"ali": 95, "can": 88}
""", expect_clean=True)

run("Tuple literal", """
let pair: (int, str) = (42, "hello")
""", expect_clean=True)

run("List indexing", """
let nums = [10, 20, 30]
let first = nums[0]
""", expect_clean=True)

run("Dict access", """
let d = {"x": 1, "y": 2}
let v = d["x"]
""", expect_clean=True)


section("6 · Sınıflar")

run("Simple class", """
class Point:
    x: float
    y: float

    fn init(x: float, y: float):
        self.x = x
        self.y = y

    fn magnitude() -> float:
        return self.x + self.y
""", expect_clean=True)

run("Inheritance", """
class Animal:
    name: str

    fn init(name: str):
        self.name = name

    fn speak() -> str:
        return "..."

class Dog extends Animal:
    fn speak() -> str:
        return "Woof!"
""", expect_clean=True)

run("Interface implementation", """
interface Printable:
    fn toString() -> str

class Box implements Printable:
    value: int

    fn init(v: int):
        self.value = v

    fn toString() -> str:
        return "Box"
""", expect_clean=True)


section("7 · Try-Catch")

run("Try catch finally", """
fn safe():
    try:
        let x = 1
    catch IOError as e:
        let msg = e
    finally:
        let done = true
""", expect_clean=True)


# ═══════════════════════════════════════════════════════════════════
#  BÖLÜM 2 — HATA SENARYOLARI (Beklenen hataları yakala)
# ═══════════════════════════════════════════════════════════════════

section("8 · Tip Uyumsuzluğu Hataları")

run("Int ← str mismatch", """
let x: int = "hello"
""", expect_errors=["type mismatch"])

run("Bool ← int mismatch", """
let flag: bool = 42
""", expect_errors=["type mismatch"])

run("Return type mismatch", """
fn getName() -> str:
    return 42
""", expect_errors=["type mismatch"])

run("Wrong argument type", """
fn double(x: int) -> int:
    return x * 2

let r = double("oops")
""", expect_errors=["argument"])

run("Wrong arg count", """
fn add(a: int, b: int) -> int:
    return a + b

let r = add(1)
""", expect_errors=["expects"])

run("Arithmetic on string", """
let x = "hello" - 1
""", expect_errors=["not supported"])

run("Non-bool in if condition", """
fn test():
    if 42:
        let x = 1
""", expect_errors=["bool"])

run("Non-bool in while condition", """
fn test():
    while 1:
        let x = 2
""", expect_errors=["bool"])

run("Const reassignment", """
const MAX = 100
MAX = 200
""", expect_errors=["constant"])

run("Undefined variable", """
fn test():
    let x = undefined_var + 1
""", expect_errors=["undefined"])

run("Break outside loop", """
fn test():
    break
""", expect_errors=["outside loop"])

run("Continue outside loop", """
fn test():
    continue
""", expect_errors=["outside loop"])

run("List type mismatch", """
let nums: [int] = [1, 2, "three"]
""", expect_errors=["type"])

run("Non-int list index", """
let nums = [1, 2, 3]
let x = nums["bad"]
""", expect_errors=["index"])

run("Await outside async", """
fn notAsync():
    let x = await something()
""", expect_errors=["async"])

run("Interface not implemented", """
interface Flyable:
    fn fly() -> str

class Rock implements Flyable:
    fn init():
        let x = 1
""", expect_errors=["implement"])

run("Non-comparable type in <", """
fn test():
    let x = true < false
""", expect_errors=["comparable"])


# ═══════════════════════════════════════════════════════════════════
#  BÖLÜM 3 — TAM PROGRAMLAR
# ═══════════════════════════════════════════════════════════════════

section("9 · Tam Programlar")

run("Fibonacci", """
fn fib(n: int) -> int:
    if n == 0:
        return 0
    elif n == 1:
        return 1
    else:
        return fib(n - 1) + fib(n - 2)

fn main():
    let result = fib(10)
    print(result)
""", expect_clean=True)

run("Stack class", """
class Stack:
    items: [int]

    fn init():
        self.items = [1]

    fn push(item: int):
        self.items = [item]

    fn isEmpty() -> bool:
        return true
""", expect_clean=True)

run("Pipeline: filter-map", """
fn main():
    let nums = [1, 2, 3, 4, 5]
    let evens = nums.filter(isEven)
    let doubled = evens.map(double)

fn isEven(n: int) -> bool:
    return n % 2 == 0

fn double(n: int) -> int:
    return n * 2
""", expect_clean=True)

run("Pipeline: filter with undefined fn is compile error", """
fn main():
    let nums = [1, 2, 3]
    let evens = nums.filter(missingFn)
""", expect_errors=["Undefined variable 'missingFn'"])

run("Multi-class hierarchy", """
class Shape:
    color: str

    fn init(c: str):
        self.color = c

    fn area() -> float:
        return 0.0

class Circle extends Shape:
    radius: float

    fn init(c: str, r: float):
        self.color = c
        self.radius = r

    fn area() -> float:
        return 3.14 * self.radius * self.radius

class Rect extends Shape:
    width: float
    height: float

    fn area() -> float:
        return self.width * self.height
""", expect_clean=True)


run("F-string: undefined interpolation is compile error", """
fn main():
    print(f"hello {no_such_var}")
""", expect_errors=["Undefined variable 'no_such_var'"])

run("F-string: @own value interpolation is compile error", """
fn main():
    @own let buf = to_own([1, 2])
    let s = f"val: {buf}"
""", expect_errors=["cannot use @own value as f-string interpolation"])

run("Match guard: undefined variable in guard", """
fn main():
    match 1:
        case _ if len(no_such_var) > 0 -> print("nope")
""", expect_errors=["Undefined variable 'no_such_var'"])

run("Match guard: @own read in guard is compile error", """
fn main():
    @own let x = to_own([1, 2])
    match 1:
        case _ if len(x) > 0 -> print("nope")
""", expect_errors=["cannot copy @own value 'x'"])

run("Match guard: non-bool guard is compile error", """
fn main():
    match 5:
        case _ if 42 -> print("nope")
""", expect_errors=["Match guard must be 'bool', got 'int'"])

run("Match guard: guard expression checked (match expr)", """
fn main():
    let v = match 1:
        case _ if len(x) > 0 -> 5
    print(str(v))

let x = [1, 2]
""", expect_errors=["Undefined variable 'x'"])

# ─── @raw statik kısıtı (§3) ─────────────────────────────────────
run("@raw: primitives allowed", """
fn main():
    @raw let p = 42
    @raw let f = 3.14
    p = 99
""", expect_clean=True)

run("@raw: stack array source allowed", """
fn main():
    @stack let buf: [int; 2] = [1, 2]
    @raw let p = buf
""", expect_clean=True)

run("@raw: list literal is compile error", """
fn main():
    @raw let xs = [1, 2, 3]
""", expect_errors=["cannot assign GC/own-derived value of type '[int]'"])

run("@raw: string is compile error (GC object)", """
fn main():
    @raw let s = "hello"
""", expect_errors=["cannot assign GC/own-derived value of type 'str'"])

run("@raw: class instance is compile error", """
class P:
    x: int

fn main():
    let i = P()
    @raw let p = i
""", expect_errors=["cannot assign GC/own-derived value of type 'P'"])

run("@raw: to_gc result is compile error", """
fn main():
    @own let q = to_own([1, 2])
    @raw let p = to_gc(q)
""", expect_errors=["cannot assign GC/own-derived value of type"])

run("@raw: list assignment to raw var is compile error", """
fn main():
    @raw let xs = 1
    xs = [3, 4]
""", expect_errors=["cannot assign GC/own-derived value of type '[int]'"])

# ─── Sonuç ────────────────────────────────────────────────────────

total = passed + failed
print(f"\n{BOLD}{'═'*50}")
print(f"  Results: ", end="")
print(f"{GREEN}{passed}/{total} passed{RESET}", end="")
if failed:
    print(f"  {RED}{failed} failed{RESET}")
else:
    print(f"  {GREEN}— All tests passed! ✓{RESET}")
print(f"{'═'*50}{RESET}")

if failed:
    sys.exit(1)
