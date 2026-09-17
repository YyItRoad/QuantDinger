"""可替换的计算模板；业务服务不解释指标含义或形态阈值。"""
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AnalysisRecipe:
    name: str
    version: str
    bars: int
    indicators: dict
    params: dict
    code: str
    prompt: str


def default_recipe():
    root = Path(__file__).parent / 'templates'
    return AnalysisRecipe(
        name='基础行情观察', version='1', bars=180,
        indicators={
            'ma7': {'name': 'SMA', 'params': {'timeperiod': 7}},
            'ma25': {'name': 'SMA', 'params': {'timeperiod': 25}},
            'ma90': {'name': 'SMA', 'params': {'timeperiod': 90}},
            'atr14': {'name': 'ATR', 'params': {'timeperiod': 14}},
        },
        params={'window': 120, 'recent': 6},
        code=(root / 'basic.py').read_text(encoding='utf-8'),
        prompt=(root / '基础行情观察.md').read_text(encoding='utf-8'),
    )
