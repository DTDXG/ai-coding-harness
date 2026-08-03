#!/usr/bin/env python3
"""AI 反模式检查 —— 机械可检测的症状层。

设计约束(来自 ai-coding-rules-design.md 第七节):

  * **全部是警告,永远不拦截。** 这些规则都有反例,拦截会误伤正确行为
    (最典型:800 行文件里新增函数,很可能正是在拆分巨型函数)。
  * **允许误报,靠豁免收敛。** 每处豁免写理由,脚本统计豁免次数;
    某条规则长期只被豁免,说明规则错了,删掉它。
  * **不是「修了根因」的证据。** 它只能看代码外形。真正证明修复泛化的是
    AGENTS.md「验证」段那条:变体 + 邻近反例。

用法::

    python scripts/check.py                 # 检查 git 里改动过的 Python 文件
    python scripts/check.py path/to/f.py    # 检查指定文件/目录
    python scripts/check.py --all           # 检查全仓库
    python scripts/check.py --advise        # 额外判断要不要跑 code-simplifier
    python scripts/check.py --report        # 汇总 .check-hits.log,做月度规则复盘
    python scripts/check.py --json          # 机器可读输出
    python scripts/check.py --strict        # 有未豁免命中时 exit 1(CI 用,默认关)

豁免写法(写在命中行,或它上面一行)::

    text = query.lower()
    if "ping" in text:              # check: ignore[keyword-match] 这是协议探活,不是猜用户意图
        return PONG

    # check: ignore-file[prod-mock] 本文件是给本地开发用的 fake provider,不进生产构建

理由不是可选的。没写理由的豁免仍然生效,但会单独列出来提醒。
"""

from __future__ import annotations

# check: ignore-file[prod-mock] 本文件是检测器本身,fake/mock 是它的词汇表,不是假实现
# (这条豁免是自测时 check.py 扫自己扫出来的,顺便当成 ignore-file 的示例)
# check: ignore-file[file-too-long] 权衡后选单文件:要能直接 cp 进任意仓库、零安装、无相对导入。
# 拆成包会省下这条警告,但换来每个项目多一层目录和 sys.path 问题。超过 1000 行再拆。

import argparse
import ast
import io
import json
import os
import re
import subprocess
import sys
import tokenize
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

# ── 可调参数 ────────────────────────────────────────────────────────────────
MAX_FILE_LINES = 800
DUP_MIN_STATEMENTS = 5
DUP_SIMILARITY = 0.93
DUP_MAX_FUNCTIONS = 600  # 超过就跳过重复检测(O(n²)),并明说跳过了
LOG_NAME = ".check-hits.log"

RULES = {
    "file-too-long": f"单文件超过 {MAX_FILE_LINES} 行",
    "keyword-match": "对自由文本做关键词/正则判断来猜意图",
    "fake-success": "失败被转换成看似成功的结果",
    "prod-mock": "生产代码里出现 mock / fake / stub",
    "version-suffix": "新增 _v2 / _new / _legacy 命名",
    "dup-func": "疑似重复实现",
}

# ── 路径分类 ────────────────────────────────────────────────────────────────
NON_PROD_DIRS = {
    "tests", "test", "fixtures", "fixture", "examples", "example",
    "benchmarks", "docs", "migrations", "node_modules", ".venv", "venv", ".git",
}
NON_PROD_FILE_PREFIX = re.compile(r"^(test|repro|debug|scratch|sandbox|demo)[-_]", re.I)

def is_production(path: Path) -> bool:
    """生产代码?tests / fixture / 复现脚本 / 文档一律不是。"""
    parts = {p.lower() for p in path.parts}
    if parts & NON_PROD_DIRS:
        return False
    name = path.name
    if name == "conftest.py" or name.endswith("_test.py"):
        return False
    return not NON_PROD_FILE_PREFIX.match(name)


