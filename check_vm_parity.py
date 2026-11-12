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
  ERROR-PARITY          — for pinned runtime error programs, BOTH backends must
                          exit 1 and emit `Runtime Error at line L:C — <msg>`
                          (stdout+stderr merged, ANSI stripped) with the same
                          pinned message; line/col may diverge by design.

Deterministic, no network, no external deps (stdlib only).
"""

import re
import subprocess
import sys
import tempfile
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
    # examples/buffer_econ.nex: @raw Phase C — tree/VM byte-equal stdout.
    "examples/buffer_econ.nex": ("SUPPORTED", None),
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
RUNTIME_ERROR_RE = re.compile(r"^Runtime Error at line (\d+):(\d+) — (.+)$", re.M)

# Runtime-error programs: (name, source, pinned message). Never written to examples/.
ERROR_CASES = [
    ("div0",
     "fn main():\n    print(1 / 0)\n\nmain()\n",
     "Division by zero"),
    ("mod0",
     "fn main():\n    print(10 % 0)\n\nmain()\n",
     "Division by zero"),
    ("overflow_add",
     "fn main():\n    print(9223372036854775807 + 1)\n\nmain()\n",
     "integer overflow"),
    ("call_depth",
     "fn down(n: int) -> int:\n"
     "    if n == 0:\n"
     "        return 0\n"
     "    return down(n - 1)\n\n"
     "fn main():\n"
     "    print(down(5000))\n\n"
     "main()\n",
     "call depth limit exceeded (256)"),
]


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


def run_path(backend_flag: str, path: Path):
    """Run `main.py [flag] <absolute path>`; return (exit_code, stdout, stderr text)."""
    cmd = [sys.executable, str(ROOT / "main.py")]
    if backend_flag:
        cmd.append(backend_flag)
    cmd.append(str(path))
    p = subprocess.run(cmd, capture_output=True)
    out = p.stdout.decode("utf-8", "replace")
    err = strip_ansi(p.stderr.decode("utf-8", "replace"))
    return p.returncode, out, err


def main() -> int:
    failures = []
    n_supported = 0
    n_unsupported = 0
    n_error_parity = 0

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

    # Error-diagnostic parity (spec §8): same message format in both backends.
    with tempfile.TemporaryDirectory() as td:
        tdir = Path(td)
        for name, src, pinned_msg in ERROR_CASES:
            p = tdir / f"{name}.nex"
            p.write_text(src)
            t_ec, t_out, t_err = run_path("", p)
            v_ec, v_out, v_err = run_path("--vm", p)
            t_stream = strip_ansi(t_out + "\n" + t_err)
            v_stream = strip_ansi(v_out + "\n" + v_err)
            t_m = RUNTIME_ERROR_RE.search(t_stream)
            v_m = RUNTIME_ERROR_RE.search(v_stream)
            ok = True
            if t_ec != 1 or v_ec != 1:
                failures.append(
                    f"ERROR-PARITY {name}: expected exit 1/1, got "
                    f"tree={t_ec} vm={v_ec}")
                ok = False
            if not t_m:
                failures.append(
                    f"ERROR-PARITY {name}: tree missing 'Runtime Error at line L:C — msg': "
                    f"{t_stream.strip()[:160]!r}")
                ok = False
            if not v_m:
                failures.append(
                    f"ERROR-PARITY {name}: VM missing 'Runtime Error at line L:C — msg': "
                    f"{v_stream.strip()[:160]!r}")
                ok = False
            if t_m and t_m.group(3) != pinned_msg:
                failures.append(
                    f"ERROR-PARITY {name}: tree message {t_m.group(3)!r} != pinned {pinned_msg!r}")
                ok = False
            if v_m and v_m.group(3) != pinned_msg:
                failures.append(
                    f"ERROR-PARITY {name}: VM message {v_m.group(3)!r} != pinned {pinned_msg!r}")
                ok = False
            if t_m and v_m and t_m.group(3) != v_m.group(3):
                failures.append(
                    f"ERROR-PARITY {name}: message mismatch tree={t_m.group(3)!r} vm={v_m.group(3)!r}")
                ok = False
            if ok:
                n_error_parity += 1
                print(f"  OK  ERROR-PARITY          {name}")

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
          f"{n_error_parity} error-parity OK / "
          f"{len(failures)} drift")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
