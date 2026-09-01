"""持仓管理策略实例的运行周期覆盖。"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Mapping

from .contract import CompiledStrategyV2, StrategyV2ContractError
from .frequencies import normalize_frequency
from .instruments import parse_instrument
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


def effective_position_management_instrument(
    manifest: StrategyManifest,
    position_management: Mapping[str, Any] | None,
) -> tuple[str, str] | None:
    """返回通用持仓管理实例绑定的真实品种和市场类型。"""
    config = position_management if isinstance(position_management, Mapping) else {}
    instrument = str(config.get("instrument") or "").strip()
    if (
        manifest.metadata_fields.get("position_management_generic") is not True
        or not instrument
    ):
        return None
    spec = parse_instrument(instrument)
    return spec.symbol, spec.market_type


def prepare_position_management_deployment(
    program: CompiledStrategyV2,
    value: Any,
) -> tuple[CompiledStrategyV2, dict[str, Any], tuple[str, str] | None]:
    """集中验证并生成部署阶段需要的持仓管理覆盖。"""
    position_management = value or {}
    if not isinstance(position_management, dict):
        raise StrategyV2ContractError("strategyV2.runtimeConfigInvalid")
    effective_program = effective_position_management_program(program, position_management)
    instrument = effective_position_management_instrument(
        effective_program.manifest, position_management
    )
    return effective_program, dict(position_management), instrument


def position_management_candidate(
    trading_config: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """将持仓管理实例配置转换为原执行器使用的候选品种。"""
    config = trading_config if isinstance(trading_config, Mapping) else {}
    position_management = config.get("position_management")
    position_management = (
        position_management if isinstance(position_management, Mapping) else {}
    )
    instrument = str(position_management.get("instrument") or "").strip()
    if position_management.get("enabled") is not True or not instrument:
        return None
    spec = parse_instrument(instrument)
    return {
        "key": spec.key,
        "market": spec.market,
        "symbol": spec.symbol,
        "exchange_id": spec.exchange_id,
        "market_type": spec.market_type,
        "instrument_id": spec.instrument_id,
    }


def resolve_position_management_candidates(
    trading_config: Mapping[str, Any] | None,
    fallback,
) -> tuple[list[dict[str, Any]], Any]:
    """管理实例只读取绑定品种，普通策略继续调用原候选解析。"""
    candidate = position_management_candidate(trading_config)
    return ([candidate], None) if candidate else fallback()


__all__ = [
    "SUPPORTED_POSITION_MANAGEMENT_TIMEFRAMES",
    "effective_position_management_instrument",
    "effective_position_management_manifest",
    "effective_position_management_program",
    "normalize_position_management_timeframe",
    "position_management_candidate",
    "prepare_position_management_deployment",
    "resolve_position_management_candidates",
]
