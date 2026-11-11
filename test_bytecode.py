"""
Nex Bytecode / VM Test Suite (prototip alt kümesi)

Sözleşme: NEX_BYTECODE_SPEC.md (v0.1). Tasarım: NEX_VM_DESIGN.md.

Kapsam:
- encode → decode → encode roundtrip (byte eşitliği) + determinism
- disassembler determinism ve okunabilirlik
- aritmetik/karşılaştırma, if/elif/else, while + break/continue,
  for (range + indeksle yineleme) + break/continue, list.push metodu
- fonksiyon çağrısı, özyineleme, native çağrıları
- list/dict literalleri (NEW_LIST/NEW_DICT), indeks okuma/yazma,
  to_own(list/dict literal) uçtan uca
- and/or eager semantiği (tree-walker parity)
- shadowing: kullanıcı `fn len` → CALL, aksi hâlde CALL_PURE_NATIVE
- verifier negatifleri: jump operand ortası, stack underflow/overflow,
  eksik RET, argc uyuşmazlığı, slot taşması, geçersiz entry, pure olmayan
  native'e CALL_PURE_NATIVE, OWN/RAW slot modu ihlalleri
- bozuk modül reddi: magic/version/truncation/unknown opcode
- tree-walker ↔ VM çıktı parity
- CLI (`main.run_source`) --vm/--disasm akışı
"""

import io
import struct
import subprocess
import sys
import contextlib
from pathlib import Path

from lexer import Lexer
from parser import Parser
from compiler import compile_program, CompileError
from bytecode import (
    BytecodeError, VerifyError, Module, FunctionCode, NativeInfo,
    OPCODES, UNIMPLEMENTED_OPCODES, MODE_TEMP, MODE_OWN, MODE_STACK,
    MODE_BORROW,
    NAT_PURE, NAT_RETAINS_REF, TAG_FUNC_REF, TAG_NATIVE_REF, TAG_TYPE,
    TAG_NULL, TAG_INT,
    encode_module, decode_module, decode_code, disassemble, verify_module,
)
from vm import VM, VMError, run_module, StackArrayValue, OwnValueValue

GREEN  = "\033[92m"
RED    = "\033[91m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

passed = failed = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  {GREEN}✓{RESET} {name}")
    else:
        failed += 1
        print(f"  {RED}✗ {name}{RESET}")
        if detail:
            print(f"    {detail}")


def section(title: str) -> None:
    print(f"\n{CYAN}{BOLD}━━━ {title} ━━━{RESET}")


# ─── Yardımcılar ─────────────────────────────────────────────────────────────

def parse(source: str):
    return Parser(Lexer(source.strip() + "\n").tokenize()).parse()


def compile_source(source: str) -> Module:
    return compile_program(parse(source), "<test>")


def run_vm(source: str):
    """(module, stdout satırları) döndür — derle+doğrula+çalıştır."""
    module = compile_source(source)
    verify_module(module)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        run_module(module)
    return module, buf.getvalue().splitlines()


def run_tree(source: str):
    from interpreter import Interpreter
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        Interpreter().interpret(parse(source))
    return buf.getvalue().splitlines()


def expect_output(name: str, source: str, expected: list[str]) -> None:
    try:
        _m, got = run_vm(source)
    except Exception as e:  # noqa: BLE001 — test amaçlı
        check(name, False, f"{type(e).__name__}: {e}")
        return
    check(name, got == expected, f"got={got!r} expected={expected!r}")


def expect_parity(name: str, source: str) -> None:
    try:
        _m, vm_out = run_vm(source)
        tree_out = run_tree(source)
    except Exception as e:  # noqa: BLE001
        check(name, False, f"{type(e).__name__}: {e}")
        return
    check(name, vm_out == tree_out,
          f"vm={vm_out!r} tree={tree_out!r}")


def expect_compile_error(name: str, source: str, needle: str) -> None:
    try:
        compile_source(source)
    except CompileError as e:
        check(name, needle.lower() in str(e).lower(), f"msg={e}")
        return
    except Exception as e:  # noqa: BLE001
        check(name, False, f"wrong error {type(e).__name__}: {e}")
        return
    check(name, False, "expected CompileError, got success")


def expect_vm_error(name: str, source: str, needle: str) -> None:
    try:
        run_vm(source)
    except VMError as e:
        check(name, needle.lower() in str(e).lower(), f"msg={e}")
        return
    except Exception as e:  # noqa: BLE001
        check(name, False, f"wrong error {type(e).__name__}: {e}")
        return
    check(name, False, "expected VMError, got success")


def expect_verify_error(name: str, module: Module, needle: str) -> None:
    try:
        verify_module(module)
    except VerifyError as e:
        check(name, needle.lower() in str(e).lower(), f"msg={e}")
        return
    except Exception as e:  # noqa: BLE001
        check(name, False, f"wrong error {type(e).__name__}: {e}")
        return
    check(name, False, "expected VerifyError, got success")


def mk_module(code: bytes, *, consts=None, natives=None, n_locals: int = 0,
              max_stack: int = 4, slot_modes: bytes = b"",
              entry=None) -> Module:
    return Module(
        consts=list(consts or []),
        functions=[FunctionCode(
            name="main", arity=0, n_locals=n_locals,
            max_stack=max_stack, code=code, slot_modes=slot_modes,
        )],
        natives=list(natives or []),
        entry=0 if entry is None else entry,
    )


def fn_bytes(*parts) -> bytes:
    """Opcode adı + argümanlardan ham bytecode üret."""
    out = bytearray()
    for part in parts:
        name = part[0]
        args = part[1:]
        opcode, _n = OPCODES[name]
        out.append(opcode)
        for a in args:
            out += int(a).to_bytes(4, "little")
    return bytes(out)


SAMPLE = """
fn add(a: int, b: int) -> int:
    return a + b

fn fib(n: int) -> int:
    if n < 2:
        return n
    return fib(n - 1) + fib(n - 2)

fn main():
    let i = 0
    while i < 3:
        i = i + 1
    print(add(i, 2))
    print(fib(6))
"""


# ─── Testler ─────────────────────────────────────────────────────────────────

def test_encoding():
    section("Encoding / decoding")
    m = compile_source(SAMPLE)
    verify_module(m)
    blob = encode_module(m)
    m2 = decode_module(blob)
    check("header magic + version decode",
          m2.entry == m.entry and len(m2.functions) == len(m.functions))
    check("encode → decode → encode byte eşitliği",
          encode_module(m2) == blob)
    check("decode edilen modül verify'dan geçer",
          verify_module(m2) is None)
    check("fonksiyon adları korunur",
          [f.name for f in m2.functions] == [f.name for f in m.functions])
    check("line table korunur",
          [f.line_table for f in m2.functions] ==
          [f.line_table for f in m.functions])
    check("slot_modes korunur",
          [f.slot_modes for f in m2.functions] ==
          [f.slot_modes for f in m.functions])

    m3 = compile_source(SAMPLE)
    check("aynı kaynak iki kez derlenince aynı bytecode",
          encode_module(m3) == blob)

    # Bozuk modül reddi
    for label, bad in [
        ("kötü magic reddedilir", b"XXXX" + blob[4:]),
        ("desteklenmeyen version reddedilir",
         blob[:4] + struct.pack("<H", 99) + blob[6:]),
        ("kısa dosya reddedilir", blob[:20]),
        ("bozuk section offset reddedilir", blob[:40] + struct.pack(
            "<I", 0xFFFFFF) + blob[44:]),
        ("entry indeksi taşması decode'da reddedilir",
         blob[:8] + struct.pack("<I", 0xFFFFFF) + blob[12:]),
    ]:
        try:
            decode_module(bad)
        except BytecodeError as e:
            check(label, True, str(e))
        else:
            check(label, False, "accepted corrupt module")

    # Geçersiz UTF-8 string sabiti
    strmod = compile_source("""
fn main():
    print("x")
""")
    sblob = bytearray(encode_module(strmod))
    idx = sblob.rfind(b"x")
    sblob[idx] = 0xFF
    try:
        decode_module(bytes(sblob))
    except BytecodeError as e:
        check("geçersiz UTF-8 sabiti reddedilir", True, str(e))
    else:
        check("geçersiz UTF-8 sabiti reddedilir", False, "accepted bad UTF-8")

    # Line tablosu boyutu bozuk (func info line_len = 1)
    (func_off,) = struct.unpack_from("<I", blob, 24)
    badline = bytearray(blob)
    struct.pack_into("<I", badline, func_off + 8 * 4, 1)
    try:
        decode_module(bytes(badline))
    except BytecodeError as e:
        check("bozuk line tablosu reddedilir", True, str(e))
    else:
        check("bozuk line tablosu reddedilir", False, "accepted bad line table")

    # Sabit havuzu çeşitleri roundtrip
    src = """
fn main():
    print(1)
    print(1.5)
    print("s")
    print(true)
    print(false)
    print(null)
"""
    m4 = compile_source(src)
    verify_module(m4)
    check("int/float/str/bool/null sabitleri roundtrip",
          encode_module(decode_module(encode_module(m4))) == encode_module(m4))


def test_disassembler():
    section("Disassembler")
    m = compile_source(SAMPLE)
    d1 = disassemble(m)
    d2 = disassemble(decode_module(encode_module(m)))
    check("disassembly deterministik (roundtrip sonrası aynı)", d1 == d2)
    check("entry işaretlenir", "; fn main [entry]" in d1)
    check("CALL hedef adı gösterilir", "CALL" in d1 and "; fib" in d1)
    check("CALL_PURE_NATIVE native adı gösterilir",
          "CALL_PURE_NATIVE" in d1 and "; print" in d1)
    check("jump hedefleri hex", "JUMP_IF_FALSE" in d1 and "JUMP" in d1)

    shadow = """
fn len(x: int) -> int:
    return x + 100

fn main():
    print(len(1))
    print(str(2))
"""
    ms = compile_source(shadow)
    ds = disassemble(ms)
    check("shadowed len → CALL (CALL_PURE_NATIVE değil)",
          "CALL " in ds and "; len" in ds)
    check("shadowed len disasm'da CALL_PURE_NATIVE yok",
          "CALL_PURE_NATIVE  4" not in ds and "; str" in ds)

    no_shadow = """
fn main():
    print(len("abc"))
"""
    dn = disassemble(compile_source(no_shadow))
    check("shadow yoksa len → CALL_PURE_NATIVE",
          "CALL_PURE_NATIVE" in dn and "; len" in dn)


def test_execution_basics():
    section("Yürütme — temel ifadeler")
    expect_output("int aritmetiği + öncelik", """
fn main():
    print(2 + 3 * 4)
    print((2 + 3) * 4)
    print(10 - 3 - 2)
""", ["14", "20", "5"])

    expect_output("bölme/mod/negatif", """
fn main():
    print(7 / 2)
    print(7 % 2)
    print(-5 + 2)
""", ["3.5", "1", "-3"])

    expect_output("string birleştirme", """
fn main():
    print("a" + "b")
    print("n=" + str(3))
""", ["ab", "n=3"])

    expect_output("karşılaştırmalar", """
fn main():
    print(1 < 2)
    print(2 <= 2)
    print(3 > 4)
    print(1 == 1)
    print(1 != 2)
""", ["True", "True", "False", "True", "True"])

    expect_output("bool/null truthiness", """
fn main():
    if 0:
        print("bad")
    else:
        print("zero-false")
    if null:
        print("bad")
    else:
        print("null-false")
    if "x":
        print("str-true")
""", ["zero-false", "null-false", "str-true"])

    expect_output("not / unary", """
fn main():
    print(not true)
    print(not 0)
""", ["False", "True"])

    expect_output("variadic print", """
fn main():
    print(1, "a", true)
""", ["1 a True"])

    expect_output("native str/int/float/len", """
fn main():
    print(str(12))
    print(int("7") + 1)
    print(float(2) / 4)
    print(len("hello"))
""", ["12", "8", "0.5", "5"])


