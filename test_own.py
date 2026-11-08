"""
Nex Language — @own Test Suite (Step 1: yalnızca derleme/type-check tarafı)
Sahiplik kuralları: NEX_MEMORY_MODEL.md §1 (mod tablosu), §2 (GC ⇄ own
geçişleri), §5 (statik katman kapsamı), §6 (yol haritası, adım 1/3/4).

Bu adımda runtime yoktur: yalnızca type_checker denetlenir.
- @own bildirimi sembol tipini OwnType(inner) yapar.
- GC → own yalnızca to_own(x) (deep copy) ile; düz atama yasak.
- own → GC yalnızca to_gc(x) (move) ile; sonraki kullanım use-after-move.
- @own değer başka hiçbir ifade bağlamında okunamaz (borrow bu adımda yok).
- free yalnızca @own değişken üzerinde çağrılabilir.
- @own + @stack dizi tipi birleşemez (§1: modlar ayrık kategoriler).
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

    print(report)
    if ok:
        print(f"  {GREEN}✓ OK{RESET}")
    else:
        print(f"  {YELLOW}⚠ Has errors (informational){RESET}")
    passed += 1


def section(title):
    print(f"\n{BOLD}{YELLOW}{'═'*50}\n  {title}\n{'═'*50}{RESET}")


# ═══════════════════════════════════════════════════════════════════
#  BÖLÜM 1 — GEÇERLİ KULLANIMLAR
# ═══════════════════════════════════════════════════════════════════

section("1 · Geçerli @own bildirimleri ve geçişler")

run("own list via to_own", """
@own let p = to_own([1, 2, 3])
""", expect_clean=True)

run("own primitive via to_own", """
@own let n = to_own(5)
""", expect_clean=True)

run("own + explicit annotation", """
@own let p: [int] = to_own([1, 2, 3])
@own let n: int = to_own(5)
""", expect_clean=True)

run("to_own from a GC variable", """
let xs = [1, 2, 3]
@own let p = to_own(xs)
""", expect_clean=True)

run("to_gc result inferred as inner type", """
@own let p = to_own([1, 2, 3])
let g = to_gc(p)
g.push(4)
""", expect_clean=True)

run("to_gc result into declared [int]", """
@own let p = to_own([1, 2, 3])
let g: [int] = to_gc(p)
""", expect_clean=True)

run("to_gc result into declared int", """
@own let n = to_own(5)
let m: int = to_gc(n)
""", expect_clean=True)

run("free on an @own variable", """
@own let p = to_own([1, 2, 3])
free(p)
""", expect_clean=True)

run("reassign @own via to_own", """
@own let p = to_own([1, 2, 3])
p = to_own([4, 5])
""", expect_clean=True)

run("element write through @own is mutation, not move", """
@own let p = to_own([1, 2, 3])
p[0] = 9
""", expect_clean=True)

run("member write through @own is mutation, not move", """
class Box:
    x: int

    fn init():
        self.x = 0

@own let b = to_own(Box())
b.x = 7
""", expect_clean=True)

run("moved flag resets when name is redefined", """
@own let p = to_own([1])
let g = to_gc(p)
@own let p = to_own([2])
let g2 = to_gc(p)
""", expect_clean=True)


# ═══════════════════════════════════════════════════════════════════
#  BÖLÜM 2 — GC → OWN İHLALLERİ (§2)
# ═══════════════════════════════════════════════════════════════════

section("2 · Düz GC değerini @own yapma denemeleri")

run("plain list into @own", """
@own let p = [1, 2, 3]
""", expect_errors=["cannot assign GC value to @own variable"])

run("plain annotated list into @own", """
@own let p: [int] = [1, 2, 3]
""", expect_errors=["cannot assign GC value to @own variable"])

run("GC array variable into @own without to_own", """
let xs: [int] = [1, 2, 3]
@own let p: [int] = xs
""", expect_errors=["cannot assign GC value to @own variable 'p' (use to_own(x))"])

run("GC class value into @own", """
class Box:
    x: int

    fn init():
        self.x = 0

let b = Box()
@own let p: Box = b
""", expect_errors=["cannot assign GC value to @own variable"])

run("GC value reassigned into existing @own", """
@own let p = to_own([1, 2])
p = [3, 4]
""", expect_errors=["cannot assign GC value to @own variable 'p'"])

run("@own requires own initializer", """
@own let p
""", expect_errors=["requires a type annotation or a to_own"])

run("to_own on an already-@own value", """
@own let p = to_own([1, 2])
@own let q = to_own(p)
""", expect_errors=["already @own"])

run("nested to_own", """
@own let p = to_own(to_own([1, 2]))
""", expect_errors=["already @own"])


# ═══════════════════════════════════════════════════════════════════
#  BÖLÜM 3 — KOPYALAMA / MOVE İHLALLERİ (§2, §5)
# ═══════════════════════════════════════════════════════════════════

section("3 · @own değerin kopyalanması ve yasak okumalar")

run("copy: let y = x where x is @own", """
@own let x = to_own([1, 2])
let y = x
""", expect_errors=["cannot copy @own value 'x' (move it with to_gc(x))"])

run("own-to-own variable assignment", """
@own let x = to_own([1, 2])
@own let y = to_own([3, 4])
y = x
""", expect_errors=["cannot copy @own value 'x'"])

run("own-to-own declaration with another own var", """
@own let x = to_own([1])
@own let y = x
""", expect_errors=["cannot copy @own value 'x'"])

run("@own var as function argument", """
fn consume(xs: [int]):
    let n = len(xs)

