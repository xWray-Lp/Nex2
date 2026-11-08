"""
Nex Language — @stack Test Suite
Sabit boyutlu stack dizileri: [T; N] tipleri ve @stack bildirimleri.

Kapsam (NEX_MEMORY_MODEL.md Bölüm 6, madde 0 — v1 + v2 kaçış kuralı regresyonları):
- '@stack let buf: [int; 4] = [...]' ve '@'sız 'stack let ...' yazımı
- bildirimsiz başlatmada eleman tipine göre varsayılan değerler
- sabit indekslerde derleme-zamanı sınır denetimi, değişken indekste runtime
- boyut uyuşmazlığı, @stack üzerinde mutator metod yasağı
- kaçış kuralları: değer olarak geçen stack dizi heap liste kopyasına döner
- sıradan listelerin (push, dinamik indeks) geriye uyumluluğu
"""

import sys
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


def check_source(source: str):
    """(ok, error_messages) döndürür — sadece tip kontrolü."""
    tokens = Lexer(source.strip() + "\n").tokenize()
    tree   = Parser(tokens).parse()
    tc     = TypeChecker()
    ok     = tc.check(tree)
    return ok, [d.message for d in tc.errors], tree


def run_source(source: str):
    """Programı çalıştırır, çıktıyı (list[str]) ve runtime hata mesajını döndürür.

    Interpreter top-level NexRuntimeError'ları stdout'a basıp yutuyor;
    runtime hata metni bu yüzden çıktının içinde aranır.
    """
    import io, contextlib
    ok, _errs, tree = check_source(source)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        Interpreter().interpret(tree)
    out = buf.getvalue()
    rt = [ln for ln in out.splitlines() if "Runtime Error" in ln]
    lines = [ln for ln in out.splitlines() if "Runtime Error" not in ln]
    return lines, (rt[0] if rt else None)


def expect_type_error(name, source, needle):
    global passed, failed
    print(f"\n{CYAN}{BOLD}━━━ {name} ━━━{RESET}")
    ok, errs, _ = check_source(source)
    if not ok and any(needle.lower() in e.lower() for e in errs):
        print(f"  {GREEN}✓ Caught expected error{RESET}")
        passed += 1
    else:
        print(f"  {RED}✗ Expected type error containing '{needle}', got: {errs}{RESET}")
        failed += 1


def expect_clean(name, source):
    global passed, failed
    print(f"\n{CYAN}{BOLD}━━━ {name} ━━━{RESET}")
    ok, errs, _ = check_source(source)
    if ok:
        print(f"  {GREEN}✓ Clean — no errors{RESET}")
        passed += 1
    else:
        print(f"  {RED}✗ Expected clean, got: {errs}{RESET}")
        failed += 1


def expect_output(name, source, expected_lines):
    global passed, failed
    print(f"\n{CYAN}{BOLD}━━━ {name} ━━━{RESET}")
    lines, err = run_source(source)
    if err is None and lines == expected_lines:
        print(f"  {GREEN}✓ Output {lines}{RESET}")
        passed += 1
    else:
        print(f"  {RED}✗ Expected {expected_lines}, got lines={lines} err={err}{RESET}")
        failed += 1


def expect_runtime_error(name, source, needle):
    global passed, failed
    print(f"\n{CYAN}{BOLD}━━━ {name} ━━━{RESET}")
    lines, err = run_source(source)
    if err is not None and needle.lower() in err.lower():
        print(f"  {GREEN}✓ Caught expected runtime error: {err}{RESET}")
        passed += 1
    else:
        print(f"  {RED}✗ Expected runtime error containing '{needle}', got err={err} lines={lines}{RESET}")
        failed += 1


def expect_parse_error(name, source, needle):
    global passed, failed
    print(f"\n{CYAN}{BOLD}━━━ {name} ━━━{RESET}")
    try:
        check_source(source)
        ok = False
        errs = "no error raised"
    except Exception as e:
        ok = needle.lower() in str(e).lower()
        errs = str(e)
    if ok:
        print(f"  {GREEN}✓ Caught expected parse error: {errs}{RESET}")
        passed += 1
    else:
        print(f"  {RED}✗ Expected parse error containing '{needle}', got: {errs}{RESET}")
        failed += 1


# ═══════════════════════════════════════════════════════════════════
print(f"\n{BOLD}{'═'*50}")
print("  1 · Geçerli programlar")
print(f"{'═'*50}{RESET}")