def test_control_flow():
    section("Yürütme — kontrol akışı")
    expect_output("if/elif/else zinciri", """
fn classify(n: int) -> str:
    if n < 0:
        return "neg"
    elif n == 0:
        return "zero"
    else:
        return "pos"

fn main():
    print(classify(-3))
    print(classify(0))
    print(classify(9))
""", ["neg", "zero", "pos"])

    expect_output("while döngüsü", """
fn main():
    let i = 0
    let total = 0
    while i < 5:
        total = total + i
        i = i + 1
    print(total)
""", ["10"])

    expect_output("break döngüden çıkar", """
fn main():
    let i = 0
    while i < 100:
        if i == 3:
            break
        i = i + 1
    print(i)
""", ["3"])

    expect_output("continue sonraki tura atlar", """
fn main():
    let i = 0
    let sum = 0
    while i < 5:
        i = i + 1
        if i % 2 == 0:
            continue
        sum = sum + i
    print(sum)
""", ["9"])

    expect_output("iç içe while", """
fn main():
    let i = 0
    let n = 0
    while i < 3:
        let j = 0
        while j < 3:
            n = n + 1
            j = j + 1
        i = i + 1
    print(n)
""", ["9"])

    expect_output("blok gölgeleme (iç scope)", """
fn main():
    let x = 1
    if true:
        let x = 2
        print(x)
    print(x)
""", ["2", "1"])

    expect_output("return erken çıkış", """
fn first_even(limit: int) -> int:
    let i = 0
    while i < limit:
        if i % 2 == 0:
            return i
        i = i + 1
    return -1

fn main():
    print(first_even(9))
""", ["0"])


def test_functions():
    section("Yürütme — fonksiyonlar")
    expect_output("parametre + dönüş değeri", """
fn add(a: int, b: int) -> int:
    return a + b

fn main():
    print(add(3, 4))
""", ["7"])

    expect_output("özyineleme (fib)", """
fn fib(n: int) -> int:
    if n < 2:
        return n
    return fib(n - 1) + fib(n - 2)

fn main():
    print(fib(10))
""", ["55"])

    expect_output("çıplak return → null", """
fn noop():
    return

fn main():
    print(noop() == null)
""", ["True"])

    expect_output("düşen akış null döner", """
fn fall():
    let x = 1

fn main():
    print(fall() == null)
""", ["True"])

    expect_output("iç içe çağrı argümanları", """
fn inc(x: int) -> int:
    return x + 1

fn main():
    print(inc(inc(inc(0))))
""", ["3"])

    expect_output("çift argüman sırası korunur", """
fn sub(a: int, b: int) -> int:
    return a - b

fn main():
    print(sub(10, 3))
""", ["7"])

    expect_compile_error("tanımsız fonksiyon", """
fn main():
    nope(1)
""", "undefined function")

    expect_compile_error("yerel değişken native'i gölgelerse çağrılamaz", """
fn main():
    let len = 5
    print(len(1))
""", "not a callable")

    expect_compile_error("yerel değişken fonksiyonu gölgelerse çağrılamaz", """
fn f() -> int:
    return 1

fn main():
    let f = 2
    print(f())
""", "not a callable")


def test_call_depth():
    section("Çağrı derinliği sınırı")
    deep = """
fn down(n: int) -> int:
    if n == 0:
        return 0
    return down(n - 1)

fn main():
    print(down(%d))
"""
    expect_vm_error("derin özyineleme çıplak RecursionError değil VMError verir",
                    deep % 5000, "call depth limit")

    expect_output("makul derinlik sınırın altında çalışır", deep % 100, ["0"])

    module = compile_source(deep % 10)
    try:
        run_module(module, max_call_depth=3)
    except VMError as e:
        check("max_call_depth yapılandırılabilir", 
              "call depth limit" in str(e) and e.line > 0,
              f"line={e.line} msg={e}")
    except Exception as e:  # noqa: BLE001
        check("max_call_depth yapılandırılabilir", False,
              f"wrong error {type(e).__name__}: {e}")
    else:
        check("max_call_depth yapılandırılabilir", False,
              "expected VMError, got success")

    try:
        VM(module, max_call_depth=0)
    except ValueError:
        check("max_call_depth=0 reddedilir", True)
    except Exception as e:  # noqa: BLE001
        check("max_call_depth=0 reddedilir", False,
              f"wrong error {type(e).__name__}: {e}")
    else:
        check("max_call_depth=0 reddedilir", False, "accepted invalid limit")


def test_tree_call_depth():
    section("Çağrı derinliği sınırı — tree-walker + CLI")
    from interpreter import Interpreter

    deep = """
fn down(n: int) -> int:
    if n == 0:
        return 0
    return down(n - 1)

fn main():
    print(down(%d))
"""
    # main kök autocalli de bir düzey sayar (VM run() → call(entry) ile aynı).
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ok = Interpreter().interpret(parse(deep % 5000))
    out = buf.getvalue()
    check("tree varsayılan 256 derin özyinelemeyi konumlu hata ile yakalar",
          ok is False and "call depth limit exceeded (256)" in out
          and "Runtime Error at line" in out, f"ok={ok} out={out!r}")

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ok = Interpreter().interpret(parse(deep % 100))
    check("tree makul derinlik varsayılan sınır altında çalışır",
          ok is True and buf.getvalue() == "0\n", f"out={buf.getvalue()!r}")

    # main kökü sayılmadığında düzey sayısı net olsun diye üst-düzey çağrı:
    # down(2) → 3 iç içe çağrı (sınır tam 3 → geçer), down(4) → 5 (aşar).
    toplevel = """
fn down(n: int) -> int:
    if n == 0:
        return 0
    return down(n - 1)

down(%d)
"""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        Interpreter(max_call_depth=3).interpret(parse(toplevel % 4))
    err_out = buf.getvalue()
    check("Interpreter(max_call_depth=3) depth-4 özyinelemeyi yakalar",
          "call depth limit exceeded (3)" in err_out,
          f"out={err_out!r}")

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ok = Interpreter(max_call_depth=3).interpret(parse(toplevel % 2))
    check("Interpreter(max_call_depth=3) depth-2 çalışır",
          ok is True, f"ok={ok} out={buf.getvalue()!r}")

    # Mesaj metni VM ile aynı (substring parity; konum tree'de FnDecl'dir).
    check("tree hata metni VM ile aynı ('call depth limit')",
          "call depth limit exceeded" in err_out)

    # CLI: geçersiz değer → exit 2 + stderr mesajı
    root = Path(__file__).resolve().parent
    proc = subprocess.run(
        [sys.executable, "main.py", "--max-call-depth", "abc", "x.nex"],
        capture_output=True, text=True, cwd=str(root))
    check("CLI --max-call-depth abc → exit 2",
          proc.returncode == 2
          and "positive integer expected" in proc.stderr,
          f"rc={proc.returncode} err={proc.stderr!r}")

    proc = subprocess.run(
        [sys.executable, "main.py", "--max-call-depth", "0", "x.nex"],
        capture_output=True, text=True, cwd=str(root))
    check("CLI --max-call-depth 0 → exit 2",
          proc.returncode == 2
          and "positive integer expected" in proc.stderr,
          f"rc={proc.returncode} err={proc.stderr!r}")

    # CLI: tree backend limit uygular → exit 1 + 'call depth limit'
    proc = subprocess.run(
        [sys.executable, "main.py", "--max-call-depth", "2",
         "examples/vm_basics.nex"],
        capture_output=True, text=True, cwd=str(root))
    streams = proc.stdout + proc.stderr
    check("CLI --max-call-depth 2 tree'de exit 1 + limit mesajı",
          proc.returncode == 1 and "call depth limit exceeded (2)" in streams,
          f"rc={proc.returncode} out={proc.stdout!r} err={proc.stderr!r}")

    # CLI: VM backend parity (mesaj aynı, konum stderr'de)
    proc = subprocess.run(
        [sys.executable, "main.py", "--max-call-depth", "2", "--vm",
         "examples/vm_basics.nex"],
        capture_output=True, text=True, cwd=str(root))
    check("CLI --max-call-depth 2 VM'de exit 1 + limit mesajı",
          proc.returncode == 1
          and "call depth limit exceeded (2)" in proc.stderr,
          f"rc={proc.returncode} err={proc.stderr!r}")


def test_and_or():
    section("and / or — eager semantik")
    expect_parity("and truthy → sağ operand", """
fn main():
    print(1 and 2)
    print(0 and 2)
    print("a" and "b")
""")
    expect_parity("or truthy → sol operand", """
fn main():
    print(1 or 2)
    print(0 or 2)
    print("" or "x")
""")
    expect_parity("and/or zinciri", """
fn main():
    print(1 and 2 and 3)
    print(0 or 0 or 5)
""")
    expect_parity("and/or karışık (öncelik)", """
fn main():
    print(0 or (1 and 2))
    print((0 or 1) and 3)
""")

    # Eager kanıtı: sağ taraf her zaman çalışır.
    got = run_tree("""
fn side() -> int:
    print("side")

fn main():
    let r = 0 and side()
    print(r)
""")
    check("tree-walker and eager (side çalışır)", got == ["side", "0"], f"{got}")

    _m, vm_out = run_vm("""
fn side() -> int:
    print("side")

fn main():
    let r = 0 and side()
    print(r)
""")
    check("VM and eager semantiği korur", vm_out == ["side", "0"], f"{vm_out}")

    # Değer seçimi: sonuç operandlardan biridir (bool'a indirgenmez).
    expect_output("and sonucu operand değeri", """
fn main():
    let x = 0 and 42
    print(x)
    let y = 1 and 42
    print(y)
""", ["0", "42"])