@own let p = to_own([1, 2])
consume(p)
""", expect_errors=["cannot copy @own value 'p'"])

run("print(@own) is an illegal use", """
@own let n = to_own(5)
print(n)
""", expect_errors=["cannot copy @own value 'n'"])

run("@own var in binary expression", """
@own let n = to_own(5)
let x = n + 1
""", expect_errors=["cannot copy @own value 'n'"])

run("@own var in list literal", """
@own let n = to_own(5)
let xs = [n]
""", expect_errors=["cannot copy @own value 'n'"])

run("@own var in return statement", """
fn leak() -> [int]:
    @own let p = to_own([1, 2])
    return p
""", expect_errors=["cannot copy @own value 'p'"])

run("@own var as for-loop iterable", """
@own let p = to_own([1, 2])
for v in p:
    let x = v
""", expect_errors=["cannot copy @own value 'p'"])

run("read @own via index expression", """
@own let p = to_own([1, 2])
let first = p[0]
""", expect_errors=["cannot copy @own value 'p'"])

run("compound assignment on @own", """
@own let n = to_own(1)
n += 1
""", expect_errors=["compound assignment is not supported on @own variable"])


# ═══════════════════════════════════════════════════════════════════
#  BÖLÜM 4 — to_gc / free İHLALLERİ (§2, adım 4)
# ═══════════════════════════════════════════════════════════════════

section("4 · to_gc ve free hataları")

run("double move", """
@own let p = to_own([1, 2])
let g1 = to_gc(p)
let g2 = to_gc(p)
""", expect_errors=["use of moved @own value 'p'"])

run("use after move", """
fn consume(xs: [int]):
    let n = len(xs)

@own let p = to_own([1, 2])
let g = to_gc(p)
consume(p)
""", expect_errors=["use of moved @own value 'p'"])

run("to_gc on a GC variable", """
let xs = [1, 2]
let g = to_gc(xs)
""", expect_errors=["to_gc requires an @own variable"])

run("to_gc on a non-variable expression", """
@own let p = to_own([1, 2])
let g = to_gc(p[0])
""", expect_errors=["to_gc requires a variable reference"])

run("to_gc wrong argument count", """
@own let p = to_own([1, 2])
let g = to_gc(p, p)
""", expect_errors=["to_gc requires exactly 1 argument"])

run("free on a GC variable", """
let xs = [1, 2]
free(xs)
""", expect_errors=["free requires an @own variable"])

run("free after move", """
@own let p = to_own([1, 2])
let g = to_gc(p)
free(p)
""", expect_errors=["use of moved @own value 'p'"])

run("use after free", """
@own let p = to_own([1, 2])
free(p)
let g = to_gc(p)
""", expect_errors=["use of moved @own value 'p'"])


# ═══════════════════════════════════════════════════════════════════
#  BÖLÜM 5 — OWN DEĞERİN GC BAĞLAMINA SIZMASI
# ═══════════════════════════════════════════════════════════════════

section("5 · Own değer yalnızca @own değişkende yaşayabilir")

run("let without @own binding a to_own result", """
let x = to_own(5)
""", expect_errors=["initializer of non-@own variable 'x'"])

run("to_own result passed as argument", """
print(to_own(5))
""", expect_errors=["cannot use @own value as argument"])

run("to_own result in list literal", """
let xs = [to_own(5)]
""", expect_errors=["cannot use @own value as list element"])

run("to_own result in dict value", """
let d = {"k": to_own(5)}
""", expect_errors=["cannot use @own value as dict value"])

run("to_own result in tuple literal", """
let t = (to_own(5), 1)
""", expect_errors=["cannot use @own value as tuple element"])

run("to_own result returned from function", """
fn leak():
    return to_own(5)
""", expect_errors=["cannot use @own value as return value"])


# ═══════════════════════════════════════════════════════════════════
#  BÖLÜM 6 — MODLARIN AYRILIĞI (§1)
# ═══════════════════════════════════════════════════════════════════

section("6 · @own + @stack birleşimi")

run("@own with stack array type", """
@own let p: [int; 2] = [1, 2]
""", expect_errors=["@own cannot be combined with @stack array type"])

run("@own with stack array type and to_own", """
@own let p: [int; 2] = to_own([1, 2])
""", expect_errors=["@own cannot be combined with @stack array type"])


# ═══════════════════════════════════════════════════════════════════

section("7 · moved durumunun sıfırlanması (Iceman-review regresyonları)")

run("moved @own reset via re-assignment (must be clean)", """
fn main():
    @own let p = to_own([1, 2, 3])
    let g = to_gc(p)
    p = to_own([4])
    free(p)
""", expect_clean=True)

run("moved @own then compound assign reports moved specifically", """
fn main():
    @own let n = to_own(5)
    let g = to_gc(n)
    n += 1
""", expect_errors=["use of moved @own value 'n'"])

run("assign GC into moved @own still specific GC message", """
fn main():
    @own let n = to_own(5)
    let g = to_gc(n)
    n = 7
""", expect_errors=["cannot assign GC value to @own variable 'n'"])


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
