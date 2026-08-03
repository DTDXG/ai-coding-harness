"""check.py 的自测样本。

上半部分:每条规则**应该**命中的写法。
下半部分:每条规则**不应该**命中的写法(邻近反例)——
这份文件本身就是 AGENTS.md 那条「变体 + 邻近反例」要求的实践。

注意它放在 `selftest/` 而不是 `tests/`:好几条规则只对生产代码生效,
放进 `tests/` 会全部被豁免掉,测了个寂寞。这本身就是第一次自测发现的问题。

跑法::

    python selftest/run.py          # 断言式,改了 check.py 之后跑这个
    python scripts/check.py selftest/sample_hits.py --no-log --no-dup   # 只看输出
"""

from __future__ import annotations

import re

# ══════════════════════════════════════════════════════════════════
# 应该命中
# ══════════════════════════════════════════════════════════════════


def route_by_keyword(query: str) -> str:
    """keyword-match ×3"""
    if "退款" in query:                          # keyword-match
        return "refund_agent"
    if query.lower().startswith("帮我画"):        # keyword-match
        return "image_agent"
    if re.search(r"视频|video", query):           # keyword-match
        return "video_agent"
    return "default_agent"


def summarize(text: str) -> str:
    """fake-success:LLM 挂了就返回空串,调用方分辨不出"""
    try:
        return _call_llm(text)
    except Exception:
        return ""                                # fake-success


def load_profile(user_id: str) -> dict:
    """fake-success:占位 DTO"""
    try:
        return _fetch(user_id)
    except Exception:
        return {"name": "unknown", "age": 0}     # fake-success


def mock_payment_gateway(amount: int) -> dict:   # prod-mock
    return {"ok": True, "amount": amount}


def build_payload_v2(items: list) -> dict:       # version-suffix
    out = {}
    for it in items:
        out[it["id"]] = it
    return out


def collect_alpha(records: list) -> list:        # dup-func(与 collect_beta 结构相同)
    result = []
    for record in records:
        if record is None:
            continue
        value = record.get("value")
        result.append(value)
    return result


def collect_beta(rows: list) -> list:            # dup-func
    output = []
    for row in rows:
        if row is None:
            continue
        item = row.get("item")
        output.append(item)
    return output


# ══════════════════════════════════════════════════════════════════
# 不应该命中(邻近反例)
# ══════════════════════════════════════════════════════════════════


def dispatch_by_role(msg: dict) -> str:
    """msg["role"] 是自己定义的枚举字段,不是自由文本 —— 不该报 keyword-match"""
    if msg["role"] == "system":
        return "system"
    if msg["role"] in ("user", "assistant"):
        return "chat"
    return "other"


def handle_node(node: dict) -> str:
    """节点 type 同理"""
    if node["type"] == "imageNode":
        return "image"
    return "common"


def parse_frame(header: bytes) -> int:
    """协议识别,不是猜意图"""
    if header[:4] == b"RIFF":
        return 1
    return 0


def fetch_with_retry(url: str, attempts: int = 3) -> bytes:
    """显式失败处理:重试耗尽后异常照样抛给调用方 —— 不该报 fake-success"""
    last: Exception | None = None
    for _ in range(attempts):
        try:
            return _http_get(url)
        except TimeoutError as exc:
            last = exc
    raise RuntimeError("all attempts failed") from last


def close_quietly(handle) -> None:
    """except 里没有 return,不该报 fake-success"""
    try:
        handle.close()
    except OSError:
        pass


def find_config(name: str) -> dict | None:
    """签名显式声明了 `| None`,调用方分辨得出失败 —— 不该报 fake-success"""
    try:
        return _load(name)
    except FileNotFoundError:
        return None


def new_session(user: str) -> dict:
    """new_ 前缀的正常工厂函数 —— 不该报 version-suffix(只查后缀)"""
    payload: dict = {"user": user}
    payload["state"] = "open"
    payload["seq"] = 0
    payload["tags"] = []
    return payload


def _call_llm(text: str) -> str: ...
def _fetch(user_id: str) -> dict: ...
def _http_get(url: str) -> bytes: ...
def _load(name: str) -> dict: ...