def test_stack_vm():
    section("`@stack` — STACK_ALLOC / STACK_MATERIALIZE")

    # ── Geçerli programlar (tree-walker parity) ─────────────────────────
    expect_output("stack decl + index read/write", """
fn main():
    @stack let buf: [int; 4] = [0, 0, 0, 0]
    buf[0] = 10
    print(str(buf[0]))
""", ["10"])

    expect_output("bare 'stack' anahtar kelimesi", """
fn main():
    stack let buf: [int; 2] = [7, 8]
    print(str(buf[1]))
""", ["8"])

    expect_output("başlatıcısız öntanımlı değerler (int/float/bool)", """
fn main():
    @stack let a: [int; 3]
    @stack let f: [float; 2]
    @stack let b: [bool; 2] = [true, false]
    print(str(a[0]))
    print(str(a))
    print(str(f[0]))
    print(str(b[0]) + "/" + str(b[1]))
""", ["0", "[0, 0, 0]", "0.0", "true/false"])

    expect_output("escape by copy: GC var bağımsız", """
fn main():
    @stack let buf: [int; 2] = [1, 2]
    let xs = buf
    xs[0] = 99
    print(str(buf[0]))
    print(str(xs[0]))
    print(str(len(xs)))
""", ["1", "99", "2"])

    expect_output("escape by copy: argüman ve return", """
fn total(xs: [int]) -> int:
    xs[0] = 100
    return xs[0] + xs[1]

fn get() -> [int]:
    @stack let buf: [int; 2] = [1, 2]
    return buf

fn main():
    @stack let buf: [int; 2] = [7, 8]
    print(total(buf))
    print(buf[0])
    let xs = get()
    print(str(xs[0]) + "/" + str(xs[1]))
""", ["108", "7", "1/2"])

    expect_output("dinamik indeks (while) + atama değeri", """
fn main():
    @stack let buf: [int; 4] = [0, 0, 0, 0]
    let i = 0
    while i < 4:
        buf[i] = i * i
        i = i + 1
    print(str(buf[3]))
    print(str(buf[0] = 5))
    print(str(buf[0]))
""", ["9", "5", "5"])

    expect_output("döngü gövdesinde @stack (slot yeniden kurulur)", """
fn main():
    let i = 0
    let total = 0
    while i < 3:
        @stack let t: [int; 2] = [i, i + 1]
        total = total + t[1]
        i = i + 1
    print(total)
""", ["6"])

    expect_output("float/bool dizi yazdırma + int→float eleman", """
fn main():
    @stack let f: [float; 2]
    f[0] = 3
    print(str(f))
    @stack let b: [bool; 2] = [true, false]
    print(str(b))
""", ["[3, 0.0]", "[True, False]"])

    # encode → decode → verify: TAG_TYPE + STACK_ALLOC roundtrip
    stack_src = """
fn main():
    @stack let buf: [int; 3] = [4, 5, 6]
    buf[1] = buf[0] + 10
    print(str(buf[1]))
"""
    m_rt = compile_source(stack_src)
    m_rt2 = decode_module(encode_module(m_rt))
    verify_module(m_rt2)
    buf_rt = io.StringIO()
    with contextlib.redirect_stdout(buf_rt):
        run_module(m_rt2)
    check("TAG_TYPE + STACK_ALLOC encode/decode roundtrip",
          buf_rt.getvalue().splitlines() == ["14"], buf_rt.getvalue())

    # ── Disassembly / metadata ───────────────────────────────────────────
    m = compile_source("""
fn main():
    @stack let buf: [int; 3] = [1, 2, 3]
    buf[1] = 4
    let xs = buf
    print(str(xs[0]))
""")
    d = disassemble(m)
    f = m.functions[0]
    check("STACK_ALLOC disasm'da tip/size ile görünür",
          "STACK_ALLOC" in d and "; int size=3" in d, d)
    check("SET_INDEX/GET_INDEX disasm'da", "SET_INDEX" in d
          and "GET_INDEX" in d)
    check("kaçışta STACK_MATERIALIZE üretilir", "STACK_MATERIALIZE" in d, d)
    check("stack slot modu MODE_STACK",
          f.slot_modes[0] == MODE_STACK,
          f"slot_modes={list(f.slot_modes)}")
    check("STACK_ALLOC slotu TİP sabiti taşır",
          m.consts[next(i.args[1] for i in decode_code(f.code)
                        if i.name == "STACK_ALLOC")][0] == TAG_TYPE)

    # ── Derleme zamanı reddi (kapsam dışı) ──────────────────────────────
    expect_compile_error("@stack tipi zorunlu", """
fn main():
    @stack let b = [1, 2]
""", "fixed-size")
    expect_compile_error("@stack 'str' eleman VM'de kapsam dışı", """
fn main():
    @stack let b: [str; 2] = ["a", "b"]
""", "not supported")
    expect_compile_error("nested stack dizi VM'de kapsam dışı", """
fn main():
    @stack let m: [[int; 2]; 2] = [[1, 2], [3, 4]]
""", "nested stack")
    expect_compile_error("whole-buffer reassign reddedilir", """
fn main():
    @stack let b: [int; 2] = [1, 2]
    b = [9, 8]
""", "reassignment")
    expect_compile_error("stack literal boyut uyuşmazlığı", """
fn main():
    @stack let b: [int; 4] = [1, 2]
""", "size mismatch")
    expect_compile_error("stack init literal olmalı", """
fn main():
    let xs = 1
    @stack let b: [int; 2] = xs
""", "array literal")
    expect_compile_error("stack dizi boyutu >= 1", """
fn main():
    @stack let b: [int; 0] = []
""", "at least 1")
    expect_compile_error("ptr eleman tipi VM'de yok", """
fn main():
    @stack let p: [ptr; 2]
""", "not supported")

    # ── Çalışma zamanı hataları (tree-walker metinleriyle) ─────────────
    expect_vm_error("stack indeksi int değil", """
fn main():
    @stack let b: [int; 2] = [1, 2]
    print(str(b["a"]))
""", "Stack array index must be 'int', got 'str'")
    expect_vm_error("stack negatif indeks", """
fn main():
    @stack let b: [int; 2] = [1, 2]
    print(str(b[-1]))
""", "Index -1 out of bounds for stack array of size 2")
    expect_vm_error("stack değişken indeks sınır dışı", """
fn main():
    @stack let b: [int; 2] = [1, 2]
    let i = 5
    print(str(b[i]))
""", "Index 5 out of bounds")
    expect_vm_error("stack yazma sınır dışı", """
fn main():
    @stack let b: [int; 2] = [1, 2]
    let i = 5
    b[i] = 1
""", "Index 5 out of bounds")

    # ── Verifier negatifleri ───────────────────────────────────────────
    def stack_mod(code, *, consts=None, n_locals=1,
                  slot_modes=bytes([MODE_STACK]), max_stack=2):
        return mk_module(code, consts=consts or [], n_locals=n_locals,
                         slot_modes=slot_modes, max_stack=max_stack)

    good_consts = [(TAG_TYPE, (1, 4)), (TAG_NULL, None)]
    m = stack_mod(fn_bytes(("STACK_ALLOC", 0, 0, 4), ("CONST", 1),
                           ("RET",)), consts=good_consts)
    check("geçerli STACK_ALLOC verify'dan geçer",
          verify_module(m) is None)

    m = stack_mod(fn_bytes(("STACK_ALLOC", 0, 0, 4), ("CONST", 1),
                           ("RET",)), consts=good_consts,
                  slot_modes=bytes([MODE_TEMP]))
    expect_verify_error("STACK_ALLOC non-STACK slotta", m, "non-STACK")

    m = stack_mod(fn_bytes(("STACK_ALLOC", 0, 0, 4), ("CONST", 1),
                           ("RET",)),
                  consts=[(TAG_NULL, None), (TAG_NULL, None)])
    expect_verify_error("STACK_ALLOC TİP olmayan sabit", m, "not a TYPE")

    m = stack_mod(fn_bytes(("STACK_ALLOC", 0, 0, 4), ("CONST", 1),
                           ("RET",)),
                  consts=[(TAG_TYPE, (0x09, 4)), (TAG_NULL, None)])
    expect_verify_error("STACK_ALLOC geçersiz kind", m, "kind")

    m = stack_mod(fn_bytes(("STACK_ALLOC", 0, 0, 0), ("CONST", 1),
                           ("RET",)), consts=good_consts)
    expect_verify_error("STACK_ALLOC n=0", m, ">= 1")

    m = stack_mod(fn_bytes(("STACK_ALLOC", 0, 0, 3), ("CONST", 1),
                           ("RET",)), consts=good_consts)
    expect_verify_error("STACK_ALLOC n != tip size", m, "type size")

    m = stack_mod(fn_bytes(("STACK_ALLOC", 2, 0, 4), ("CONST", 1),
                           ("RET",)), consts=good_consts)
    expect_verify_error("STACK_ALLOC slot taşması", m, "out of range")

    m = stack_mod(fn_bytes(("CONST", 1), ("STORE_LOCAL", 0), ("CONST", 1),
                           ("RET",)), consts=good_consts)
    expect_verify_error("STORE_LOCAL STACK slotuna yazamaz", m, "STACK slot")

    # Parametre slotu STACK olamaz: argüman bağlama ikinci kuruluş yolu olur
    # ve callee çıkışında çağıranın canlı tamponunu öldürürdü.
    m = stack_mod(fn_bytes(("CONST", 1), ("RET",)), consts=good_consts)
    m.functions[0].arity = 1
    expect_verify_error("parametre slotu STACK olamaz", m, "cannot be STACK")

    # Bozuk TYPE payload'ı TypeError değil VerifyError üretmeli.
    m = stack_mod(fn_bytes(("STACK_ALLOC", 0, 0, 4), ("CONST", 1),
                           ("RET",)),
                  consts=[(TAG_TYPE, None), (TAG_NULL, None)])
    expect_verify_error("bozuk TYPE payload'ı reddedilir", m,
                        "malformed TYPE payload")

    m = stack_mod(fn_bytes(("STACK_ALLOC", 0, 0, 4), ("CONST", 1),
                           ("RET",)),
                  consts=[(TAG_TYPE, (1,)), (TAG_NULL, None)])
    expect_verify_error("eksik TYPE payload boyutu reddedilir", m,
                        "malformed TYPE payload")

    # Aşırı eleman sayısı host belleğini tüketmeden reddedilir.
    big = 1_000_001
    m = stack_mod(fn_bytes(("STACK_ALLOC", 0, 0, big), ("CONST", 1),
                           ("RET",)),
                  consts=[(TAG_TYPE, (1, big)), (TAG_NULL, None)])
    expect_verify_error("STACK_ALLOC eleman sınırı", m, "exceeds limit")

    # ── Frame ömrü: ham kaçış ölü tampona dönüşür ──────────────────────
    f_code = fn_bytes(("STACK_ALLOC", 0, 0, 2), ("LOAD_LOCAL", 0), ("RET",))
    main_code = fn_bytes(("CALL", 0, 0), ("STORE_LOCAL", 0),
                         ("LOAD_LOCAL", 0), ("CONST", 1), ("GET_INDEX",),
                         ("POP",), ("CONST", 2), ("RET",))
    m = Module(
        consts=[(TAG_TYPE, (1, 2)), (TAG_INT, 0), (TAG_NULL, None)],
        functions=[
            FunctionCode(name="leak", arity=0, n_locals=1, max_stack=1,
                         code=f_code, slot_modes=bytes([MODE_STACK])),
            FunctionCode(name="main", arity=0, n_locals=1, max_stack=3,
                         code=main_code, slot_modes=bytes([MODE_TEMP])),
        ],
        natives=[], entry=1,
    )
    try:
        run_module(m)
    except VMError as e:
        check("frame çıkışında @stack ölür (stack-use-after-scope)",
              "stack-use-after-scope" in str(e), f"msg={e}")
    except Exception as e:  # noqa: BLE001
        check("frame çıkışında @stack ölür (stack-use-after-scope)", False,
              f"wrong error {type(e).__name__}: {e}")
    else:
        check("frame çıkışında @stack ölür (stack-use-after-scope)", False,
              "expected VMError, got success")

    # Materialize edilmiş kopya frame ölümünden etkilenmez.
    check("materialize kopyası bağımsız list",
          StackArrayValue(2, 1).to_list() == [0, 0])

    # Aynı slotun yeniden STACK_ALLOC'u eski tamponu öldürür: döngü
    # gövdesindeki bildirimin ham-bytecode karşılığı.
    realloc_code = fn_bytes(
        ("STACK_ALLOC", 0, 0, 2),
        ("LOAD_LOCAL", 0), ("STORE_LOCAL", 1),
        ("STACK_ALLOC", 0, 0, 2),
        ("LOAD_LOCAL", 1), ("CONST", 1), ("GET_INDEX",),
        ("POP",), ("CONST", 2), ("RET",))
    m = Module(
        consts=[(TAG_TYPE, (1, 2)), (TAG_INT, 0), (TAG_NULL, None)],
        functions=[FunctionCode(
            name="main", arity=0, n_locals=2, max_stack=3,
            code=realloc_code,
            slot_modes=bytes([MODE_STACK, MODE_TEMP]))],
        natives=[], entry=0,
    )
    try:
        run_module(m)
    except VMError as e:
        check("yeniden STACK_ALLOC eski tamponu öldürür",
              "stack-use-after-scope" in str(e), f"msg={e}")
    except Exception as e:  # noqa: BLE001
        check("yeniden STACK_ALLOC eski tamponu öldürür", False,
              f"wrong error {type(e).__name__}: {e}")
    else:
        check("yeniden STACK_ALLOC eski tamponu öldürür", False,
              "expected VMError, got success")


