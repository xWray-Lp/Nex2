#!/usr/bin/env python3
"""
VM ↔ tree-walker parity matrix — CI gate.

Discovers all example programs, runs each through BOTH backends via
`main.py` subprocesses, and checks the result against the bucket pinned
below. Buckets were determined empirically (2026-11-11).

  SUPPORTED             — tree and VM exit codes AND stdout must match exactly.
  KNOWN_VM_UNSUPPORTED  — VM is expected to fail with a pinned reason.
                          Drift in EITHER direction is a failure: the program
                          must not start passing, and it must not fail for a
                          different reason.

Deterministic, no network, no external deps (stdlib only).
"""

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# Pinned matrix. ONE entry per program, ONE line reason per entry.
# Reason substrings are matched against the VM stderr (ANSI codes stripped).
# ---------------------------------------------------------------------------
# (bucket, pinned reason substring)
MATRIX = {
    # examples/vm_basics.nex: designed for the VM; tree output matches exactly.
    "examples/vm_basics.nex": ("SUPPORTED", None),
    # examples/hello.nex: for loops (range + list by-index) compile and run
    # byte-identical to the tree backend.
    "examples/hello.nex": ("SUPPORTED", None),
    # examples/fizzbuzz.nex: top-level `for` (line 1) — range loop, VM parity.
    "examples/fizzbuzz.nex": ("SUPPORTED", None),
    # examples/stack.nex: `for v in buf` materializes the @stack buffer as a
    # heap copy (STACK_MATERIALIZE), matching the tree backend's to_list.
    "examples/stack.nex": ("SUPPORTED", None),
    # examples/data_pipeline.nex: for over list params + `out.push(...)`
    # (CALL_NATIVE push); full parity with the tree backend.
    "examples/data_pipeline.nex": ("SUPPORTED", None),
    # examples/buffer_econ.nex: top-level `@raw` (line 19) — @raw not in VM.
    "examples/buffer_econ.nex": ("KNOWN_VM_UNSUPPORTED",
                                 "@raw is not supported in the VM prototype"),
    # examples/tree_own.nex: classes not supported in VM (ClassDecl).
    "examples/tree_own.nex": ("KNOWN_VM_UNSUPPORTED",
                              "ClassDecl is not supported in the VM prototype"),
    # test_features.nex: top-level statements now compile (synthetic <script>
    # entry), but the script calls `println` which is not in the VM's native
    # table (tree-walker has it as a builtin; line 6).
    "test_features.nex": ("KNOWN_VM_UNSUPPORTED",
                          "undefined function 'println'"),
}

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def run(backend_flag: str, rel_path: str):
    """Run `main.py [flag] file`; return (exit_code, stdout_bytes, stderr_text)."""
    cmd = [sys.executable, str(ROOT / "main.py")]
    if backend_flag:
        cmd.append(backend_flag)
    cmd.append(str(ROOT / rel_path))
    p = subprocess.run(cmd, capture_output=True)
    return p.returncode, p.stdout, strip_ansi(p.stderr.decode("utf-8", "replace"))


def main() -> int:
    failures = []
    n_supported = 0
    n_unsupported = 0

    for rel_path, (bucket, reason) in MATRIX.items():
        if not (ROOT / rel_path).exists():
            failures.append(f"{rel_path}: pinned in MATRIX but file is missing")
            continue

        t_ec, t_out, t_err = run("", rel_path)
        v_ec, v_out, v_err = run("--vm", rel_path)

        if bucket == "SUPPORTED":
            if v_ec != 0:
                failures.append(
                    f"{rel_path}: drift — SUPPORTED but VM now fails (exit {v_ec}): "
                    f"{v_err.strip()[:160]}")
            elif v_ec != t_ec or v_out != t_out:
                failures.append(
                    f"{rel_path}: drift — SUPPORTED output mismatch "
                    f"(tree exit {t_ec}, VM exit {v_ec}, stdout differs)")
            else:
                n_supported += 1
                print(f"  OK  SUPPORTED            {rel_path}")
        else:  # KNOWN_VM_UNSUPPORTED
            if v_ec == 0 and not v_err:
                failures.append(
                    f"{rel_path}: drift — expected VM failure ({reason!r}) "
                    f"but VM now passes (exit 0)")
            elif reason not in v_err:
                failures.append(
                    f"{rel_path}: drift — reason changed. Expected {reason!r}, "
                    f"got (exit {v_ec}): {v_err.strip()[:160]}")
            else:
                n_unsupported += 1
                print(f"  OK  KNOWN_VM_UNSUPPORTED {rel_path}  [{reason[:48]}…]")

    # Disasm smoke for the VM example (moved into the matrix from tests.yml).
    p = subprocess.run(
        [sys.executable, str(ROOT / "main.py"), "--disasm",
         str(ROOT / "examples/vm_basics.nex")],
        capture_output=True)
    if p.returncode != 0:
        failures.append(
            "examples/vm_basics.nex --disasm: exit "
            f"{p.returncode}: {strip_ansi(p.stderr.decode('utf-8','replace')).strip()[:160]}")
    else:
        print(f"  OK  DISASM                examples/vm_basics.nex")

    print()
    if failures:
        print("VM parity failures:")
        for f in failures:
            print(f"  FAIL  {f}")
    print(f"VM parity: {n_supported} supported OK / "
          f"{n_unsupported} known unsupported pinned / "
          f"{len(failures)} drift")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
