"""行情分析模型输出解析边界测试。"""

import pytest

from app.market_state.model_output import extract_json_object


@pytest.mark.parametrize(
    ('raw', 'expected'),
    [
        ('{"value": 1}', {'value': 1}),
        ('```json\n{"value": 2}\n```', {'value': 2}),
        ('<think>普通推理</think>\n{"value": 3}', {'value': 3}),
        (
            '<think>先参考 {"phase":"BASE"}</think>\n'
            '{"phase":"ADVANCE","details":{"confirmed":true}}',
            {'phase': 'ADVANCE', 'details': {'confirmed': True}},
        ),
        (
            '草稿 {"phase":"BASE"}\n最终 {"phase":"ADVANCE"}\n完成',
            {'phase': 'ADVANCE'},
        ),
        ('没有 JSON', None),
        ('[1, 2, 3]', None),
    ],
)
def test_extract_json_object_isolated_for_market_state(raw, expected):
    assert extract_json_object(raw) == expected