expect_clean("Stack decl + index read/write", """
fn main():
    @stack let buf: [int; 4] = [0, 0, 0, 0]
    buf[0] = 10
    print(str(buf[0]))
""")

expect_clean("Bare 'stack' keyword", """
fn main():
    stack let buf: [int; 2] = [7, 8]
    print(str(buf[1]))
""")

expect_output("Happy path: write/len/iterate", """
fn main():
    @stack let buf: [int; 4] = [0, 0, 0, 0]
    buf[0] = 10
    buf[1] = 20
    print(str(buf[0] + buf[1]))
    print(str(len(buf)))
    print(str(buf.length))
    let total = 0
    for v in buf:
        total = total + v
    print(str(total))
main()
""", ["30", "4", "4", "30"])

expect_output("No-init defaults", """
fn main():
    @stack let buf: [int; 3]
    print(str(buf[0]))
    print(str(buf))
main()
""", ["0", "[0, 0, 0]"])

expect_output("Escape by copy: GC var is independent", """
fn main():
    @stack let buf: [int; 2] = [1, 2]
    let xs = buf
    xs[0] = 99
    print(str(buf[0]))
    print(str(xs[0]))
    print(str(len(xs)))
main()
""", ["1", "99", "2"])

expect_output("Escape by copy: function arg and return", """
fn total(xs: [int]) -> int:
    let s = 0
    for v in xs:
        s = s + v
    return s

fn get() -> [int]:
    @stack let buf: [int; 2] = [1, 2]
    return buf

fn main():
    @stack let buf: [int; 3] = [1, 2, 3]
    print(str(total(buf)))
    let xs = get()
    xs.push(3)
    print(str(len(xs)))
main()
""", ["6", "3"])

expect_output("Reassign same size", """
fn main():
    @stack let buf: [int; 2] = [1, 2]
    buf = [9, 8]
    print(str(buf[0]))
main()
""", ["9"])

expect_output("Ordinary lists keep working", """
fn main():
    let xs = [1, 2, 3]
    xs.push(4)
    print(str(len(xs)))
    print(str(xs[3]))
    let i = 3
    print(str(xs[i]))
main()
""", ["4", "4", "4"])

expect_output("Ordinary annotated list", """
fn main():
    let xs: [int] = [1, 2, 3]
    xs.push(4)
    print(str(len(xs)))
main()
""", ["4"])

# ═══════════════════════════════════════════════════════════════════
print(f"\n{BOLD}{'═'*50}")
print("  2 · Derleme-zamanı hataları")
print(f"{'═'*50}{RESET}")

expect_type_error("Decl size mismatch", """
fn main():
    @stack let buf: [int; 4] = [1, 2]
""", "size mismatch")

expect_type_error("Missing type annotation", """
fn main():
    @stack let buf = [1, 2, 3]
""", "requires a fixed-size")

expect_type_error("Const index out of bounds", """
fn main():
    @stack let buf: [int; 2] = [1, 2]
    print(str(buf[5]))
""", "out of bounds")

expect_type_error("Const index write out of bounds", """
fn main():
    @stack let buf: [int; 2] = [1, 2]
    buf[9] = 1
""", "out of bounds")

expect_type_error("Reassign wrong size", """
fn main():
    @stack let buf: [int; 2] = [1, 2]
    buf = [1, 2, 3]
""", "size mismatch")

expect_type_error("Mutator method on stack array", """
fn main():
    @stack let buf: [int; 2] = [1, 2]
    buf.push(3)
""", "has no method")

expect_type_error("Return stack as int", """
fn leak() -> int:
    @stack let buf: [int; 2] = [1, 2]
    return buf
""", "Type mismatch in return")

expect_type_error("Stack param by ref", """
fn f(x: &[int; 2]):
    print(str(1))
""", "cannot be passed by reference")

# ═══════════════════════════════════════════════════════════════════
print(f"\n{BOLD}{'═'*50}")
print("  3 · Çalışma-zamanı hataları")
print(f"{'═'*50}{RESET}")

expect_runtime_error("Variable index out of bounds", """
fn main():
    @stack let buf: [int; 2] = [1, 2]
    let i = 5
    print(str(buf[i]))
main()
""", "out of bounds")

expect_runtime_error("Negative index", """
fn main():
    @stack let buf: [int; 2] = [1, 2]
    print(str(buf[-1]))
main()
""", "out of bounds")

expect_runtime_error("Non-int index", """
fn main():
    @stack let buf: [int; 2] = [1, 2]
    print(str(buf["a"]))
main()
""", "must be 'int'")