def test_own_vm():
    section("`@own` — OWN_ALLOC / OWN_MOVE / OWN_FREE")

    # ── Geçerli programlar (tree-walker parity) ─────────────────────────
    expect_output("own decl + to_gc + print", """
fn main():
    @own let p = to_own(5)
    let n = to_gc(p)
    print(str(n + 1))
""", ["6"])

    expect_parity("own decl + to_gc + print (parity)", """
fn main():
    @own let p = to_own("abc")
    let s = to_gc(p)
    print(s + "!")
""")

    expect_output("free sonrası devam (değişken okunmaz)", """
fn main():
    @own let p = to_own(41)
    free(p)
    print("done")
""", ["done"])

    expect_parity("free ifade bağlamında null bırakır", """
fn main():
    @own let p = to_own(1)
    let r = free(p)
    print(str(r))
""")

    expect_output("reassign @own via to_own", """
fn main():
    @own let p = to_own(1)
    p = to_own(7)
    let g = to_gc(p)
    print(str(g))
""", ["7"])

    expect_output("to_gc ifade olarak atılır (OWN_MOVE + POP)", """
fn main():
    @own let p = to_own(3)
    to_gc(p)
    print("moved")
""", ["moved"])

    expect_output("moved-out değere eleman yazma (GC alias)", """
fn main():
    @stack let buf: [int; 2] = [1, 2]
    @own let p = to_own(buf)
    let g = to_gc(p)
    g[0] = 9
    print(str(g[0]))
    print(str(g[1]))
""", ["9", "2"])

    expect_output("to_own derin kopya: stack snapshot izole", """
fn main():
    @stack let buf: [int; 2] = [7, 8]
    @own let p = to_own(buf)
    buf[0] = 99
    let g = to_gc(p)
    print(str(g[0]))
    print(str(buf[0]))
""", ["7", "99"])

    # Frame çıkışında canlı @own ölür; gözlemlenebilir tek sonuç temiz
    # çıkıştır (sahiplik fonksiyon sınırını geçemez).
    expect_output("frame sonu @own kill (exit 0)", """
fn main():
    @own let p = to_own(1)
    print("alive")
""", ["alive"])

    # ── @own slot metadata ─────────────────────────────────────────────
    m = compile_source("""
fn main():
    @own let p = to_own(5)
    let g = to_gc(p)
    print(str(g))
""")
    f = m.functions[0]
    d = disassemble(m)
    check("@own slotu MODE_OWN", MODE_OWN in list(f.slot_modes),
          f"slot_modes={list(f.slot_modes)}")
    check("OWN_ALLOC / OWN_MOVE disasm'da", "OWN_ALLOC" in d
          and "OWN_MOVE" in d, d)
    check("@own slotunda LOAD/STORE_LOCAL yok",
          not any(i.name in ("LOAD_LOCAL", "STORE_LOCAL")
                  and f.slot_modes[i.args[0]] == MODE_OWN
                  for i in decode_code(f.code)),
          d)

    # ── Derleme zamanı reddi ───────────────────────────────────────────
    expect_compile_error("@own init to_own olmalı", """
fn main():
    @own let x = 1
""", "to_own")
    expect_compile_error("@own değer ifade olarak okunamaz", """
fn main():
    @own let x = to_own(1)
    print(x)
""", "cannot copy @own value")
    expect_compile_error("@own değer başka değişkene atanamaz", """
fn main():
    @own let x = to_own(1)
    let y = x
""", "cannot copy @own value")
    expect_compile_error("own indeks okuma reddedilir", """
fn main():
    @own let x = to_own(1)
    print(str(x[0]))
""", "cannot copy @own value")
    expect_compile_error("own indeks yazma reddedilir", """
fn main():
    @own let x = to_own(1)
    x[0] = 5
""", "@own")
    expect_compile_error("@own reassign to_own olmalı", """
fn main():
    @own let x = to_own(1)
    x = 5
""", "to_own")
    expect_compile_error("@own reassign to_gc ile yapılamaz", """
fn main():
    @own let x = to_own(1)
    x = to_gc(x)
""", "to_own")
    expect_compile_error("to_own zaten-own değere uygulanamaz", """
fn main():
    @own let x = to_own(1)
    @own let y = to_own(x)
""", "cannot copy @own value")
    expect_compile_error("to_own yalnız @own başlatıcısında", """
fn main():
    to_own(1)
""", "only allowed")
    expect_compile_error("let y = to_own reddedilir", """
fn main():
    let y = to_own(1)
""", "only allowed")
    expect_compile_error("to_own çağrı hedefi olarak reddedilir", """
fn main():
    let f = to_own(1)(2)
""", "direct function calls")
    expect_compile_error("to_own 0 argüman", """
fn main():
    @own let x = to_own()
""", "exactly 1")
    expect_compile_error("to_own 2 argüman", """
fn main():
    @own let x = to_own(1, 2)
""", "exactly 1")
    expect_compile_error("to_gc 0 argüman", """
fn main():
    @own let x = to_own(1)
    to_gc()
""", "exactly 1")
    expect_compile_error("to_gc değişken referansı ister", """
fn main():
    to_gc(1)
""", "variable reference")
    expect_compile_error("to_gc @own olmayan değişkeni reddeder", """
fn main():
    let g = 1
    to_gc(g)
""", "not @own")
    expect_compile_error("free @own olmayan değişkeni reddeder", """
fn main():
    let g = 1
    free(g)
""", "not @own")
    expect_compile_error("free değişken referansı ister", """
fn main():
    free(1)
""", "variable reference")
    expect_compile_error("sahiplik builtin'i değer olarak kullanılamaz", """
fn main():
    let f = to_gc
""", "builtin")

    # ── Verifier negatifleri ───────────────────────────────────────────
    own_null = [(TAG_INT, 1), (TAG_NULL, None)]

    m = mk_module(fn_bytes(("LOAD_LOCAL", 0), ("POP",), ("CONST", 1),
                           ("RET",)),
                  consts=own_null, n_locals=1,
                  slot_modes=bytes([MODE_OWN]), max_stack=2)
    expect_verify_error("LOAD_LOCAL OWN slotta yasak", m, "OWN slot")

    m = mk_module(fn_bytes(("CONST", 0), ("STORE_LOCAL", 0), ("CONST", 1),
                           ("RET",)),
                  consts=own_null, n_locals=1,
                  slot_modes=bytes([MODE_OWN]), max_stack=2)
    expect_verify_error("STORE_LOCAL OWN slotta yasak", m, "OWN slot")

    m = mk_module(fn_bytes(("CONST", 0), ("OWN_ALLOC", 0), ("OWN_FREE", 0),
                           ("CONST", 1), ("RET",)),
                  consts=own_null, n_locals=1,
                  slot_modes=bytes([MODE_OWN]), max_stack=2)
    check("geçerli OWN_ALLOC/OWN_FREE verify'dan geçer",
          verify_module(m) is None)

    m = mk_module(fn_bytes(("OWN_FREE", 0), ("CONST", 1), ("RET",)),
                  consts=own_null, n_locals=1,
                  slot_modes=bytes([MODE_TEMP]), max_stack=2)
    expect_verify_error("OWN_FREE non-OWN slotta", m, "non-OWN")

    # OWN_ALLOC non-OWN zaten test_verifier_negatives'te; OWN_MOVE de öyle.

    # ── Çalışma zamanı negatifleri (elle yazılmış modül) ───────────────
    m = mk_module(
        fn_bytes(("CONST", 0), ("OWN_ALLOC", 0), ("OWN_MOVE", 0),
                 ("POP",), ("OWN_MOVE", 0), ("POP",), ("CONST", 1),
                 ("RET",)),
        consts=own_null, n_locals=1, slot_modes=bytes([MODE_OWN]),
        max_stack=2)
    try:
        run_module(m)
    except VMError as e:
        check("moved own tekrar OWN_MOVE → use-after-free",
              "use-after-free" in str(e), f"msg={e}")
    except Exception as e:  # noqa: BLE001
        check("moved own tekrar OWN_MOVE → use-after-free", False,
              f"wrong error {type(e).__name__}: {e}")
    else:
        check("moved own tekrar OWN_MOVE → use-after-free", False,
              "expected VMError, got success")

    m = mk_module(
        fn_bytes(("CONST", 0), ("OWN_ALLOC", 0), ("OWN_FREE", 0),
                 ("OWN_MOVE", 0), ("POP",), ("CONST", 1), ("RET",)),
        consts=own_null, n_locals=1, slot_modes=bytes([MODE_OWN]),
        max_stack=2)
    try:
        run_module(m)
    except VMError as e:
        check("free sonrası OWN_MOVE → use-after-free",
              "use-after-free" in str(e), f"msg={e}")
    except Exception as e:  # noqa: BLE001
        check("free sonrası OWN_MOVE → use-after-free", False,
              f"wrong error {type(e).__name__}: {e}")
    else:
        check("free sonrası OWN_MOVE → use-after-free", False,
              "expected VMError, got success")

    # ── Encode/decode roundtrip (OWN_ALLOC) ────────────────────────────
    m_rt = compile_source("""
fn main():
    @own let p = to_own(9)
    let g = to_gc(p)
    print(str(g))
""")
    blob = encode_module(m_rt)
    m_rt2 = decode_module(blob)
    verify_module(m_rt2)
    check("OWN_ALLOC encode → decode → encode byte eşitliği",
          encode_module(m_rt2) == blob)
    buf_rt = io.StringIO()
    with contextlib.redirect_stdout(buf_rt):
        run_module(m_rt2)
    check("decode edilen OWN_ALLOC modülü çalışır",
          buf_rt.getvalue().splitlines() == ["9"], buf_rt.getvalue())

    # OwnValueValue repr tree-walker metinleriyle aynı.
    ov = OwnValueValue(5, name="x")
    check("OwnValueValue repr canlı", repr(ov) == "<own 'x'>", repr(ov))
    ov.kill()
    check("OwnValueValue repr ölü", repr(ov) == "<freed own 'x'>", repr(ov))


def test_borrow_vm():
    section("Borrow — BORROW_SHARED / BORROW_MUT / BORROW_END (Phase B)")

    # ── Geçerli programlar (tree-walker parity, test_own.py §12 matrisi) ─
    expect_parity("mut borrow read/write", """
fn main():
    @own let xs = to_own([1, 2])
    let r = &mut xs
    r[0] = 42
    print(str(r[0]))
""")

    expect_parity("two shared borrows fine", """
fn main():
    @own let xs = to_own([1, 2])
    let a = &xs
    let b = &xs
    print(str(a[0]))
    print(str(b[0]))
""")

    expect_parity("len(&expr) — direct BorrowExpr argument", """
fn main():
    @own let xs = to_own([1, 2, 3])
    print(str(len(&xs)))
""")

    expect_parity("len(r) on list", """
fn main():
    @own let xs = to_own([1, 2])
    let r = &xs
    print(str(len(r)))
""")

    expect_parity("len(r) on dict", """
fn main():
    @own let d = to_own({"a": 1, "b": 2})
    let r = &d
    print(str(len(r)))
""")

    expect_parity("keys(r)/values(r) on dict (allowlist natives)", """
fn main():
    @own let d = to_own({"a": 1, "b": 2})
    let r = &d
    print(str(len(keys(r))))
    print(str(len(values(r))))
""")

    expect_parity("list index write via &mut", """
fn main():
    @own let xs = to_own([1, 2])
    let r = &mut xs
    r[0] = 42
    print(str(r[1]))
    print(str(len(r)))
""")

    expect_parity("dict index write via &mut", """
fn main():
    @own let d = to_own({"a": 1})
    let r = &mut d
    r["a"] = 5
    print(str(r["a"]))
""")

    expect_parity("scope auto-release allows to_gc after block", """
fn main():
    @own let xs = to_own([7, 8])
    if true:
        let r = &xs
        print(str(r[0]))
    let g = to_gc(xs)
    print(str(g[1]))
""")

    # ── Derleme zamanı reddi ────────────────────────────────────────────
    expect_compile_error("borrow non-own rejected", """
fn main():
    let x = [1, 2]
    let r = &x
""", "only @own values can be borrowed")

    expect_compile_error("borrow into user fn rejected", """
fn take(xs: [int]):
    let n = len(xs)

fn main():
    @own let p = to_own([1, 2])
    take(&p)
""", "borrowed references cannot cross function boundaries")

    expect_compile_error("shadowed fn len rejects borrow at compile", """
fn len(x: [int]) -> int:
    return 99

fn main():
    @own let xs = to_own([1, 2])
    print(str(len(&xs)))
""", "borrowed references cannot cross function boundaries")

    expect_compile_error("return borrow expr rejected", """
fn leak() -> [int]:
    @own let p = to_own([1, 2])
    return &p

fn main():
    leak()
""", "borrowed references cannot cross function boundaries")

    expect_compile_error("reassign existing var via borrow expr rejected", """
fn main():
    @own let xs = to_own([1, 2])
    let r = &xs
    r = &xs
""", "borrow expressions need a fresh let binding")

    # ── Runtime reddi (VMError) ─────────────────────────────────────────
    expect_vm_error("shadowed fn len rejects borrow at runtime", """
fn len(x: [int]) -> int:
    return 99

fn main():
    @own let xs = to_own([1, 2])
    let r = &xs
    print(str(len(r)))
""", "borrowed references cannot cross function boundaries")

    expect_vm_error("non-pure builtin str rejects borrow arg", """
fn main():
    @own let xs = to_own([1, 2])
    let r = &xs
    print(str(r))
""", "borrowed references cannot cross function boundaries")

    expect_vm_error("free while borrowed", """
fn main():
    @own let xs = to_own([1, 2])
    let r = &xs
    free(xs)
""", "cannot move/free")

    expect_vm_error("to_gc while borrowed", """
fn main():
    @own let xs = to_own([1, 2])
    let r = &xs
    let g = to_gc(xs)
""", "cannot move/free")

    expect_vm_error("write through shared borrow", """
fn main():
    @own let xs = to_own([1, 2])
    let r = &xs
    r[0] = 9
""", "cannot write through shared borrow")

    expect_vm_error("mut borrow after shared conflicts", """
fn main():
    @own let xs = to_own([1, 2])
    let a = &xs
    let b = &mut xs
""", "borrow conflict")

    expect_vm_error("keys() on non-dict gives a positioned error", """
fn main():
    print(str(keys([1, 2])))
""", "keys() expects a dict")

    # ── Borrow slot metadata / disassembly ──────────────────────────────
    m = compile_source("""
fn main():
    @own let xs = to_own([1, 2])
    if true:
        let r = &mut xs
        r[0] = 42
        print(str(r[0]))
""")
    f = m.functions[0]
    d = disassemble(m)
    check("borrow slotu MODE_BORROW", MODE_BORROW in list(f.slot_modes),
          f"slot_modes={list(f.slot_modes)}")
    check("BORROW_MUT / BORROW_END disasm'da", "BORROW_MUT" in d
          and "BORROW_END" in d, d)
    check("allowlist native'ler tabloda",
          [n.name for n in m.natives] ==
          ["print", "str", "int", "float", "len", "keys", "values", "push"],
          str([n.name for n in m.natives]))

    # ── Handwritten bytecode: BORROW_END released işaretler ─────────────
    released_mod = mk_module(
        fn_bytes(
            ("NEW_LIST", 0),
            ("OWN_ALLOC", 0),
            ("BORROW_SHARED", 1, 0),
            ("BORROW_END", 1),
            ("LOAD_LOCAL", 1),
            ("CONST", 0),
            ("GET_INDEX",),
            ("POP",),
            ("CONST", 0),
            ("RET",),
        ),
        consts=[(TAG_NULL, None)],
        n_locals=2,
        max_stack=3,
        slot_modes=bytes([MODE_OWN, MODE_BORROW]),
    )
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            run_module(released_mod)
        check("BORROW_END sets released (use after end)", False, "no error")
    except VMError as e:
        check("BORROW_END sets released (use after end)",
              "use of released borrow" in str(e), f"msg={e}")

    # ── Handwritten: bayat epoch borrow deref → use-after-free ─────────
    stale_mod = mk_module(
        fn_bytes(
            ("NEW_LIST", 0),
            ("OWN_ALLOC", 0),          # epoch 1
            ("BORROW_SHARED", 1, 0),   # target_epoch 1
            ("NEW_LIST", 0),
            ("OWN_ALLOC", 0),          # slot yeniden kuruldu: epoch 2
            ("LOAD_LOCAL", 1),
            ("CONST", 0),
            ("GET_INDEX",),
            ("POP",),
            ("CONST", 0),
            ("RET",),
        ),
        consts=[(TAG_NULL, None)],
        n_locals=2,
        max_stack=3,
        slot_modes=bytes([MODE_OWN, MODE_BORROW]),
    )
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            run_module(stale_mod)
        check("stale epoch borrow deref panics", False, "no error")
    except VMError as e:
        check("stale epoch borrow deref panics",
              "use-after-free" in str(e), f"msg={e}")

    # ── Handwritten: bayat borrow'a BORROW_END sayaç düşürmez ──────────
    # Owner sayaç 0 kalır → OWN_FREE (move/free) serbest geçer.
    stale_end_mod = mk_module(
        fn_bytes(
            ("NEW_LIST", 0),
            ("OWN_ALLOC", 0),          # epoch 1
            ("BORROW_SHARED", 1, 0),
            ("NEW_LIST", 0),
            ("OWN_ALLOC", 0),          # epoch 2 (borrow bayat)
            ("BORROW_END", 1),         # bayat: sayaç düşürülmez
            ("OWN_FREE", 0),           # sayaç 0 → serbest
            ("CONST", 0),
            ("RET",),
        ),
        consts=[(TAG_NULL, None)],
        n_locals=2,
        max_stack=3,
        slot_modes=bytes([MODE_OWN, MODE_BORROW]),
    )
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            run_module(stale_end_mod)
        check("BORROW_END on stale borrow leaves counters free", True)
    except Exception as e:  # noqa: BLE001
        check("BORROW_END on stale borrow leaves counters free", False,
              f"{type(e).__name__}: {e}")

    # ── Handwritten: BORROW_END non-borrow slotta VMError ──────────────
    nonborrow_mod = mk_module(
        fn_bytes(
            ("BORROW_END", 0),
            ("CONST", 0),
            ("RET",),
        ),
        consts=[(TAG_NULL, None)],
        n_locals=1,
        max_stack=2,
        slot_modes=bytes([MODE_BORROW]),
    )
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            run_module(nonborrow_mod)
        check("BORROW_END on non-borrow slot", False, "no error")
    except VMError as e:
        check("BORROW_END on non-borrow slot",
              "BORROW_END on non-borrow slot" in str(e), f"msg={e}")


