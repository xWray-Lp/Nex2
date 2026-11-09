"""
Nex Bytecode / VM Test Suite (prototip alt kümesi)

Sözleşme: NEX_BYTECODE_SPEC.md (v0.1). Tasarım: NEX_VM_DESIGN.md.

Kapsam:
- encode → decode → encode roundtrip (byte eşitliği) + determinism
- disassembler determinism ve okunabilirlik
- aritmetik/karşılaştırma, if/elif/else, while + break/continue
- fonksiyon çağrısı, özyineleme, native çağrıları
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
import sys
import contextlib

from lexer import Lexer
from parser import Parser
from compiler import compile_program, CompileError
from bytecode import (
    BytecodeError, VerifyError, Module, FunctionCode, NativeInfo,
    OPCODES, MODE_TEMP, MODE_OWN, MODE_STACK, MODE_BORROW,
    NAT_PURE, NAT_RETAINS_REF, TAG_FUNC_REF, TAG_NATIVE_REF,
    encode_module, decode_module, decode_code, disassemble, verify_module,
)
from vm import VM, VMError, run_module

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
    expect_compile_error("desteklenmeyen list literal",
                         "fn main():\n    let x = [1, 2]\n",
                         "not supported")
    expect_compile_error("@own VM prototipinde desteklenmez",
                         "fn main():\n    @own let x = 1\n", "@own")
    expect_compile_error("top-level statement reddedilir",
                         "print(1)\n", "top-level")
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
    let x = [1, 2]
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
    }
    for name, src in programs.items():
        expect_parity(name, src)


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
    test_and_or()
    test_errors()
    test_verifier_negatives()
    test_vm_cli()
    test_parity()
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
