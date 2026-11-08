"""
Nex Language — Giriş Noktası
  nex main.py <file.nex>     dosya çalıştırır
  nex main.py                etkileşimli REPL başlatır
  nex main.py --check <file> sadece tip kontrolü yapar
"""

import sys
import os

from lexer import Lexer
from parser import Parser, ParseError
from interpreter import Interpreter
from type_checker import TypeChecker

RESET  = "\033[0m"
BOLD   = "\033[1m"
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
DIM    = "\033[2m"


def run_source(source: str, filename: str = "<stdin>", check_only: bool = False) -> bool:
    """Kaynak kodu çalıştır. Hata varsa False döner."""
    try:
        tokens = Lexer(source).tokenize()
    except SyntaxError as e:
        print(f"{RED}Lexer Error:{RESET} {e}", file=sys.stderr)
        return False

    try:
        tree = Parser(tokens).parse()
    except ParseError as e:
        print(f"{RED}Parse Error:{RESET} {e}", file=sys.stderr)
        return False

    tc = TypeChecker(filename=filename)
    ok = tc.check(tree)
    if not ok:
        print(f"{YELLOW}Type Errors in {filename}:{RESET}", file=sys.stderr)
        for diag in tc.errors:
            print(f"  {diag}", file=sys.stderr)
        for diag in tc.warnings:
            print(f"  {diag}", file=sys.stderr)

    if check_only:
        if ok:
            print(f"{GREEN}✓ {filename}: no type errors{RESET}")
        return ok

    if not ok:
        return False

    interp = Interpreter()
    interp.interpret(tree)
    return True


def run_file(path: str, check_only: bool = False) -> None:
    """Dosyayı oku ve çalıştır."""
    if not os.path.exists(path):
        print(f"{RED}Error:{RESET} File not found: {path}", file=sys.stderr)
        sys.exit(1)

    with open(path, "r", encoding="utf-8") as f:
        source = f.read()

    if not source.strip():
        return

    success = run_source(source, path, check_only=check_only)
    if not success and not check_only:
        sys.exit(1)


def repl() -> None:
    """Etkileşimli REPL (Read-Eval-Print Loop)."""
    print(f"{BOLD}{CYAN}Nex Language REPL{RESET}  {DIM}(exit veya quit ile çık, :check ile tip kontrolü){RESET}")
    print()

    interp = Interpreter()
    buffer = []
    indent_level = 0

    while True:
        try:
            if buffer:
                prompt = f"{DIM}...{RESET} " + "    " * indent_level
            else:
                prompt = f"{CYAN}nex>{RESET} "

            line = input(prompt)
        except (EOFError, KeyboardInterrupt):
            print()
            break

        low = line.strip().lower()
        if low in ("exit", "quit", ":q"):
            break

        if low == ":clear":
            buffer.clear()
            indent_level = 0
            continue

        if low.startswith(":check "):
            path = line.strip()[7:].strip()
            run_file(path, check_only=True)
            continue

        buffer.append(line)

        ends_with_colon = line.rstrip().endswith(":")
        if ends_with_colon:
            indent_level += 1
            continue

        stripped = line.strip()
        if not stripped and indent_level > 0:
            indent_level -= 1
            if indent_level > 0:
                continue

        source = "\n".join(buffer) + "\n"
        buffer.clear()
        indent_level = 0

        if not source.strip():
            continue

        try:
            tokens = Lexer(source).tokenize()
            tree = Parser(tokens).parse()
            tc = TypeChecker(filename="<repl>")
            if not tc.check(tree):
                for diag in tc.errors:
                    print(f"  {diag}")
                continue
            interp.interpret(tree)
        except ParseError as e:
            print(f"{RED}Parse Error:{RESET} {e}")
        except SyntaxError as e:
            print(f"{RED}Lexer Error:{RESET} {e}")
        except Exception as e:
            print(f"{RED}Error:{RESET} {e}")


def main() -> None:
    args = sys.argv[1:]

    if not args:
        repl()
        return

    check_only = False
    files = []

    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ("--check", "-c"):
            check_only = True
        elif arg in ("--help", "-h"):
            print("Usage:")
            print("  main.py [file.nex]          — dosya çalıştır")
            print("  main.py                     — REPL başlat")
            print("  main.py --check [file.nex]  — sadece tip kontrolü")
            return
        else:
            files.append(arg)
        i += 1

    if not files:
        repl()
        return

    for path in files:
        run_file(path, check_only=check_only)


if __name__ == "__main__":
    main()