def test_borrow_escape_paths():
    """F1 (continue/break/return borrow release) + F2 (RET sınırı denetimi)."""
    section("Borrow — continue/break/return scope release + RET boundary")

    # ── F1: continue döngü gövdesi borrow'unu release etmeli ────────────
    continue_src = """
fn main():
    @own let xs = to_own([1, 2, 3])
    let i = 0
    while i < 3:
        let r = &xs
        i = i + 1
        if r[0] == 1:
            continue
    let g = to_gc(xs)
    print(str(len(g)))
"""
    expect_parity("continue releases loop-body borrow (to_gc after loop)",
                  continue_src)

    # Doğrudan sayaç denetimi (tiny repro): acquire kayıtları — continue
    # her turda release etmezse sayaç birikir (1,2,3…); düzeltilmişte 1.
    import vm as _vm
    acq_rec: list[int] = []
    _orig_acq = _vm.OwnValueValue.acquire_shared

    def _rec_acq(self):
        _orig_acq(self)
        acq_rec.append(self.shared)

    _vm.OwnValueValue.acquire_shared = _rec_acq
    try:
        _m, got = run_vm(continue_src)
    finally:
        _vm.OwnValueValue.acquire_shared = _orig_acq
    check("continue path: owner shared counter never exceeds 1",
          acq_rec and max(acq_rec) == 1, f"counters={acq_rec}")

    # ── F1: break aynı şekilde release etmeli ───────────────────────────
    break_src = """
fn main():
    @own let xs = to_own([1, 2, 3])
    let i = 0
    while i < 3:
        let r = &xs
        i = i + 1
        if i == 2:
            break
    let g = to_gc(xs)
    print(str(g[0]))
"""
    expect_parity("break releases loop-body borrow (to_gc after loop)",
                  break_src)

    acq_rec2: list[int] = []
    _orig_acq2 = _vm.OwnValueValue.acquire_shared

    def _rec_acq2(self):
        _orig_acq2(self)
        acq_rec2.append(self.shared)

    _vm.OwnValueValue.acquire_shared = _rec_acq2
    try:
        _m, got = run_vm(break_src)
    finally:
        _vm.OwnValueValue.acquire_shared = _orig_acq2
    check("break path: owner shared counter never exceeds 1",
          acq_rec2 and max(acq_rec2) == 1, f"counters={acq_rec2}")

    # ── F1: return blok içinden — RET'ten önce BORROW_END ──────────────
    ret_src = """
fn probe() -> int:
    @own let xs = to_own([7, 8])
    let i = 0
    while i < 3:
        let r = &xs
        i = i + 1
        if i == 2:
            return len(r)
    return 0

fn main():
    print(str(probe()))
"""
    expect_output("return from loop body runs (borrow released before RET)",
                  ret_src, ["2"])

    m = compile_source(ret_src)
    for fn in m.functions:
        if fn.name == "probe":
            instrs = decode_code(fn.code)
            check("in-loop return emits BORROW_END before RET",
                  any(ins.name == "RET" and instrs[idx - 1].name == "BORROW_END"
                      for idx, ins in enumerate(instrs) if idx > 0),
                  "no RET preceded by BORROW_END")

    # ── F2: ham bytecode borrow'u NEW_LIST'e sızdırıp RET eder ─────────
    leak_mod = mk_module(
        fn_bytes(
            ("NEW_LIST", 0),
            ("OWN_ALLOC", 0),
            ("BORROW_SHARED", 1, 0),
            ("LOAD_LOCAL", 1),
            ("NEW_LIST", 1),           # list içine canlı borrow
            ("RET",),
        ),
        consts=[(TAG_NULL, None)],
        n_locals=2,
        max_stack=4,
        slot_modes=bytes([MODE_OWN, MODE_BORROW]),
    )
    try:
        run_module(leak_mod)
        check("raw RET of borrow inside list rejected", False, "no error")
    except VMError as e:
        check("raw RET of borrow inside list rejected",
              "function boundary violation" in str(e), f"msg={e}")

    # ── F2: aynı denetim @own değeri için ───────────────────────────────
    own_leak_mod = mk_module(
        fn_bytes(
            ("NEW_LIST", 0),
            ("OWN_ALLOC", 0),
            ("LOAD_LOCAL", 0),
            ("CONST", 0),
            ("NEW_DICT", 1),           # dict içine canlı @own (key)
            ("RET",),
        ),
        consts=[(TAG_NULL, None)],
        n_locals=1,
        max_stack=4,
        slot_modes=bytes([MODE_OWN]),
    )
    try:
        # Verifier LOAD_LOCAL OWN'ı zaten reddeder; bu test ham bytecode
        # savunma ağını (RET denetimi) hedefler, verifier'ı bypass eder.
        _orig_verify = _vm.verify_module
        _vm.verify_module = lambda _m: None
        try:
            run_module(own_leak_mod)
            check("raw RET of @own inside dict rejected", False, "no error")
        finally:
            _vm.verify_module = _orig_verify
    except VMError as e:
        check("raw RET of @own inside dict rejected",
              "function boundary violation" in str(e), f"msg={e}")


def test_list_dict_vm():
    section("list/dict literalleri — NEW_LIST / NEW_DICT")

    # ── Geçerli programlar ───────────────────────────────────────────────
    expect_output("list literal: eleman okuma/yazma, len/str", """
fn main():
    let xs = [1, 2, 3]
    print(xs[1])
    xs[0] = 9
    print(str(xs))
    print(len(xs))
""", ["2", "[9, 2, 3]", "3"])

    expect_output("nested list literal", """
fn main():
    let xs = [[1, 2], [3, 4]]
    print(str(xs[1]))
    print(xs[0][1] + xs[1][0])
""", ["[3, 4]", "5"])

    expect_output("dict literal: okuma/yazma, len/str", """
fn main():
    let d = { "k": "v", "a": 1 }
    print(d["k"])
    d["a"] = 2
    print(str(d))
    print(len(d))
""", ["v", "{'k': 'v', 'a': 2}", "2"])

    expect_output("boş dict literal → NEW_DICT 0", """
fn main():
    let e = {}
    print(str(e))
    print(len(e))
""", ["{}", "0"])

    expect_output("to_own([1, 2, 3]) → to_gc → yazdırma", """
fn main():
    @own let x = to_own([1, 2, 3])
    let y = to_gc(x)
    print(str(y))
""", ["[1, 2, 3]"])

    expect_output("to_own(dict literal) → to_gc", """
fn main():
    @own let x = to_own({ "a": 1, "b": [2, 3] })
    let y = to_gc(x)
    print(str(y["b"][1]))
""", ["3"])

    expect_output("dict'te @stack kaçışı: STACK_MATERIALIZE + NEW_DICT", """
fn main():
    @stack let buf: [int; 2] = [7, 8]
    let w = { "k": buf }
    print(str(w["k"]))
""", ["[7, 8]"])

    expect_output("list'te @stack kaçışı: STACK_MATERIALIZE + NEW_LIST", """
fn main():
    @stack let buf: [int; 2] = [7, 8]
    let w = [buf, 9]
    print(str(w[0]) + "/" + str(w[1]))
""", ["[7, 8]/9"])

    # ── Bytecode seviyesi: emisyon biçimi ───────────────────────────────
    d0 = decode_code(compile_source("""
fn main():
    let e = {}
""").functions[0].code)
    nd = [i for i in d0 if i.name == "NEW_DICT"]
    check("boş dict → tek NEW_DICT 0",
          len(nd) == 1 and nd[0].args == (0,), str([(i.name, i.args) for i in d0]))

    dl = decode_code(compile_source("""
fn main():
    let d = { "k": "v", "a": 1 }
""").functions[0].code)
    nd = [i for i in dl if i.name == "NEW_DICT"]
    check("2 çiftli dict → NEW_DICT 2",
          len(nd) == 1 and nd[0].args == (2,), str([(i.name, i.args) for i in dl]))

    ll = decode_code(compile_source("""
fn main():
    let xs = [1, 2, 3]
""").functions[0].code)
    nl = [i for i in ll if i.name == "NEW_LIST"]
    check("3 elemanlı list → NEW_LIST 3",
          len(nl) == 1 and nl[0].args == (3,), str([(i.name, i.args) for i in ll]))

    # ── Kapsam dışı (bilinçli reddedilen) kullanımlar ───────────────────
    expect_compile_error("list üzerinde compound assignment reddedilir", """
fn main():
    let xs = [1, 2]
    xs[0] += 1
""", "compound assignment")

    expect_output("list metodu (.push) artık desteklenir", """
fn main():
    let xs = [1, 2]
    xs.push(3)
    print(str(xs))
""", ["[1, 2, 3]"])

    expect_compile_error("list metodu (.length) reddedilir", """
fn main():
    let xs = [1, 2]
    print(xs.length)
""", "memberexpr is not supported")

    # ── Tree-walker ↔ VM parity ─────────────────────────────────────────
    expect_parity("parity: list literal + indeks yazma", """
fn main():
    let xs = [1, 2, 3]
    xs[0] = 9
    print(str(xs))
    print(len(xs))
    print(str([xs[0], xs[2]]))
""")

    expect_parity("parity: dict literal + @stack kaçışı", """
fn main():
    @stack let buf: [int; 2] = [7, 8]
    let d = { "k": buf, "n": 1 }
    d["n"] = 2
    print(str(d))
    print(str(d["k"]))
    print(len(d))
""")