# ═══════════════════════════════════════════════════════════════════
print(f"\n{BOLD}{'═'*50}")
print("  4 · Kaçış kuralı regresyonları (v2 denetim turu)")
print(f"{'═'*50}{RESET}")
# Önceden (v1'de) aliasing / referans sızması olsun, artık her kaçış noktası
# bağımsız heap kopyası üretir. Denetim turunda doğrulanan davranışlar.

expect_output("Nested stack array: escape → deep heap copy", """
fn main():
    @stack let m: [[int; 2]; 2] = [[1, 2], [3, 4]]
    let g = m
    g[0][0] = 99
    print(str(m[0][0]))
main()
""", ["1"])

expect_output("Class field assignment: escape → deep heap copy", """
class Box:
    data: [int]

fn main():
    let b = Box()
    @stack let buf: [int; 2] = [1, 2]
    b.data = buf
    b.data[0] = 55
    print(str(buf[0]))
main()
""", ["1"])

expect_output("Pipe escape: callee cannot mutate caller's stack array", """
fn smash(xs: [int]) -> int:
    xs[0] = 100
    return xs[1]

fn main():
    @stack let buf: [int; 2] = [7, 8]
    print(str(buf |> smash))
    print(str(buf[0]))
main()
""", ["8", "7"])

expect_output("List literal element: stack array smuggles as heap copy", """
fn main():
    @stack let buf: [int; 2] = [1, 2]
    let ys = [buf, buf]
    ys[0][1] = 42
    print(str(buf[1]))
main()
""", ["2"])

expect_output("Escape accepts heap-list ops on inferred type", """
fn main():
    @stack let buf: [int; 2] = [1, 2]
    let xs = buf
    xs.push(3)
    print(str(len(xs)))
main()
""", ["3"])

expect_output("StackArray methods: isEmpty/indexOf/contains/join", """
fn main():
    @stack let buf: [int; 2] = [1, 2]
    print(str(buf.isEmpty))
    print(str(buf.indexOf(2)))
    print(str(buf.contains(1)))
    print(str(buf.join("-")))
main()
""", ["false", "1", "true", "1-2"])

expect_output("Dead closure capture: StackPanic is catchable", """
fn probe() -> fn() -> int:
    @stack let buf: [int; 2] = [1, 2]
    return fn() -> int: buf[0]

fn main():
    let g = probe()
    try:
        print(str(g()))
    catch e:
        print("caught: " + e)
main()
""", ["caught: Stack variable 'buf' used after its scope ended (stack-use-after-scope)"])

expect_type_error("Element type mismatch in decl (nested elem types)", """
fn main():
    @stack let b: [int; 2] = ["x", "y"]
main()
""", "expected 'int', got 'str'")

expect_type_error("Element assignment type check", """
fn main():
    @stack let b: [int; 2] = [1, 2]
    b[0] = true
main()
""", "expected 'int', got 'bool'")

expect_type_error("Inner row size mismatch (nested literal)", """
fn main():
    @stack let m: [[int; 2]; 2] = [[1, 2], [3]]
main()
""", "Stack array size mismatch")

expect_type_error("Element assign: inner row size mismatch", """
fn main():
    @stack let m: [[int; 2]; 2] = [[1, 2], [3, 4]]
    m[0] = [1, 2, 3, 4, 5]
main()
""", "size mismatch")

expect_parse_error("Unknown memory annotation", """
@foo let x: [int] = [1, 2]
""", "unknown memory annotation")


expect_output("Dict literal value: stack array smuggles as heap copy", """
fn main():
    @stack let buf: [int; 2] = [1, 2]
    let d = { "k": buf }
    d["k"][0] = 77
    print(str(buf[0]))
main()
""", ["1"])

expect_output("Tuple literal element: stack array smuggles as heap copy", """
fn main():
    @stack let buf: [int; 2] = [1, 2]
    let t = (buf, 5)
    t[0][1] = 99
    print(str(buf[1]))
main()
""", ["2"])

expect_output("Default param: stack array passed as default is a heap copy", """
fn poke(xs):
    xs[0] = 100
    print(str(xs[0]))

fn main():
    @stack let buf: [int; 2] = [3, 4]
    poke(buf)
    print(str(buf[0]))
main()
""", ["100", "3"])

expect_output("indexOf miss returns null", """
fn main():
    @stack let buf: [int; 2] = [1, 2]
    print(str(buf.indexOf(99)))
main()
""", ["null"])


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
