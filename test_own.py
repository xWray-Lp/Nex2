"""
Nex Language — @own Test Suite (checker + runtime v1)
Sahiplik kuralları: NEX_MEMORY_MODEL.md §1 (mod tablosu), §2 (GC ⇄ own
geçişleri), §4 (borrow), §5 (uygulama katmanları), §6 (yol haritası 1/3/4).

Kapsam: type_checker denetimleri + @own runtime (OwnValue, to_own/to_gc/
free, & / &mut borrow'ları, blok-lokal ömür, çakışma paniçleri).
- @own bildirimi sembol tipini OwnType(inner) yapar.
- GC → own yalnızca to_own(x) (deep copy) ile; düz atama yasak.
- own → GC yalnızca to_gc(x) (move) ile; sonraki kullanım use-after-move.
- @own değer, borrow/transfer noktaları dışında okunamaz.
- free yalnızca @own değişken üzerinde çağrılabilir.
- @own + @stack dizi tipi birleşemez (§1: modlar ayrık kategoriler).
"""

import sys
import io
import contextlib
from lexer import Lexer
from parser import Parser
from type_checker import TypeChecker
from interpreter import Interpreter

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


# ─── Runtime helpers (test_stack.py tarzı) ─────────────────────────

def _check_source(source: str):
    tokens = Lexer(source.strip() + "\n").tokenize()
    tree = Parser(tokens).parse()
    tc = TypeChecker()
    ok = tc.check(tree)
    return ok, [d.message for d in tc.errors], tree


def _run_source(source: str):
    """Çalıştırır; (output_lines, runtime_error_line|None) döndürür."""
    _ok, _errs, tree = _check_source(source)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        Interpreter().interpret(tree)
    out = buf.getvalue()
    rt = [ln for ln in out.splitlines() if "Runtime Error" in ln]
    lines = [ln for ln in out.splitlines() if "Runtime Error" not in ln]
    return lines, (rt[0] if rt else None)


def expect_runtime_output(name, source, expected_lines):
    global passed, failed
    print(f"\n{CYAN}{BOLD}━━━ {name} ━━━{RESET}")
    lines, err = _run_source(source)
    if err is None and lines == expected_lines:
        print(f"  {GREEN}✓ Output {lines}{RESET}")
        passed += 1
    else:
        print(f"  {RED}✗ Expected {expected_lines}, got lines={lines} err={err}{RESET}")
        failed += 1


def expect_runtime_error(name, source, needle):
    global passed, failed
    print(f"\n{CYAN}{BOLD}━━━ {name} ━━━{RESET}")
    lines, err = _run_source(source)
    if err is not None and needle.lower() in err.lower():
        print(f"  {GREEN}✓ Caught expected runtime error: {err}{RESET}")
        passed += 1
    else:
        print(f"  {RED}✗ Expected runtime error containing '{needle}', got err={err} lines={lines}{RESET}")
        failed += 1


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


# ═══════════════════════════════════════════════════════════════════
#  BÖLÜM 8 — RUNTIME (Step 2)
# ═══════════════════════════════════════════════════════════════════

section("8 · Runtime: move (to_gc) ve use-after-move")

expect_runtime_output("to_gc move returns heap value", """
fn main():
    @own let p = to_own([1, 2, 3])
    let g = to_gc(p)
    print(str(g[0]))
    print(str(g[2]))
main()
""", ["1", "3"])

expect_runtime_error("read after move panics (use-after-free)", """
fn main():
    @own let p = to_own([1, 2, 3])
    let g = to_gc(p)
    print(str(p))
main()
""", "use-after-free")

expect_runtime_output("to_gc result is independent heap data", """
fn main():
    @own let p = to_own([1, 2, 3])
    let g = to_gc(p)
    g[0] = 9
    print(str(g[0]))
    print(str(len(g)))
main()
""", ["9", "3"])

section("9 · Runtime: free ve use-after-free")

expect_runtime_error("read after free panics", """
fn main():
    @own let p = to_own([5, 6])
    free(p)
    print(str(p))
main()
""", "use-after-free")

expect_runtime_error("to_gc after free panics", """
fn main():
    @own let p = to_own([1])
    free(p)
    let g = to_gc(p)
main()
""", "use-after-free")

section("10 · Runtime: to_own deep independence ve scope kill")

expect_runtime_output("to_own deep-copies source (independent)", """
fn main():
    let src = [1, 2, 3]
    @own let p = to_own(src)
    src[0] = 99
    let g = to_gc(p)
    print(str(g[0]))
    print(str(src[0]))
main()
""", ["1", "99"])

expect_runtime_error("scope-end kill: dead closure capture panics", """
fn probe():
    @own let p = to_own([7, 8])
    return fn() -> int:
        let g = to_gc(p)
        return g[0]

fn main():
    let f = probe()
    print(str(f()))
main()
""", "use-after-free")

expect_runtime_output("reassign with to_own resets lifecycle", """
fn main():
    @own let p = to_own([1])
    let g1 = to_gc(p)
    p = to_own([2, 3])
    let g2 = to_gc(p)
    print(str(g1[0]))
    print(str(g2[1]))
main()
""", ["1", "3"])


