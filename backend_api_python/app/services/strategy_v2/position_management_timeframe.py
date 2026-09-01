"""持仓管理策略实例的运行周期覆盖。"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Mapping

from .contract import CompiledStrategyV2, StrategyV2ContractError
from .frequencies import normalize_frequency
from .models import StrategyManifest


SUPPORTED_POSITION_MANAGEMENT_TIMEFRAMES = frozenset({"15m", "1h", "4h", "1d"})


def normalize_position_management_timeframe(value: Any) -> str:
    timeframe = normalize_frequency(value, default="")
    if timeframe not in SUPPORTED_POSITION_MANAGEMENT_TIMEFRAMES:
        raise StrategyV2ContractError("strategyV2.positionManagementTimeframeInvalid")
    return timeframe


def effective_position_management_manifest(
    manifest: StrategyManifest,
    position_management: Mapping[str, Any] | None,
) -> StrategyManifest:
    """仅允许通用持仓管理源码按实例覆盖订阅周期。"""
    config = position_management if isinstance(position_management, Mapping) else {}
    if manifest.metadata_fields.get("position_management_generic") is not True:
        return manifest
    if config.get("enabled") is not True:
        return manifest
    raw_timeframe = config.get("timeframe")
    if raw_timeframe is None or not str(raw_timeframe).strip():
        return manifest
    timeframe = normalize_position_management_timeframe(raw_timeframe)
    subscriptions = tuple(
        replace(subscription, frequency=timeframe)
        for subscription in manifest.subscriptions
    )
    return replace(manifest, subscriptions=subscriptions)


def effective_position_management_program(
    program: CompiledStrategyV2,
    position_management: Mapping[str, Any] | None,
) -> CompiledStrategyV2:
    manifest = effective_position_management_manifest(program.manifest, position_management)
    if manifest is program.manifest:
        return program
    return replace(program, manifest=manifest)


__all__ = [
    "SUPPORTED_POSITION_MANAGEMENT_TIMEFRAMES",
    "effective_position_management_manifest",
    "effective_position_management_program",
    "normalize_position_management_timeframe",
]
