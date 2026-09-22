"""行情分析模型输出解析；不改变其他 AI 功能的响应处理。"""

from __future__ import annotations

import json
from typing import Any


def extract_json_object(raw: str) -> dict[str, Any] | None:
    """从模型文本中提取最靠后的完整 JSON 对象。

    部分模型会在最终 JSON 前输出 ``<think>``、Markdown 代码块或草稿
    对象。逐位置使用标准 JSON 解码器可以正确处理嵌套花括号，同时避免
    贪婪正则把多个对象拼成一段无效 JSON。
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None

    decoder = json.JSONDecoder()
    try:
        value = decoder.decode(text)
    except json.JSONDecodeError:
        value = None
    else:
        return value if isinstance(value, dict) else None

    candidates: list[tuple[int, int, dict[str, Any]]] = []
    for start, character in enumerate(text):
        if character != '{':
            continue
        try:
            value, end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            candidates.append((end, -start, value))

    if not candidates:
        return None
    return max(candidates, key=lambda item: (item[0], item[1]))[2]