def test_errors():
    section("Hatalar ve konum bilgisi")
    expect_vm_error("sıfıra bölme VMError", """
fn main():
    print(1 / 0)
""", "division by zero")

    expect_vm_error("sıfıra mod VMError", """
fn main():
    print(1 % 0)
""", "division by zero")

    expect_vm_error("tip hatası native çağrısında VMError", """
fn main():
    print(len(3))
""", "len()")

    try:
        run_vm("""
fn main():
    let x = 5
    print(x + null)
""")
    except VMError as e:
        check("VMError satır numarası taşır", e.line == 3, f"line={e.line}")
    else:
        check("VMError satır numarası taşır", False, "no error")

    expect_compile_error("tanımsız değişken",
                         "fn main():\n    print(y)\n", "undefined variable")
    expect_compile_error("desteklenmeyen tuple literal",
                         "fn main():\n    let x = (1, 2)\n",
                         "not supported")
    expect_compile_error("@own VM prototipinde desteklenmez",
                         "fn main():\n    @own let x = 1\n", "@own")
    # Üst seviye statement'lar artık sentetik <script> girişine derlenir
    # (3a): `print(1)` tek başına başarılı derlenir, giriş <script>.
    m_script = compile_source("print(1)\n")
    check("top-level statement derlenir (<script> girişi)",
          m_script.functions[m_script.entry].name == "<script>")
    expect_compile_error("main zorunlu",
                         "fn foo():\n    return 1\n", "main")
    expect_compile_error("iç içe fn reddedilir", """
fn main():
    fn inner():
        return 1
""", "nested function")
    expect_compile_error("bileşik atama desteklenmez", """
fn main():
    let x = 1
    x += 2
""", "compound assignment")
    expect_compile_error("referans parametresi desteklenmez", """
fn f(x: &int):
    return

fn main():
    print(1)
""", "reference parameters")


def test_verifier_negatives():
    section("Verifier — negatifler")

    # Jump operand ortasına (instruction boundary olmayan pc)
    m = compile_source("""
fn main():
    let i = 0
    while i < 3:
        i = i + 1
    print(i)
""")
    f = m.functions[0]
    code = bytearray(f.code)
    # JUMP opcode'undan sonraki 4 baytlık operandın son baytını boz.
    ins = decode_code(f.code)
    jmp = next(x for x in ins if x.name == "JUMP")
    code[jmp.pc + 4] ^= 0x01
    m.functions[0] = FunctionCode(
        name=f.name, arity=f.arity, n_locals=f.n_locals,
        max_stack=f.max_stack, code=bytes(code),
        slot_modes=f.slot_modes, line_table=f.line_table)
    expect_verify_error("jump hedefi operand ortası → ret", m,
                        "instruction boundary")

    # Stack underflow
    m = mk_module(fn_bytes(("POP",), ("RET",)), max_stack=2)
    expect_verify_error("stack underflow", m, "underflow")

    # Stack overflow (max_stack=1, iki değer)
    m = mk_module(
        fn_bytes(("CONST", 0), ("CONST", 0), ("POP",), ("POP",),
                 ("CONST", 0), ("RET",)),
        consts=[(9, None)], max_stack=1)
    expect_verify_error("stack overflow", m, "overflow")

    # Eksik RET (son komut RET değil)
    m = mk_module(fn_bytes(("CONST", 0), ("POP",), ("CONST", 0)),
                  consts=[(9, None)], max_stack=2)
    expect_verify_error("eksik RET", m, "RET")

    # Fallthrough past end
    m = mk_module(fn_bytes(("CONST", 0)), consts=[(9, None)], max_stack=2)
    expect_verify_error("kod sonundan fallthrough", m, "fallthrough")

    # argc uyuşmazlığı: CALL argc != arity
    m = compile_source("""
fn add(a: int, b: int) -> int:
    return a + b

fn main():
    print(add(1, 2))
""")
    m.functions[0].arity = 3  # add arity 3, CALL argc 2 → uyuşmaz
    expect_verify_error("CALL argc != arity", m, "argc")

    # Local slot taşması
    m = mk_module(fn_bytes(("LOAD_LOCAL", 5), ("POP",),
                           ("CONST", 0), ("RET",)),
                  consts=[(9, None)], n_locals=1, max_stack=2,
                  slot_modes=bytes([MODE_TEMP]))
    expect_verify_error("LOAD_LOCAL slot taşması", m, "out of range")

    # Geçersiz entry
    m = mk_module(fn_bytes(("CONST", 0), ("RET",)),
                  consts=[(9, None)], entry=7)
    expect_verify_error("entry indeksi taşması", m, "entry")

    # Pure olmayan native'e CALL_PURE_NATIVE
    m = mk_module(
        fn_bytes(("CONST", 0), ("CALL_PURE_NATIVE", 0, 1), ("POP",),
                 ("CONST", 0), ("RET",)),
        consts=[(9, None), (7, "impure")],
        natives=[NativeInfo("impure", 1, 0)],
        max_stack=2)
    expect_verify_error("pure olmayan native → CALL_PURE_NATIVE", m,
                        "not pure")

    # Retains-ref native'e CALL_PURE_NATIVE
    m = mk_module(
        fn_bytes(("CONST", 0), ("CALL_PURE_NATIVE", 0, 1), ("POP",),
                 ("CONST", 0), ("RET",)),
        consts=[(9, None), (7, "keeper")],
        natives=[NativeInfo("keeper", 1, NAT_PURE | NAT_RETAINS_REF)],
        max_stack=2)
    expect_verify_error("retains_ref native → CALL_PURE_NATIVE", m,
                        "not pure")

    # Native index taşması
    m = mk_module(
        fn_bytes(("CONST", 0), ("CALL_PURE_NATIVE", 3, 1), ("POP",),
                 ("CONST", 0), ("RET",)),
        consts=[(9, None)], natives=[], max_stack=2)
    expect_verify_error("native indeksi taşması", m, "out of range")

    # Fonksiyon indeksi taşması
    m = mk_module(
        fn_bytes(("CONST", 0), ("CALL", 9, 1), ("POP",),
                 ("CONST", 0), ("RET",)),
        consts=[(9, None)], max_stack=2)
    expect_verify_error("fonksiyon indeksi taşması", m, "out of range")

    # OWN_MOVE non-OWN slotta
    m = mk_module(fn_bytes(("OWN_MOVE", 0), ("RET",)),
                  n_locals=1, slot_modes=bytes([MODE_TEMP]), max_stack=2)
    expect_verify_error("OWN_MOVE non-OWN slotta", m, "non-OWN")

    # OWN_ALLOC non-OWN slotta
    m = mk_module(fn_bytes(("CONST", 0), ("OWN_ALLOC", 0), ("CONST", 0),
                           ("RET",)),
                  consts=[(9, None)], n_locals=1,
                  slot_modes=bytes([MODE_TEMP]), max_stack=2)
    expect_verify_error("OWN_ALLOC non-OWN slotta", m, "non-OWN")

    # STACK_MATERIALIZE non-STACK slotta
    m = mk_module(fn_bytes(("STACK_MATERIALIZE", 0), ("POP",),
                           ("CONST", 0), ("RET",)),
                  consts=[(9, None)], n_locals=1,
                  slot_modes=bytes([MODE_TEMP]), max_stack=2)
    expect_verify_error("STACK_MATERIALIZE non-STACK slotta", m, "non-STACK")

    # BORROW_SHARED src OWN değil
    m = mk_module(fn_bytes(("BORROW_SHARED", 1, 0), ("CONST", 0),
                           ("RET",)),
                  consts=[(9, None)], n_locals=2,
                  slot_modes=bytes([MODE_TEMP, MODE_BORROW]), max_stack=2)
    expect_verify_error("BORROW_SHARED src non-OWN", m, "not OWN")

    # BORROW_SHARED dst BORROW değil
    m = mk_module(fn_bytes(("BORROW_SHARED", 1, 0), ("CONST", 0),
                           ("RET",)),
                  consts=[(9, None)], n_locals=2,
                  slot_modes=bytes([MODE_OWN, MODE_TEMP]), max_stack=2)
    expect_verify_error("BORROW_SHARED dst non-BORROW", m, "not BORROW")

    # BORROW_END non-BORROW slotta
    m = mk_module(fn_bytes(("BORROW_END", 0), ("CONST", 0), ("RET",)),
                  consts=[(9, None)], n_locals=1,
                  slot_modes=bytes([MODE_OWN]), max_stack=2)
    expect_verify_error("BORROW_END non-BORROW slotta", m, "non-BORROW")

    # BORROW_SHARED slot taşması
    m = mk_module(fn_bytes(("BORROW_SHARED", 5, 0), ("CONST", 0),
                           ("RET",)),
                  consts=[(9, None)], n_locals=1,
                  slot_modes=bytes([MODE_OWN]), max_stack=2)
    expect_verify_error("BORROW_SHARED slot taşması", m, "out of range")

    # Geçersiz FUNC_REF sabiti
    m = mk_module(fn_bytes(("CONST", 0), ("RET",)),
                  consts=[(TAG_FUNC_REF, 5)], max_stack=2)
    expect_verify_error("FUNC_REF sabiti geçersiz", m, "FUNC_REF")

    # Geçersiz NATIVE_REF sabiti
    m = mk_module(fn_bytes(("CONST", 0), ("RET",)),
                  consts=[(TAG_NATIVE_REF, 5)], max_stack=2)
    expect_verify_error("NATIVE_REF sabiti geçersiz", m, "NATIVE_REF")

    # RAW_MAKE non-RAW slotta (underflow da raporlanabilir; mesaj aranır)
    m = mk_module(fn_bytes(("CONST", 0), ("RAW_MAKE", 0), ("CONST", 0),
                           ("RET",)),
                  consts=[(9, None)], n_locals=1,
                  slot_modes=bytes([MODE_OWN]), max_stack=2)
    expect_verify_error("RAW_MAKE non-RAW slotta", m, "non-RAW")

    # Boş fonksiyon
    m = mk_module(b"")
    expect_verify_error("boş fonksiyon reddedilir", m, "empty")

    # Unknown opcode
    m = mk_module(bytes([0xFF]))
    expect_verify_error("bilinmeyen opcode reddedilir", m, "unknown opcode")

    # Truncated instruction
    m = mk_module(bytes([OPCODES["CONST"][0], 0x01]))
    expect_verify_error("kesik instruction reddedilir", m, "truncated")

    # Slot mode tablosu boyutu
    m = mk_module(fn_bytes(("CONST", 0), ("RET",)), consts=[(9, None)],
                  n_locals=2, slot_modes=bytes([MODE_TEMP]), max_stack=2)
    expect_verify_error("slot_modes/n_locals uyuşmazlığı", m, "slot mode")

    # CONST sabit indeksi taşması
    m = mk_module(fn_bytes(("CONST", 5), ("RET",)), consts=[], max_stack=2)
    expect_verify_error("CONST sabit indeksi taşması", m, "constant index")

    # n_locals < arity
    m = mk_module(fn_bytes(("CONST", 0), ("RET",)), consts=[(9, None)],
                  max_stack=2)
    m.functions[0].arity = 1
    m.functions[0].n_locals = 0
    expect_verify_error("n_locals < arity", m, "smaller than arity")

    # Geçersiz slot modu değeri
    m = mk_module(fn_bytes(("CONST", 0), ("RET",)), consts=[(9, None)],
                  n_locals=1, slot_modes=b"\x09", max_stack=2)
    expect_verify_error("geçersiz slot modu değeri", m, "invalid slot mode")

    # Line table pc'si instruction boundary değil
    m = mk_module(fn_bytes(("CONST", 0), ("RET",)), consts=[(9, None)],
                  max_stack=2)
    m.functions[0].line_table = [(1, 1, 1)]
    expect_verify_error("line table pc'si boundary değil", m,
                        "line table pc")

    # Line table pc'leri artan değil
    m = mk_module(fn_bytes(("CONST", 0), ("RET",)), consts=[(9, None)],
                  max_stack=2)
    m.functions[0].line_table = [(5, 1, 1), (0, 2, 1)]
    expect_verify_error("line table sırası bozuk", m, "increasing")

    # Kısa slot_modes + OWN opcode: IndexError yerine VerifyError
    m = mk_module(fn_bytes(("CONST", 0), ("OWN_MOVE", 1), ("RET",)),
                  consts=[(9, None)], n_locals=2,
                  slot_modes=bytes([MODE_OWN]), max_stack=2)
    expect_verify_error("kısa slot_modes OWN opcode'da çökmez", m,
                        "slot mode")

    # Tutarsız stack derinliği (merge noktası)
    #   CONST; JUMP_IF_FALSE end; CONST; CONST; end: RET
    # end'e 0 ve 2 derinlikleriyle gelinir.
    code = fn_bytes(("CONST", 0), ("JUMP_IF_FALSE", 20),
                    ("CONST", 0), ("CONST", 0), ("RET",))
    m = mk_module(code, consts=[(9, None)], max_stack=3)
    expect_verify_error("tutarsız stack derinliği", m, "inconsistent")

    # run_module doğrulamasız modülü çalıştırmaz (spec §7: yükleme anında zorunlu)
    try:
        run_module(mk_module(b""))
    except VerifyError:
        check("run_module doğrulanmamış modülü reddeder", True)
    except Exception as e:  # noqa: BLE001
        check("run_module doğrulanmamış modülü reddeder", False,
              f"wrong error {type(e).__name__}: {e}")
    else:
        check("run_module doğrulanmamış modülü reddeder", False,
              "invalid module executed")


