#!/usr/bin/env python3
"""check.py 的回归自测。改完 check.py 就跑一遍::

    python selftest/run.py

它检查两件事:

1. `sample_hits.py` 上半部分的 6 条规则**全部**触发。
2. 下半部分的邻近反例**一条都不**触发 —— 这半边才是重点。
   一个只会报警的检查器没用,得先证明它不会到处误报,你才会一直开着它。

外加豁免机制和 file-too-long 的两个小用例。
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHECK = ROOT / "scripts" / "check.py"
SAMPLE = ROOT / "selftest" / "sample_hits.py"
NEGATIVE_MARKER = "不应该命中"

EXPECTED_RULES = {
    "keyword-match",
    "fake-success",
    "prod-mock",
    "version-suffix",
    "dup-func",
}

failures: list[str] = []


def run_check(*args: str) -> dict:
    proc = subprocess.run(
        [sys.executable, str(CHECK), "--json", "--no-log", *args],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    if proc.returncode not in (0, 1):
        raise SystemExit(f"check.py 崩了 (exit {proc.returncode}):\n{proc.stderr}")
    return json.loads(proc.stdout)


def negative_section_start() -> int:
    for i, line in enumerate(SAMPLE.read_text(encoding="utf-8").splitlines(), 1):
        if NEGATIVE_MARKER in line:
            return i
    raise SystemExit(f"在 {SAMPLE} 里找不到分隔标记 {NEGATIVE_MARKER!r}")


def case_sample() -> None:
    boundary = negative_section_start()
    result = run_check(str(SAMPLE))
    findings = result["findings"]

    fired = {f["rule"] for f in findings}
    missing = EXPECTED_RULES - fired
    if missing:
        failures.append(f"这些规则没触发:{sorted(missing)}")

    # file-too-long 不在样本里,单独测
    unexpected = fired - EXPECTED_RULES - {"file-too-long"}
    if unexpected:
        failures.append(f"触发了预期外的规则:{sorted(unexpected)}")

    false_positives = [f for f in findings if f["line"] > boundary]
    for f in false_positives:
        failures.append(
            f"邻近反例被误报:{f['path']}:{f['line']} [{f['rule']}] {f['message']}"
        )


def case_exemption() -> None:
    """带理由的豁免要生效;不带理由的也生效,但要被单独标出来。"""
    src = (
        "def f(query: str) -> str:\n"
        '    if "心跳 ping" in query:  # check: ignore[keyword-match] 协议探活不是猜意图\n'
        '        return "pong"\n'
        '    if "退款" in query:  # check: ignore[keyword-match]\n'
        '        return "refund"\n'
        '    return ""\n'
    )
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "exempt_case.py"
        path.write_text(src, encoding="utf-8")
        findings = run_check(str(path))["findings"]

    km = [f for f in findings if f["rule"] == "keyword-match"]
    if len(km) != 2:
        failures.append(f"豁免用例:期望 2 处 keyword-match,实际 {len(km)}")
        return
    if not all(f["exempt"] for f in km):
        failures.append("豁免用例:同行的 # check: ignore[...] 没生效")
    with_reason = [f for f in km if f["exempt_reason"]]
    if len(with_reason) != 1:
        failures.append(f"豁免用例:期望恰好 1 处带理由,实际 {len(with_reason)}")


def case_file_too_long() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "big.py"
        path.write_text("x = 1\n" * 900, encoding="utf-8")
        findings = run_check(str(path))["findings"]
    if not any(f["rule"] == "file-too-long" for f in findings):
        failures.append("file-too-long 没触发")


def case_no_false_positive_on_self() -> None:
    """check.py 自己应该是干净的。它扫别人,先扫得过自己。"""
    findings = run_check(str(CHECK))["findings"]
    live = [f for f in findings if not f["exempt"]]
    if live:
        for f in live:
            failures.append(f"check.py 自己被报了:{f['line']} [{f['rule']}] {f['message']}")


def main() -> int:
    case_sample()
    case_exemption()
    case_file_too_long()
    case_no_false_positive_on_self()

    if failures:
        print("FAIL")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("OK — 6 条规则全部触发,邻近反例零误报,豁免机制正常")
    return 0


if __name__ == "__main__":
    sys.exit(main())