# ═══════════════════════════════════════════════════════════════════
#  BÖLÜM 11 — BORROW (v1, &x / &mut x, NEX_MEMORY_MODEL.md §4/§5)
# ═══════════════════════════════════════════════════════════════════

section("11 · Borrow derleme hataları")

run("borrow non-own variable", """
fn main():
    let x = [1, 2]
    let r = &x
""", expect_errors=["only @own values can be borrowed"])

run("borrow as function argument (borrow expr)", """
fn take(xs: [int]):
    let n = len(xs)

fn main():
    @own let p = to_own([1, 2])
    take(&p)
""", expect_errors=["borrowed references cannot cross function boundaries"])

run("len(&own) allowed — pure builtin allowlist", """
fn main():
    @own let p = to_own([1, 2])
    let r = &p
    print(str(len(r)))
""", expect_clean=True)

run("keys(&own) / values(&own) allowed — pure builtin allowlist", """
fn main():
    @own let d = to_own({"a": 1, "b": 2})
    let r = &d
    print(str(len(keys(r))))
    print(str(len(values(r))))
""", expect_clean=True)

run("allowlist does not survive shadowing (user fn len)", """
fn len(x: [int]) -> int:
    return 99

fn main():
    @own let xs = to_own([1, 2])
    let r = &xs
    print(str(len(r)))
""", expect_errors=["borrowed references cannot cross function boundaries"])

run("return borrow expr", """
fn leak() -> [int]:
    @own let p = to_own([1, 2])
    return &p
""", expect_errors=["borrowed references cannot cross function boundaries"])

section("12 · Borrow runtime")

expect_runtime_output("mut borrow read/write through ref", """
fn main():
    @own let xs = to_own([1, 2])
    let r = &mut xs
    r[0] = 42
    print(str(r[0]))
main()
""", ["42"])

expect_runtime_output("two shared borrows fine", """
fn main():
    @own let xs = to_own([1, 2])
    let a = &xs
    let b = &xs
    print(str(a[0]))
    print(str(b[0]))
main()
""", ["1", "1"])

expect_runtime_output("pure builtin len accepts borrow", """
fn main():
    @own let xs = to_own([1, 2])
    let r = &xs
    print(str(len(r)))
main()
""", ["2"])

expect_runtime_output("pure builtins keys/values accept borrow (dict)", """
fn main():
    @own let d = to_own({"a": 1, "b": 2})
    let r = &d
    print(str(len(keys(r))))
    print(str(len(values(r))))
main()
""", ["2", "2"])

expect_runtime_error("non-pure builtin str still rejects borrow arg", """
fn main():
    @own let xs = to_own([1, 2])
    let r = &xs
    print(str(r))
main()
""", "borrowed references cannot cross function boundaries")

expect_runtime_output("len(&expr) — direct BorrowExpr argument", """
fn main():
    @own let xs = to_own([1, 2, 3])
    print(str(len(&xs)))
main()
""", ["3"])

expect_runtime_error("shadowed fn len still rejects borrow at runtime", """
fn len(x: list) -> int:
    return 99

fn main():
    @own let xs = to_own([1, 2])
    let r = &xs
    print(str(len(r)))
main()
""", "cannot cross function boundaries")

expect_runtime_error("keys() on non-dict gives a positioned error", """
fn main():
    print(str(keys([1, 2])))
main()
""", "keys() expects a dict")

expect_runtime_error("mut borrow after shared conflicts", """
fn main():
    @own let xs = to_own([1, 2])
    let a = &xs
    let b = &mut xs
    print(str(b[0]))
main()
""", "borrow conflict")

expect_runtime_error("write through shared borrow", """
fn main():
    @own let xs = to_own([1, 2])
    let r = &xs
    r[0] = 9
main()
""", "cannot write through shared borrow")

expect_runtime_output("scope auto-release allows to_gc after block", """
fn main():
    @own let xs = to_own([7, 8])
    if true:
        let r = &xs
        print(str(r[0]))
    let g = to_gc(xs)
    print(str(g[1]))
main()
""", ["7", "8"])

expect_runtime_error("free while borrowed", """
fn main():
    @own let xs = to_own([1, 2])
    let r = &xs
    free(xs)
    print(str(r[0]))
main()
""", "cannot move/free")

expect_runtime_error("to_gc while borrowed", """
fn main():
    @own let xs = to_own([1, 2])
    let r = &xs
    let g = to_gc(xs)
main()
""", "cannot move/free")


# ─── 13 · Allowlist tutarlılığı ───────────────────────────────────

section("13 · PURE_BORROW_BUILTINS checker/runtime senkronu")

from type_checker import PURE_BORROW_BUILTINS as CHECKER_PURE
from interpreter import PURE_BORROW_BUILTINS as RUNTIME_PURE

if CHECKER_PURE == RUNTIME_PURE:
    print(f"  {GREEN}✓ Both sides agree: {sorted(CHECKER_PURE)}{RESET}")
    passed += 1
else:
    print(f"  {RED}✗ Drift: checker={sorted(CHECKER_PURE)} "
          f"runtime={sorted(RUNTIME_PURE)}{RESET}")
    failed += 1


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