def test_unimplemented_opcodes():
    """Yükleme anı reddi: VM'in yürütmediği opcode verify'da yakalanmalı.

    Kök neden: verifier kabul edip runtime'da çökme sözleşme kırılmasıydı
    (BLOCK_ENTER olayı). Her unimplemented opcode için minimal, başkaca
    hiçbir ihlali olmayan elle yazılmış modül: VerifyError + opcode adı.
    Ayrıca opcode kapsama gate'i (check_opcodes.py) drift'e karşı koşulur.
    """
    section("Verifier — unimplemented opcode yükleme anı reddi")

    # Her opcode için stack dengesi tutan minimal modül (tek ihlal: opcode
    # unimplemented). CONST 0 → TAG_NULL sabiti (existing negatives aynı).
    minimal_args = {
        "LOAD_GLOBAL":   (0,),                       # (0,1) → POP ile dengelenir
        "STORE_GLOBAL":  (0,),                       # (1,0)
        "NEW_TUPLE":     (0,),                       # (0,1) → POP ile dengelenir
        "GET_MEMBER":    (0,),                       # (1,1) → POP ile dengelenir
        "SET_MEMBER":    (0,),                       # (2,0)
        "RAW_MAKE":      (0,),                       # (1,0)
        "BLOCK_ENTER":   (),                         # (0,0)
        "BLOCK_EXIT":    (),                         # (0,0)
    }

    for name in sorted(UNIMPLEMENTED_OPCODES):
        args = minimal_args[name]
        body = [fn_bytes((name, *args))]
        if name in ("LOAD_GLOBAL", "NEW_TUPLE", "GET_MEMBER"):
            body.append(fn_bytes(("POP",)))
        code = b"".join(body) + fn_bytes(("CONST", 0), ("RET",))
        m = mk_module(code, consts=[(TAG_NULL, None)], max_stack=2)
        expect_verify_error(
            f"unimplemented opcode '{name}' yükleme anında reddedilir", m,
            f"opcode '{name}' is not implemented")

    # Edge: modülün TEK yasa dışı şeyi unimplemented opcode olmalı — mesaj
    # temiz VerifyError olmalı (kısmi başarı ya da başka istisna değil).
    m = mk_module(fn_bytes(("BLOCK_ENTER",), ("CONST", 0), ("RET",)),
                  consts=[(TAG_NULL, None)], max_stack=2)
    try:
        verify_module(m)
    except VerifyError as e:
        check("tek ihlal: temiz VerifyError mesajı",
              "BLOCK_ENTER" in str(e) and "not implemented" in str(e),
              f"msg={e}")
    except Exception as e:  # noqa: BLE001
        check("tek ihlal: temiz VerifyError mesajı", False,
              f"wrong error {type(e).__name__}: {e}")
    else:
        check("tek ihlal: temiz VerifyError mesajı", False,
              "expected VerifyError, got success")

    # Opcode kapsama gate'i: VM dispatch ↔ UNIMPLEMENTED_OPCODES drift'i
    try:
        import check_opcodes
    except Exception as e:  # noqa: BLE001
        check("check_opcodes import edilir", False,
              f"{type(e).__name__}: {e}")
    else:
        try:
            rc = check_opcodes.main()
        except SystemExit as e:
            rc = e.code
        check("check_opcodes gate'i drift bildirmez", rc == 0,
              f"exit={rc} (yukarıdaki DRIFT çıktısına bakın)")

    # Listede olmayan hiçbir opcode'un, VM'in gerçekten yürüttüğü setle
    # çakışmaması (dağılım = OPCODES'in tam bölünmesi).
    try:
        import vm as _vmmod
    except Exception as e:  # noqa: BLE001
        check("vm import edilir", False, f"{type(e).__name__}: {e}")
    else:
        from check_opcodes import collect_vm_handled
        handled = collect_vm_handled(
            Path(__file__).resolve().parent / "vm.py")
        check("UNIMPLEMENTED ∩ VM-dispatch boş",
              not (UNIMPLEMENTED_OPCODES & handled),
              str(sorted(UNIMPLEMENTED_OPCODES & handled)))
        check("her OPCODES girişi handled/unimplemented ayrımına düşer",
              handled | UNIMPLEMENTED_OPCODES >= set(OPCODES),
              str(sorted(set(OPCODES) - handled - UNIMPLEMENTED_OPCODES)))


def test_vm_cli():
    section("CLI — --vm / --disasm")
    import main as nexmain

    src = """
fn main():
    print(40 + 2)
"""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ok = nexmain.run_source(src, "<cli>", backend="vm")
    check("run_source backend=vm çalışır", ok and buf.getvalue() == "42\n",
          f"ok={ok} out={buf.getvalue()!r}")

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ok = nexmain.run_source(src, "<cli>", backend="vm", disasm=True)
    out = buf.getvalue()
    check("--disasm disassembly basar", ok and "CALL_PURE_NATIVE" in out
          and "42" in out, f"out={out!r}")

    buf_err = io.StringIO()
    buf_out = io.StringIO()
    with contextlib.redirect_stdout(buf_out), \
            contextlib.redirect_stderr(buf_err):
        ok = nexmain.run_source("""
fn main():
    print(1 / 0)
""", "<cli>", backend="vm")
    check("VM hatası CLI'da tree-walker biçiminde raporlanır",
          not ok and "Runtime Error at line 3:13" in buf_err.getvalue(),
          buf_err.getvalue())

    buf_err = io.StringIO()
    with contextlib.redirect_stdout(io.StringIO()), \
            contextlib.redirect_stderr(buf_err):
        ok = nexmain.run_source("""
fn main():
    let x = (1, 2)
""", "<cli>", backend="vm")
    check("derleme hatası CLI'da Compile Error olarak raporlanır",
          not ok and "Compile Error" in buf_err.getvalue(),
          buf_err.getvalue())

    # Tree-walker backend'i değişmedi.
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ok = nexmain.run_source(src, "<cli>")
    check("varsayılan backend hâlâ tree-walker",
          ok and buf.getvalue() == "42\n", buf.getvalue())


def test_exit_codes():
    section("CLI — çıkış kodları (run_source başarı bayrağı)")
    import main as nexmain

    # (a) runtime hatası: tree backend False döner
    buf_err = io.StringIO()
    buf_out = io.StringIO()
    with contextlib.redirect_stdout(buf_out), \
            contextlib.redirect_stderr(buf_err):
        ok = nexmain.run_source("let x = 1 / 0", "<test>")
    check("tree backend runtime hatasında False",
          ok is False and "Runtime Error" in buf_out.getvalue(),
          f"ok={ok} out={buf_out.getvalue()!r}")

    # (a) runtime hatası: VM backend de False döner
    buf_err = io.StringIO()
    with contextlib.redirect_stdout(io.StringIO()), \
            contextlib.redirect_stderr(buf_err):
        ok = nexmain.run_source("let x = 1 / 0", "<test>", backend="vm")
    check("VM backend runtime hatasında False",
          ok is False and "Runtime Error" in buf_err.getvalue(),
          f"ok={ok} err={buf_err.getvalue()!r}")

    # (b) --check: tip hatasında False döner
    buf_err = io.StringIO()
    with contextlib.redirect_stdout(io.StringIO()), \
            contextlib.redirect_stderr(buf_err):
        ok = nexmain.run_source('let x: int = "s"', "<test>", check_only=True)
    check("--check tip hatasında False",
          ok is False and "Type Errors" in buf_err.getvalue(),
          f"ok={ok} err={buf_err.getvalue()!r}")

    # (b) --check: hatasız kaynak True döner
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), \
            contextlib.redirect_stderr(io.StringIO()):
        ok = nexmain.run_source("let x = 1", "<test>", check_only=True)
    check("--check hatasız kaynakta True", ok is True, f"ok={ok}")

    # (c) hatasız kaynak: her iki backend de True döner
    src = """
fn main():
    print(str(40 + 2))
"""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), \
            contextlib.redirect_stderr(io.StringIO()):
        ok = nexmain.run_source(src, "<test>")
    check("tree backend hatasız kaynakta True",
          ok is True and buf.getvalue() == "42\n", f"ok={ok}")

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), \
            contextlib.redirect_stderr(io.StringIO()):
        ok = nexmain.run_source(src, "<test>", backend="vm")
    check("VM backend hatasız kaynakta True",
          ok is True and buf.getvalue() == "42\n", f"ok={ok}")


def test_parity():
    section("Tree-walker ↔ VM parity")
    programs = {
        "aritmetik + fonksiyon": """
fn add(a: int, b: int) -> int:
    return a + b

fn main():
    print(add(6, 7) * 2)
""",
        "kontrol akışı + özyineleme": SAMPLE,
        "and/or + erken dönüş": """
fn pick(a: int, b: int) -> int:
    if a and b:
        return a
    return b

fn main():
    print(pick(1, 2))
    print(pick(0, 2))
    print(pick(3, 0))
""",
        "string + native": """
fn greet(name: str) -> str:
    return "hi " + name

fn main():
    print(greet("nex"))
    print(len(greet("nex")))
    print(str(3.5))
""",
        "sıfıra bölme öncesi çıktı": """
fn main():
    print("before")
    let x = 10
    let y = 2
    print(x / y)
""",
        "@stack: okuma/yazma/kaçış kopyası": """
fn fill(buf: [int]) -> [int]:
    buf[0] = 42
    return buf

fn main():
    @stack let buf: [int; 3] = [1, 2, 3]
    buf[1] = 7
    let copy = fill(buf)
    print(str(buf[0]))
    print(str(buf[1]))
    print(str(copy[0]))
    print(str(len(copy)))
""",
        "@stack: döngü gövdesi + float/bool": """
fn main():
    let i = 0
    let total = 0
    while i < 3:
        @stack let t: [int; 2] = [i, i + 1]
        total = total + t[1]
        i = i + 1
    print(total)
    @stack let f: [float; 2]
    f[0] = 3
    print(str(f))
    @stack let b: [bool; 2] = [true, false]
    print(str(b))
""",
        "@own: decl + to_gc + print": """
fn main():
    @own let p = to_own(5)
    let n = to_gc(p)
    print(str(n + 1))
""",
        "@own: free sonrası devam": """
fn main():
    @own let p = to_own(41)
    free(p)
    print("done")
""",
        "@own: yeniden atama": """
fn main():
    @own let p = to_own(1)
    p = to_own(7)
    let g = to_gc(p)
    print(str(g))
""",
        "@own: stack snapshot derin kopya": """
fn main():
    @stack let buf: [int; 2] = [7, 8]
    @own let p = to_own(buf)
    buf[0] = 99
    let g = to_gc(p)
    print(str(g[0]))
    print(str(buf[0]))
""",
        "@own: moved-out değere eleman yazma": """
fn main():
    @stack let buf: [int; 2] = [1, 2]
    @own let p = to_own(buf)
    let g = to_gc(p)
    g[0] = 9
    print(str(g[0]))
    print(str(g[1]))
""",
    }
    for name, src in programs.items():
        expect_parity(name, src)


