#!/usr/bin/env python3
"""Opcode kapsama denetimi (ci gate).

Kök neden: sözleşme kırılması — verifier, VM'in yürütmediği opcode'u kabul
ediyor; elle yazılmış modül `verify_module`'dan geçip runtime'da
"opcode BLOCK_ENTER is not implemented in the prototype VM" ile çöküyordu.
Bilinen olaylar:
  BLOCK_ENTER / BLOCK_EXIT  (0x58/0x59: kapsam stübü, dispatch yok)
  GET_MEMBER / SET_MEMBER   (MemberExpr v1'de yok)
  NEW_TUPLE / RAW_MAKE      (sonraki tur)
  LOAD_GLOBAL / STORE_GLOBAL (module sistemi yok)

Bu script vm.py metnini AST ile tarar, `_execute` dispatch'inin gerçekten
ele aldığı isimleri (name == "X" / name in ("A", "B") karşılaştırmaları)
toplar ve her OPCODES girişi için üç kapıdan birini zorunlu kılar:
  (a) VM dispatch gerçekten ele alıyor, ya da
  (b) bytecode.UNIMPLEMENTED_OPCODES'ta dürüstçe listeli (yükleme anında
      verify reddeder), ya da
  (c) UNREACHABLE'da gerekçe yorumuyla "tasarım gereği ulaşılamaz" listeli.

Drift (yeni opcode, listeden çıkarılmamış unimplemented, listede olup
VM'e eklenmiş opcode, yazım hatası) → exit 1. check_ast_coverage.py
gibi dış bağımlılığı yoktur.

Kullanım: python3 check_opcodes.py  (exit 1 = drift var)
"""

import ast as pyast
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))

from bytecode import OPCODES, UNIMPLEMENTED_OPCODES  # noqa: E402

VM_FILE = REPO / "vm.py"

# Aç yeşil ışık: tasarım gereği VM'e ulaşması imkânsız opcode'lar.
# Her giriş: opcode adı -> gerekçe. Yeni giriş gerekçesiz eklenmemeli.
UNREACHABLE: dict[str, str] = {}


def collect_vm_handled(path: Path) -> set[str]:
    """vm.py dispatch'inin `name == "X"` / `name in ("A", "B")` ile ele
    aldığı tüm string isimleri toplar (AST tabanlı, text taraması değil)."""
    tree = pyast.parse(path.read_text(encoding="utf-8"))
    handled: set[str] = set()
    for node in pyast.walk(tree):
        if not isinstance(node, pyast.Compare):
            continue
        if not (isinstance(node.left, pyast.Name) and node.left.id == "name"):
            continue
        for comp in node.comparators:
            candidates = [comp] if isinstance(comp, pyast.Constant) else (
                comp.elts if isinstance(comp, pyast.Tuple) else [])
            for c in candidates:
                if (isinstance(c, pyast.Constant)
                        and isinstance(c.value, str)):
                    handled.add(c.value)
    return handled


def main() -> int:
    handled = collect_vm_handled(VM_FILE)
    opcode_names = set(OPCODES)

    covered: list[str] = []
    unimplemented: list[str] = []
    unreachable: list[str] = []
    missing: list[str] = []

    for name in sorted(opcode_names):
        if name in handled:
            covered.append(name)
        elif name in UNIMPLEMENTED_OPCODES:
            unimplemented.append(name)
        elif name in UNREACHABLE:
            unreachable.append(f"{name} — {UNREACHABLE[name]}")
        else:
            missing.append(name)

    problems: list[str] = []
    if missing:
        problems.append(
            "OPCODES'ta olup VM'de dispatch'i OLMAYAN ve UNIMPLEMENTED_"
            "OPCODES/UNREACHABLE'da da listelenmeyen opcode'lar (verifier "
            "kabul eder, runtime çöker):\n    "
            + "\n    ".join(missing))
    stale = sorted(UNIMPLEMENTED_OPCODES & handled)
    if stale:
        problems.append(
            "UNIMPLEMENTED_OPCODES'ta listeli ama VM artık ele alıyor "
            "(listeden çıkarın):\n    " + "\n    ".join(stale))
    unknown = sorted((UNIMPLEMENTED_OPCODES | set(UNREACHABLE)) - opcode_names)
    if unknown:
        problems.append(
            "OPCODES tablosunda olmayan opcode listelenmiş (yazım hatası?):\n    "
            + "\n    ".join(unknown))

    print(f"Opcode coverage: {len(covered)} handled / "
          f"{len(unimplemented)} unimplemented / {len(unreachable)} unreachable "
          f"(total={len(opcode_names)})")

    if unimplemented:
        print("\nUnimplemented (yükleme anında verify reddeder):")
        for name in unimplemented:
            print(f"  ~ {name} (0x{OPCODES[name][0]:02X})")
    if unreachable:
        print("\nUnreachable (tasarım gereği):")
        for entry in unreachable:
            print(f"  ~ {entry}")

    if problems:
        print("\nDRIFT DETECTED:")
        for p in problems:
            print(f"  ! {p}")
        print("\n  Ya vm.py dispatch'ine opcode'u ekleyin, ya "
              "bytecode.UNIMPLEMENTED_OPCODES'a gerekçeyle ekleyin "
              "(verifier yükleme anında reddetsin), ya da UNREACHABLE'a "
              "gerekçeyle koyun.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