# ── 自由文本识别 ────────────────────────────────────────────────────────────
# 只看变量名的**最后一段**。这样 msg["role"] == "user" 不会误报
# (role 是自己定义的枚举字段),而 resp.choices[0].message.content 会正确命中。
#
# 已知召回缺口:参数叫 `q` / `s` / `inp` 这类单字母缩写时抓不到。加进来会误伤
# 队列变量、Django 的 Q、状态字符串,得不偿失。**别指望这条规则抓全** ——
# 真正兜底的是 AGENTS.md「验证」段的变体 + 邻近反例。
FREE_TEXT_WORDS = (
    "query", "qry", "message", "msg", "prompt", "text", "txt", "content",
    "response", "resp", "reply", "answer", "utterance", "question",
    "instruction", "completion", "transcript", "user_input", "raw_input",
)
FREE_TEXT_NAME = re.compile(r"(?:^|_)(?:" + "|".join(FREE_TEXT_WORDS) + r")(?:$|_)", re.I)
NORMALIZER_METHODS = {"lower", "upper", "strip", "casefold", "lstrip", "rstrip", "title"}

MOCK_NAME = re.compile(
    r"(?:(?:^|_)(mock|fake|dummy|stub|placeholder|canned|hardcoded)(?:$|_))|(Mock|Fake|Dummy|Stub)",
)
# 只留真正表示「新旧并存」的后缀。原先还有 copy|backup|orig|tmp|temp,
# 实测把 _json_safe_copy、end2end_backup 这类正常命名也报了 —— 那些是普通英文词。
VERSION_SUFFIX = re.compile(r"_(v\d+|legacy|deprecated|bak)$", re.I)
FAKE_STR_HINT = re.compile(r"^(n/?a|none|null|unknown|default|placeholder|todo|tbd|未知|默认|失败)$", re.I)

# ── 字面量性质判断 ──────────────────────────────────────────────────────────
CJK = re.compile(r"[一-鿿]")
IDENTIFIER_LIKE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.\-/]*$")


def looks_natural_language(s: str) -> bool:
    """字面量像自然语言,还是像字段名 / 枚举值?

    2026-07-31 在两个真实仓库上实测后加的。原先只要是字符串字面量就报,结果
    `if "text" in cur_content_tmp`(dict 取键)、`if resp_type in ["function_call"]`
    (枚举)全被当成"对自由文本猜意图"。加上这个过滤,误报砍掉 86%。
    代价:`if "refund" in query` 这种纯英文单词漏掉 —— 它和字段名静态上无法区分。
    """
    s = s.strip()
    if not s or IDENTIFIER_LIKE.match(s):
        return False
    return bool(CJK.search(s)) or " " in s


def regex_has_literal_text(pattern: str) -> bool:
    """正则里有没有实际的词?没有就是结构化解析(抽 JSON、抽标签),不是猜意图。"""
    stripped = re.sub(r"\\.", "", pattern)
    return bool(re.search(r"[一-鿿]|[A-Za-z]{3,}", stripped))


def dotted_name(node: ast.AST) -> str:
    """把属性/下标/规范化调用链压成点分名字。`query.lower()` -> `query`。"""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = dotted_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    if isinstance(node, ast.Subscript):
        base = dotted_name(node.value)
        key = node.slice
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            return f"{base}.{key.value}" if base else key.value
        return base
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in NORMALIZER_METHODS:
            return dotted_name(func.value)
        return dotted_name(func)
    return ""


def leaf_name(node: ast.AST) -> str:
    return dotted_name(node).rsplit(".", 1)[-1]


def looks_free_text(node: ast.AST) -> bool:
    return bool(FREE_TEXT_NAME.search(leaf_name(node)))