def test_top_level_vm():
    section("Top-level statements → synthetic <script> entry")

    # Yalnız üst seviye statement'lar (hiç fn yok): sentetik <script>
    # girişi dosya sırasıyla çalışır, tree-walker çıktısıyla aynı.
    expect_parity(
        "top-level only: let/aritmetik/print (fn yok)",
        """
print("a")
let x = 3 * (2 + 4)
let y = x - 10
print(y)
print(str(x) + "!")
""")

    # Kullanıcı fonksiyonu üst seviyeden çağrılır (hoisted).
    expect_parity(
        "top-level calls user fn",
        """
fn greet(name: str) -> str:
    return "hi " + name

print(greet("x"))
print(len(greet("xy")))
""")

    # `fn main` tanımlıysa ikisi de yaşar: girişsentetik <script>,
    # üst seviyeden main() çağrılabilir.
    expect_parity(
        "fn main + top-level coexist",
        """
fn main():
    print("main")

let n = 2
print(n + 1)
main()
""")

    # Üst seviye if/else.
    expect_parity(
        "top-level if/else",
        """
let v = 7
if v > 5:
    print("big")
else:
    print("small")
print("end")
""")

    # Fonksiyon tablosu sırası ve giriş indeksi: kullanıcı fonksiyonları
    # önce, <script> en sonda; entry = <script> indeksi.
    m = compile_source("""
fn f() -> int:
    return 1

fn g() -> int:
    return 2

print(f() + g())
""")
    names = [fn.name for fn in m.functions]
    check("functions: user fns first, <script> last",
          names == ["f", "g", "<script>"], f"names={names!r}")
    check("entry = index of <script> (last)", m.entry == 2,
          f"entry={m.entry}")
    check("<script> arity 0", m.functions[m.entry].arity == 0)

    # Üst seviye statement yoksa eski davranış: entry = fn main (zorunlu).
    m2 = compile_source(SAMPLE)
    check("no top-level stmts: entry = fn main index", m2.entry == 2,
          f"entry={m2.entry}, names={[f.name for f in m2.functions]!r}")
    expect_compile_error(
        "no main + no top-level stmts still requires 'main'",
        "fn f() -> int:\n    return 1\n",
        "requires a 'main' function")

    # Boş dosya (0 fn, 0 statement): eski 'no functions to compile' hatası
    # bilinçli olarak korunur — sentetik girişin gövdesi boş üretilemez.
    expect_compile_error(
        "empty file: no functions to compile (kept)", "",
        "no functions to compile")


def test_for_vm():
    section("for döngüsü — range + indeksle yineleme (A.ForStmt)")

    # ── Range: while-style tamsayı sayaç döngüsü ────────────────────────
    expect_parity("for i in 0..10 sum (range, parity)", """
fn main():
    let total = 0
    for i in 0..10:
        total = total + i
    print(total)
""")

    expect_parity("for i in 1..6 (start != 0, parity)", """
fn main():
    let s = 0
    for i in 1..6:
        s = s + i
    print(s)
""")

    expect_output("range for: sayaç gövdede görünür + boş aralık", """
fn main():
    let total = 0
    for i in 2..5:
        total = total + i * i
    print(total)
    for i in 3..3:
        print("never")
    print("done")
""", ["29", "done"])

    # ── Indeksle yineleme: list ────────────────────────────────────────
    expect_parity("for n in [list] max-like reduction (parity)", """
fn main():
    let nums = [3, 1, 4, 1, 5, 9, 2, 6]
    let max_val = nums[0]
    for n in nums:
        if n > max_val:
            max_val = n
    print(max_val)
""")

    expect_output("for over list: eleman sırası korunur", """
fn main():
    let acc = ""
    for w in ["a", "b", "c"]:
        acc = acc + w
    print(acc)
    for x in []:
        print("never")
    print("empty ok")
""", ["abc", "empty ok"])

    # ── @stack: Ident yolu STACK_MATERIALIZE kopyası (to_list parity) ──
    expect_parity("for v in @stack buf (copy semantics, parity)", """
fn main():
    @stack let buf: [int; 4] = [2, 4, 6, 8]
    let total = 0
    for v in buf:
        total = total + v
    print(total)
""")

    # ── str: karakter karakter ─────────────────────────────────────────
    expect_parity('for c in "abc": print(c) (parity)', """
fn main():
    let out = ""
    for c in "abc":
        print(c)
        out = out + c
    print(out)
""")

    # ── break / continue / iç içe for ──────────────────────────────────
    expect_output("for + break", """
fn main():
    let s = 0
    for i in 0..100:
        if i == 4:
            break
        s = s + i
    print(s)
""", ["6"])

    expect_output("for + continue", """
fn main():
    let s = 0
    for i in 0..6:
        if i % 2 == 0:
            continue
        s = s + i
    print(s)
""", ["9"])

    expect_output("iç içe for + return gövdeden", """
fn find():
    for i in 0..3:
        for j in 0..3:
            if i * j == 2:
                return j
    return -1

fn main():
    print(find())
""", ["2"])

    # ── Borrow: continue gövde borrow'unu release etmeli ───────────────
    for_src = """
fn main():
    @own let xs = to_own([1, 2, 3])
    for v in 0..3:
        let r = &xs
        if v == 0:
            continue
    let g = to_gc(xs)
    print(str(len(g)))
"""
    expect_parity("for: continue releases loop-body borrow (parity)", for_src)

    # Sayaç kanıtı: continue release etmezse owner sayacı birikirdi.
    import vm as _vm
    acq_rec: list[int] = []
    _orig_acq = _vm.OwnValueValue.acquire_shared

    def _rec_acq(self):
        _orig_acq(self)
        acq_rec.append(self.shared)

    _vm.OwnValueValue.acquire_shared = _rec_acq
    try:
        _m, got = run_vm(for_src)
    finally:
        _vm.OwnValueValue.acquire_shared = _orig_acq
    check("for continue path: shared counter never exceeds 1",
          acq_rec and max(acq_rec) == 1, f"counters={acq_rec}")

    break_for_src = """
fn main():
    @own let xs = to_own([1, 2, 3])
    for v in 0..3:
        let r = &xs
        if v == 1:
            break
    let g = to_gc(xs)
    print(str(g[0]))
"""
    expect_parity("for: break releases loop-body borrow (parity)",
                  break_for_src)

    ret_for_src = """
fn probe() -> int:
    @own let xs = to_own([7, 8])
    for v in 0..3:
        let r = &xs
        if v == 2:
            return len(r)
    return 0

fn main():
    print(str(probe()))
"""
    expect_output("for: return from body releases borrow before RET",
                  ret_for_src, ["2"])

    m = compile_source(ret_for_src)
    for fn in m.functions:
        if fn.name == "probe":
            instrs = decode_code(fn.code)
            check("in-for return emits BORROW_END before RET",
                  any(ins.name == "RET" and instrs[idx - 1].name == "BORROW_END"
                      for idx, ins in enumerate(instrs) if idx > 0),
                  "no RET preceded by BORROW_END")

    # Hedef slot döngü kapsamındadır: gövde dışında görünmez.
    expect_compile_error("for hedefi gövde dışında görünmez", """
fn main():
    for i in 0..2:
        print(i)
    print(i)
""", "undefined variable")

    # ── Derleme zamanı reddi ───────────────────────────────────────────
    expect_compile_error("destructuring for target reddedilir", """
fn main():
    for a, b in [[1, 2], [3, 4]]:
        print(a)
""", "destructuring in for loops is not supported")

    # ── list.push ──────────────────────────────────────────────────────
    expect_output("xs.push(v) + len + eleman kontrolü", """
fn main():
    let xs = []
    xs.push(10)
    xs.push(20)
    xs.push(30)
    print(str(len(xs)))
    print(str(xs[0]) + "/" + str(xs[2]))
    print(str(xs))
""", ["3", "10/30", "[10, 20, 30]"])

    expect_parity("push + for döngüsü parity", """
fn main():
    let out = []
    for i in 0..5:
        out.push(i * 2)
    let total = 0
    for v in out:
        total = total + v
    print(total)
    print(str(out))
""")

    expect_parity("push tree-walker ile None döner (parity)", """
fn main():
    let xs = [1]
    let r = xs.push(2)
    print(str(r == null))
    print(str(len(xs)))
""")

    # to_own + push etkileşimi: borrow üzerinden push yok, GC kopyasında var
    expect_output("to_own → to_gc sonrası push serbest", """
fn main():
    let g = [1]
    g.push(2)
    @own let p = to_own(g)
    g.push(99)
    let h = to_gc(p)
    print(str(h))
    print(str(g))
""", ["[1, 2]", "[1, 2, 99]"])

    expect_output("bare push(...) da CALL_NATIVE olur", """
fn main():
    let xs = [1]
    push(xs, 5)
    print(str(len(xs)))
""", ["2"])

    expect_compile_error("push 0 argüman reddedilir", """
fn main():
    let xs = [1]
    xs.push()
""", "push() accepts exactly one argument")

    expect_compile_error("push 2 argüman reddedilir", """
fn main():
    let xs = [1]
    xs.push(1, 2)
""", "push() accepts exactly one argument")

    expect_compile_error("desteklenmeyen metot çağrısı reddedilir", """
fn main():
    let xs = [1]
    xs.foo(1)
""", "method call '.foo' is not supported")

    # push native'i tabloda: impure (CALL_NATIVE), borç argümanı
    # CALL_NATIVE sınırında reddedilir.
    m_push = compile_source("""
fn main():
    let xs = [1]
    xs.push(2)
""")
    push_native = next(n for n in m_push.natives if n.name == "push")
    check("push native impure (flags=0, argc=2)",
          push_native.argc == 2 and push_native.flags == 0,
          f"argc={push_native.argc} flags={push_native.flags}")
    d_push = disassemble(m_push)
    check("push CALL_NATIVE ile kodlanır (CALL_PURE_NATIVE değil)",
          "CALL_NATIVE" in d_push and "CALL_PURE_NATIVE" not in d_push, d_push)


def test_metadata():
    section("Meta — max_stack / line table / slot modu")
    m = compile_source(SAMPLE)
    for f in m.functions:
        ins = decode_code(f.code)
        check(f"{f.name}: son komut RET",
              ins[-1].name == "RET")
        check(f"{f.name}: max_stack >= 1", f.max_stack >= 1)
        check(f"{f.name}: slot_modes boyutu n_locals",
              len(f.slot_modes) == f.n_locals)
        check(f"{f.name}: line table pc'leri artan",
              all(a[0] < b[0] for a, b in zip(f.line_table,
                                               f.line_table[1:])) or
              len(f.line_table) <= 1)

    d = decode_code(compile_source("""
fn main():
    print(1)
""").functions[0].code)
    const_idx = next(i for i in d if i.name == "CONST").args[0]
    check("CONST sabiti doğru değere çözülür",
          compile_source("""
fn main():
    print(1)
""").const_value(const_idx) == 1)


# ─── Çalıştır ────────────────────────────────────────────────────────────────

def main() -> None:
    test_encoding()
    test_disassembler()
    test_execution_basics()
    test_control_flow()
    test_functions()
    test_call_depth()
    test_tree_call_depth()
    test_and_or()
    test_stack_vm()
    test_own_vm()
    test_borrow_vm()
    test_borrow_escape_paths()
    test_for_vm()
    test_list_dict_vm()
    test_errors()
    test_verifier_negatives()
    test_unimplemented_opcodes()
    test_vm_cli()
    test_exit_codes()
    test_parity()
    test_top_level_vm()
    test_metadata()

    total = passed + failed
    print(f"\n{BOLD}{'═'*50}")
    print("  Results: ", end="")
    print(f"{GREEN}{passed}/{total} passed{RESET}", end="")
    if failed:
        print(f"  {RED}{failed} failed{RESET}")
    else:
        print(f"  {GREEN}— All tests passed! ✓{RESET}")
    print(f"{'═'*50}{RESET}")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