def is_text_literal(node: ast.AST) -> bool:
    """字符串字面量,或一组字符串字面量(`x in ("a", "b")`)。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return len(node.value) >= 2
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return bool(node.elts) and all(
            isinstance(e, ast.Constant) and isinstance(e.value, str) for e in node.elts
        )
    return False


def is_fake_value(node: ast.AST | None) -> bool:
    """看起来像「假装成功」的返回值。"""
    if node is None:  # 光秃秃的 return,等于隐式 None
        return True
    if isinstance(node, ast.Constant):
        v = node.value
        if v is None or v is False:
            return True
        if isinstance(v, str):
            return v == "" or bool(FAKE_STR_HINT.match(v.strip()))
        if isinstance(v, int) and not isinstance(v, bool):
            return v == 0
        return False
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return all(isinstance(e, ast.Constant) for e in node.elts)
    if isinstance(node, ast.Dict):
        return bool(node.values) and all(isinstance(v, ast.Constant) for v in node.values)
    if isinstance(node, ast.Call) and not node.args and not node.keywords:
        return dotted_name(node) in {"dict", "list", "set", "tuple", "str"}
    return False


def returns_optional(fn: ast.FunctionDef | ast.AsyncFunctionDef | None) -> bool:
    """返回类型显式声明了 None?

    那么 except 里 `return None` 不是伪装成功 —— 签名已经告诉调用方失败会返回 None,
    而 `None` 与任何成功值都可区分。判据是「调用方能否分辨」,不是「有没有 return None」。
    这条豁免只认**显式标注**,不标注就照报。
    """
    if fn is None or fn.returns is None:
        return False
    if isinstance(fn.returns, ast.Constant) and fn.returns.value is None:
        return True
    src = ast.unparse(fn.returns)
    return "None" in src or "Optional" in src


# ── 结果 ────────────────────────────────────────────────────────────────────
@dataclass
class Finding:
    rule: str
    path: str
    line: int
    message: str
    exempt: bool = False
    exempt_reason: str = ""


@dataclass
class Exemptions:
    per_line: dict[int, list[tuple[set[str], str]]] = field(default_factory=dict)
    file_level: list[tuple[set[str], str]] = field(default_factory=list)

    def match(self, rule: str, line: int) -> tuple[bool, str] | None:
        for rules, reason in self.file_level:
            if rule in rules or "*" in rules:
                return True, reason
        for probe in (line, line - 1):
            for rules, reason in self.per_line.get(probe, []):
                if rule in rules or "*" in rules:
                    return True, reason
        return None


EXEMPT_RE = re.compile(r"#\s*check:\s*ignore(-file)?\[([^\]]*)\]\s*(.*)$")


def collect_exemptions(path: Path, source: str) -> Exemptions:
    ex = Exemptions()
    comments: list[tuple[int, str]] = []
    if path.suffix == ".py":
        try:
            comments = [
                (tok.start[0], tok.string)
                for tok in tokenize.generate_tokens(io.StringIO(source).readline)
                if tok.type == tokenize.COMMENT
            ]
        except (tokenize.TokenError, IndentationError, SyntaxError):
            comments = []
    if not comments:
        comments = [(i + 1, line) for i, line in enumerate(source.splitlines()) if "check:" in line]

    for lineno, text in comments:
        m = EXEMPT_RE.search(text)
        if not m:
            continue
        rules = {r.strip() for r in m.group(2).split(",") if r.strip()}
        reason = m.group(3).strip()
        if m.group(1):  # ignore-file
            ex.file_level.append((rules, reason))
        else:
            ex.per_line.setdefault(lineno, []).append((rules, reason))
    return ex


# ── AST 检查 ────────────────────────────────────────────────────────────────
class Visitor(ast.NodeVisitor):
    def __init__(self, path: Path, production: bool) -> None:
        self.path = path.as_posix()
        self.production = production
        self.findings: list[Finding] = []
        self._except_depth = 0
        self._func_stack: list[ast.FunctionDef | ast.AsyncFunctionDef] = []

    def _add(self, rule: str, node: ast.AST, message: str) -> None:
        self.findings.append(Finding(rule, self.path, getattr(node, "lineno", 1), message))

    # --- keyword-match ---------------------------------------------------
    def visit_Compare(self, node: ast.Compare) -> None:
        if any(isinstance(op, (ast.In, ast.NotIn, ast.Eq, ast.NotEq)) for op in node.ops):
            operands = [node.left, *node.comparators]
            literal = next((o for o in operands if is_text_literal(o)), None)
            target = next((o for o in operands if looks_free_text(o)), None)
            if literal is not None and target is not None:
                values = (
                    [literal.value] if isinstance(literal, ast.Constant)
                    else [e.value for e in literal.elts]
                )
                if any(looks_natural_language(v) for v in values):
                    self._add(
                        "keyword-match", node,
                        f"对自由文本 `{dotted_name(target)}` 做字面量比较来猜意图",
                    )
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Attribute):
            if func.attr in {"startswith", "endswith"} and node.args:
                arg = node.args[0]
                if (
                    looks_free_text(func.value)
                    and isinstance(arg, ast.Constant)
                    and isinstance(arg.value, str)
                    and looks_natural_language(arg.value)
                ):
                    self._add(
                        "keyword-match", node,
                        f"`{dotted_name(func.value)}.{func.attr}(...)` 对自由文本做前后缀判断",
                    )
            if (
                func.attr in {"search", "match", "fullmatch", "findall", "finditer"}
                and isinstance(func.value, ast.Name)
                and func.value.id == "re"
                and len(node.args) >= 2
                and looks_free_text(node.args[1])
                # 模式必须是字面量:f-string 拼出来的多半是 <tag> 抽取这类结构化解析
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
                and regex_has_literal_text(node.args[0].value)
            ):
                self._add(
                    "keyword-match", node,
                    f"正则匹配自由文本 `{dotted_name(node.args[1])}`",
                )
        # prod-mock:调用点
        if self.production:
            name = dotted_name(node)
            if name and MOCK_NAME.search(name.rsplit(".", 1)[-1]):
                self._add("prod-mock", node, f"生产代码调用 `{name}`")
        self.generic_visit(node)

    # --- fake-success ----------------------------------------------------
    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        self._except_depth += 1
        self.generic_visit(node)
        self._except_depth -= 1

    def visit_Return(self, node: ast.Return) -> None:
        enclosing = self._func_stack[-1] if self._func_stack else None
        if (
            self._except_depth
            and self.production
            and is_fake_value(node.value)
            and not returns_optional(enclosing)
        ):
            shown = ast.unparse(node.value) if node.value is not None else "<空 return>"
            self._add(
                "fake-success", node,
                f"except 里 return {shown} —— 调用方分辨不出这是真结果还是失败。"
                f"要么把失败抛出去,要么把返回类型标成 `... | None` 让签名说清楚",
            )
        self.generic_visit(node)

    # --- version-suffix / prod-mock 定义点 --------------------------------
    def _check_def_name(self, node: ast.AST, name: str) -> None:
        if VERSION_SUFFIX.search(name):
            self._add("version-suffix", node, f"`{name}` 带版本/新旧后缀 —— 确认无调用方就直接删旧的")
        if self.production and MOCK_NAME.search(name):
            self._add("prod-mock", node, f"生产代码里定义了 `{name}`")

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self._check_def_name(node, node.name)
        self._func_stack.append(node)
        outer_except, self._except_depth = self._except_depth, 0  # 嵌套函数不继承 except 上下文
        self.generic_visit(node)
        self._except_depth = outer_except
        self._func_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._check_def_name(node, node.name)
        self.generic_visit(node)

    # --- prod-mock:import ------------------------------------------------
    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if self.production and node.module and "mock" in node.module:
            self._add("prod-mock", node, f"生产代码 import 了 `{node.module}`")
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        if self.production:
            for alias in node.names:
                if "mock" in alias.name:
                    self._add("prod-mock", node, f"生产代码 import 了 `{alias.name}`")
        self.generic_visit(node)

# ── 重复函数检测 ────────────────────────────────────────────────────────────
def _struct_signature(nodes: list[ast.stmt]) -> tuple[str, ...]:
    """只保留 AST 节点类型序列,丢掉所有标识符和常量 —— 换个名字骗不过去。"""
    out: list[str] = []

    def walk(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            out.append(type(child).__name__)
            walk(child)

    for stmt in nodes:
        out.append(type(stmt).__name__)
        walk(stmt)
    return tuple(out[:400])


def collect_functions(tree: ast.AST, path: Path) -> list[tuple[str, str, int, tuple[str, ...]]]:
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name.startswith("__"):
            continue
        body = [
            s for s in node.body
            if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant) and isinstance(s.value.value, str))
        ]
        # 数全部语句,不只是顶层 —— 一个 3 行但含嵌套循环的函数照样值得比对
        total = sum(1 for s in body for n in ast.walk(s) if isinstance(n, ast.stmt))
        if total < DUP_MIN_STATEMENTS:
            continue
        found.append((path.as_posix(), node.name, node.lineno, _struct_signature(body)))
    return found


def detect_duplicates(
    functions: list[tuple[str, str, int, tuple[str, ...]]],
    limit: int = DUP_MAX_FUNCTIONS,
) -> list[Finding]:
    findings: list[Finding] = []
    if len(functions) > limit:
        print(
            f"[跳过] 函数数 {len(functions)} 超过 {limit},未做重复检测。"
            f"用 --dup-limit 调高,或缩小检查范围。",
            file=sys.stderr,
        )
        return findings

    counters = [Counter(sig) for _, _, _, sig in functions]
    for i in range(len(functions)):
        for j in range(i + 1, len(functions)):
            (pa, na, la, sa), (pb, nb, lb, sb) = functions[i], functions[j]
            if na == nb:
                continue  # 同名多为接口实现/子类覆写,噪音太大
            if not sa or not sb:
                continue
            if abs(len(sa) - len(sb)) / max(len(sa), len(sb)) > 0.25:
                continue
            overlap = sum((counters[i] & counters[j]).values())
            if overlap / max(len(sa), len(sb)) < DUP_SIMILARITY:
                continue  # 便宜的多重集预筛
            if SequenceMatcher(None, sa, sb).ratio() >= DUP_SIMILARITY:
                findings.append(Finding(
                    "dup-func", pb, lb,
                    f"`{nb}` 与 {pa}:{la} 的 `{na}` 结构几乎相同 —— 能不能合并?",
                ))
    return findings


# ── 文件遍历 ────────────────────────────────────────────────────────────────
def run_git(args: list[str], root: Path) -> list[str] | None:
    """成功返回行列表(可能为空),**失败返回 None** —— 让调用方分辨得出这两种情况。

    最早的版本两种情况都返回 `[]`,于是 git 不可用时会安静地报告「没有文件改动」。
    这正是本脚本 fake-success 规则要抓的东西,而且是它扫自己时抓到的。
    """
    try:
        out = subprocess.run(
            ["git", *args], cwd=root, capture_output=True, text=True, timeout=15, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return [line for line in out.stdout.splitlines() if line.strip()]


def repo_root(start: Path) -> Path:
    lines = run_git(["rev-parse", "--show-toplevel"], start)
    return Path(lines[0]) if lines else start


def changed_files(root: Path) -> list[Path]:
    results = [
        run_git(["diff", "--name-only", "--diff-filter=ACMR", "HEAD"], root),
        run_git(["diff", "--cached", "--name-only", "--diff-filter=ACMR"], root),
        run_git(["ls-files", "--others", "--exclude-standard"], root),
    ]
    if all(r is None for r in results):
        print(
            "[警告] git 不可用或这里不是 git 仓库,拿不到改动列表。"
            "这不等于「没有改动」—— 用 --all 或直接传文件路径。",
            file=sys.stderr,
        )
        return []
    names: set[str] = set()
    for r in results:
        if r is not None:
            names |= set(r)
    return [root / n for n in sorted(names) if (root / n).is_file()]


def expand(paths: list[Path]) -> list[Path]:
    out: list[Path] = []
    for p in paths:
        if p.is_dir():
            out.extend(
                f for f in p.rglob("*")
                if f.is_file() and not (set(f.parts) & NON_PROD_DIRS - {"tests", "test", "docs"})
            )
        elif p.is_file():
            out.append(p)
    return out


def selectable(path: Path) -> bool:
    return path.suffix == ".py"


# ── 单文件检查 ──────────────────────────────────────────────────────────────
def check_file(path: Path, root: Path, want_dup: bool):
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return [], []

    try:
        rel = path.relative_to(root)
    except ValueError:
        rel = path

    findings: list[Finding] = []
    functions: list = []
    line_count = source.count("\n") + 1
    if line_count > MAX_FILE_LINES:
        findings.append(Finding(
            "file-too-long", rel.as_posix(), 1,
            f"{line_count} 行。拆不拆看职责边界 —— 如果这次正是在拆分,忽略本条",
        ))

    if path.suffix == ".py":
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as exc:
            print(f"[跳过] {rel}: 语法错误 line {exc.lineno}", file=sys.stderr)
            return findings, functions
        visitor = Visitor(rel, is_production(rel))
        visitor.visit(tree)
        findings.extend(visitor.findings)
        if want_dup:
            functions = collect_functions(tree, rel)

    ex = collect_exemptions(path, source)
    for f in findings:
        hit = ex.match(f.rule, f.line)
        if hit:
            f.exempt, f.exempt_reason = True, hit[1]
    return findings, functions


# ── 输出 ────────────────────────────────────────────────────────────────────
def render(findings: list[Finding], advice: list[str], skipped_dup: bool) -> str:
    live = [f for f in findings if not f.exempt]
    exempt = [f for f in findings if f.exempt]
    lines: list[str] = []

    by_file: dict[str, list[Finding]] = {}
    for f in live:
        by_file.setdefault(f.path, []).append(f)
    for path in sorted(by_file):
        lines.append(f"\n{path}")
        for f in sorted(by_file[path], key=lambda x: x.line):
            lines.append(f"  {f.line:>5}  [{f.rule}]  {f.message}")

    if live:
        counts = Counter(f.rule for f in live)
        tally = "  ".join(f"{r}×{n}" for r, n in counts.most_common())
        lines.append(f"\n{len(live)} 处警告(另有 {len(exempt)} 处已豁免):{tally}")
        lines.append(
            "全部是警告,不阻止任何操作,允许误报。确认是误报就在那行加:\n"
            "  # check: ignore[规则名] 为什么是误报\n"
            "不要为了让它闭嘴而改代码结构。"
        )
    elif exempt:
        lines.append(f"无未豁免的警告({len(exempt)} 处已豁免)。")
    else:
        lines.append("无警告。")

    no_reason = [f for f in exempt if not f.exempt_reason]
    if no_reason:
        lines.append(f"\n{len(no_reason)} 处豁免没写理由 —— 补一句为什么,否则一年后没人知道该不该删:")
        for f in no_reason[:10]:
            lines.append(f"  {f.path}:{f.line}  [{f.rule}]")

    if skipped_dup:
        lines.append("\n(本次跳过了重复函数检测)")

    if advice:
        lines.append("\n改动类型命中 code-simplifier 的触发条件:")
        for a in advice:
            lines.append(f"  - {a}")
        lines.append("建议现在跑 code-simplifier skill(它会先做变体 + 邻近反例验证,再过清单)。")

    return "\n".join(lines).strip()


def append_log(root: Path, findings: list[Finding]) -> None:
    if not findings:
        return
    stamp = datetime.now().strftime("%Y-%m-%d")
    try:
        with (root / LOG_NAME).open("a", encoding="utf-8") as fh:
            for f in findings:
                status = "exempt" if f.exempt else "hit"
                fh.write(f"{stamp}\t{f.rule}\t{status}\t{f.path}:{f.line}\n")
    except OSError:
        pass


def report(root: Path) -> int:
    """月度复盘:从来没命中的规则删掉,总被豁免的改掉。"""
    log = root / LOG_NAME
    if not log.exists():
        print(f"还没有 {LOG_NAME}。跑几周 check.py 之后再来看。")
        return 0

    hits: Counter[str] = Counter()
    exempts: Counter[str] = Counter()
    first: dict[str, str] = {}
    last: dict[str, str] = {}
    days: set[str] = set()
    for line in log.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        date, rule, status = parts[0], parts[1], parts[2]
        (exempts if status == "exempt" else hits)[rule] += 1
        days.add(date)
        first.setdefault(rule, date)
        last[rule] = date

    print(f"{'规则':<22}{'命中':>6}{'豁免':>6}{'豁免率':>8}   区间")
    print("-" * 70)
    for rule in RULES:
        h, e = hits[rule], exempts[rule]
        total = h + e
        rate = f"{e / total:.0%}" if total else "—"
        span = f"{first.get(rule, '—')} → {last.get(rule, '—')}"
        print(f"{rule:<22}{h:>6}{e:>6}{rate:>8}   {span}")

    print()
    # 样本太少时不给建议。否则第一天跑完就会劝你把所有规则删光 —— 一个只看
    # 「没命中」不看「跑过多少次」的指标,正是这套设计一直在警惕的代理指标失效。
    if len(days) < 10:
        print(f"  日志只覆盖 {len(days)} 天,样本太少,先攒够 10 天再看结论。")
        return 0
    for rule in RULES:
        h, e = hits[rule], exempts[rule]
        total = h + e
        if total == 0:
            print(f"  {rule}:{len(days)} 天里从未命中 —— 考虑删掉,留着也是死重量")
        elif total >= 5 and e / total > 0.7:
            print(f"  {rule}:{e}/{total} 被豁免 —— 规则范围错了,收窄它或删掉")
    return 0


# ── code-simplifier 触发判断 ────────────────────────────────────────────────
ADVICE_PATHS = (
    (re.compile(r"(prompt|instruction|schema|tool_?desc|persona)", re.I), "改了 prompt / 工具描述 / 输出 schema"),
    (re.compile(r"(auth|permission|acl|login|token|router|route|middleware|dispatch)", re.I), "改了鉴权 / 路由 / 分发路径"),
    (re.compile(r"(error|exception|handler|retry|fallback|recover)", re.I), "改了错误处理路径"),
)
ADDED_BRANCH = re.compile(r"^\+\s*(if|elif|except|case|match)\b")
ADDED_ASSERT = re.compile(r"^[+-]\s*assert\b")


def build_advice(root: Path) -> list[str]:
    """按改动类型触发,不按行数 —— 5 行的字符串特例比 200 行机械重命名危险得多。"""
    diff = (run_git(["diff", "-U0", "HEAD"], root) or []) + (run_git(["diff", "--cached", "-U0"], root) or [])
    if not diff:
        return []

    reasons: set[str] = set()
    current = ""
    new_files = {ln[6:] for ln in diff if ln.startswith("+++ b/")} - {
        ln[6:] for ln in diff if ln.startswith("--- a/")
    }
    for line in diff:
        if line.startswith("+++ b/"):
            current = line[6:]
            for pat, why in ADVICE_PATHS:
                if pat.search(current):
                    reasons.add(f"{why}({current})")
            continue
        if not current or line.startswith("+++") or line.startswith("---"):
            continue
        if ADDED_BRANCH.match(line) and current not in new_files:
            reasons.add(f"给已有代码新增了条件分支({current})")
        if ADDED_ASSERT.match(line) and ("test" in current or "spec" in current):
            reasons.add(f"改了测试断言({current}) —— 需要独立证据证明原断言写错了")
    return sorted(reasons)


# ── 入口 ────────────────────────────────────────────────────────────────────
def gather(args, root: Path) -> list[Path]:
    if args.all:
        return [p for p in expand([root]) if selectable(p)]
    if args.paths:
        return [p for p in expand([Path(p) for p in args.paths]) if selectable(p)]
    return [p for p in changed_files(root) if selectable(p)]


def analyse(files: list[Path], root: Path, want_dup: bool, dup_limit: int = DUP_MAX_FUNCTIONS):
    findings: list[Finding] = []
    functions: list = []
    for path in files:
        f, fn = check_file(path, root, want_dup)
        findings.extend(f)
        functions.extend(fn)
    skipped_dup = False
    if want_dup and functions:
        if len(functions) > dup_limit:
            skipped_dup = True
        findings.extend(detect_duplicates(functions, dup_limit))
    # 同一行可能被同一条规则报多次(`'a' in msg or 'b' in msg` 是两个 Compare 节点)
    seen: set[tuple[str, str, int, str]] = set()
    unique = []
    for f in findings:
        key = (f.rule, f.path, f.line, f.message)
        if key not in seen:
            seen.add(key)
            unique.append(f)
    return unique, skipped_dup


def hook_post_tool(root: Path) -> int:
    """Claude Code PostToolUse:只查刚改的那个文件,有问题就让模型立刻看见。"""
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as exc:
        # 不能让 hook 崩掉会话,但也不能装作没事 —— 写到 stderr,在 hook 日志里看得见
        print(f"[check.py] PostToolUse 输入解析失败:{exc}", file=sys.stderr)
        # check: ignore[fake-success] 返回值是 hook 退出码,不是业务结果;失败已经写进 stderr
        return 0
    raw = (payload.get("tool_input") or {}).get("file_path")
    if not raw:
        return 0
    path = Path(raw)
    if not path.is_file() or not selectable(path):
        return 0

    findings, _ = analyse([path], root, want_dup=False)
    append_log(root, findings)
    live = [f for f in findings if not f.exempt]
    if not live:
        return 0
    print(render(findings, [], False), file=sys.stderr)
    return 2  # exit 2 = 把 stderr 交给模型。工具已经执行完了,这不是拦截。


def hook_stop(root: Path) -> int:
    """Claude Code Stop:收尾时对着 diff 提醒一次。靠 stop_hook_active 保证只提醒一次。"""
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"[check.py] Stop 输入解析失败:{exc}", file=sys.stderr)
        payload = {}
    if payload.get("stop_hook_active"):
        return 0
    if os.environ.get("CHECK_STOP_BLOCK") == "0":
        return 0

    files = [p for p in changed_files(root) if selectable(p)]
    if not files:
        return 0
    findings, skipped = analyse(files, root, want_dup=True)
    append_log(root, findings)
    advice = build_advice(root)
    live = [f for f in findings if not f.exempt]
    if not live and not advice:
        return 0

    text = render(findings, advice, skipped)
    json.dump({"decision": "block", "reason": text}, sys.stdout, ensure_ascii=False)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="AI 反模式检查(全部为警告)")
    ap.add_argument("paths", nargs="*", help="文件或目录;留空则检查 git 改动过的文件")
    ap.add_argument("--all", action="store_true", help="检查全仓库")
    ap.add_argument("--advise", action="store_true", help="附带 code-simplifier 触发判断")
    ap.add_argument("--report", action="store_true", help="汇总 .check-hits.log")
    ap.add_argument("--json", action="store_true", dest="as_json")
    ap.add_argument("--strict", action="store_true", help="有未豁免命中时 exit 1")
    ap.add_argument("--no-dup", action="store_true", help="跳过重复函数检测")
    ap.add_argument("--dup-limit", type=int, default=DUP_MAX_FUNCTIONS)
    ap.add_argument("--no-log", action="store_true", help="不写 .check-hits.log")
    ap.add_argument("--hook-post-tool", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--hook-stop", action="store_true", help=argparse.SUPPRESS)
    # 兼容 AGENTS.md 里写的 --diff(就是默认行为)
    ap.add_argument("--diff", action="store_true", help="只查 git 改动过的文件(默认行为)")
    args = ap.parse_args()

    root = repo_root(Path.cwd())
    if args.report:
        return report(root)
    if args.hook_post_tool:
        return hook_post_tool(root)
    if args.hook_stop:
        return hook_stop(root)

    files = gather(args, root)
    if not files:
        print("没有要检查的文件。")
        return 0

    findings, skipped = analyse(files, root, want_dup=not args.no_dup, dup_limit=args.dup_limit)
    if not args.no_log:
        append_log(root, findings)
    advice = build_advice(root) if args.advise else []

    if args.as_json:
        json.dump(
            {
                "findings": [f.__dict__ for f in findings],
                "advice": advice,
                "files_checked": len(files),
            },
            sys.stdout, ensure_ascii=False, indent=2,
        )
        print()
    else:
        print(render(findings, advice, skipped))

    if args.strict and any(not f.exempt for f in findings):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
