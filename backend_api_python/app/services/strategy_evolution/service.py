"""Application service that adapts Strategy API V2 to the evolution engine."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services.script_source import get_script_source_service
from app.services.strategy_v2 import StrategyV2BacktestService

from .constraints import discover_parameter_constraints
from .engine import StrategyEvolutionEngine
from .evaluator import PreparedEvolutionEvaluator
from .models import EvolutionConfig, SearchParameter
from .parameter_space import adapt_source_for_parameters, build_parameter_space
from .search import ParameterSampler


class StrategyEvolutionService:
    def __init__(self, *, backtest_service: StrategyV2BacktestService | None = None) -> None:
        self.backtest_service = backtest_service or StrategyV2BacktestService()

    def run(self, *, user_id: int, payload: dict[str, Any], on_progress=None) -> dict[str, Any]:
        source_id = _positive_int(payload.get("sourceId"))
        if not source_id:
            raise ValueError("strategyEvolution.sourceRequired")
        source = get_script_source_service().get_source(source_id, user_id=user_id)
        if not source:
            raise ValueError("strategyEvolution.sourceNotFound")
        code = str(source.get("code") or "").strip()
        if not code:
            raise ValueError("strategyEvolution.sourceCodeRequired")
        start_date = _date(payload.get("startDate"), "strategyEvolution.startDateRequired")
        end_date = _date(payload.get("endDate"), "strategyEvolution.endDateRequired", end_of_day=True)
        config = EvolutionConfig.from_payload(payload.get("config"))
        parameter_rows = self._parameter_rows(source, payload.get("parameterSpace"))
        parameters = [SearchParameter.from_payload(item) for item in parameter_rows]
        code = adapt_source_for_parameters(code, parameter_rows)
        constraints = discover_parameter_constraints(code, {item.name for item in parameters})
        initial_capital = max(10.0, float(payload.get("initialCapital") or 10_000))
        leverage_enabled = bool(payload.get("leverageEnabled", False))
        leverage = max(1.0, float(payload.get("leverage") or 1.0))
        commission = max(0.0, min(1.0, float(payload.get("commission") or 0.0)))
        slippage = max(0.0, min(1.0, float(payload.get("slippage") or 0.0)))

        if isinstance(self.backtest_service, StrategyV2BacktestService):
            evaluate = PreparedEvolutionEvaluator(
                backtest_service=self.backtest_service,
                user_id=user_id,
                code=code,
                start_date=start_date,
                end_date=end_date,
                initial_capital=initial_capital,
                leverage_enabled=leverage_enabled,
                leverage=leverage,
                source_id=source_id,
                strategy_name=str(source.get("name") or ""),
            )
        else:
            def evaluate(params, segment_start, segment_end, segment_commission, segment_slippage):
                _run_id, segment_result = self.backtest_service.run(
                    user_id=user_id,
                    code=code,
                    start_date=segment_start,
                    end_date=segment_end,
                    initial_capital=initial_capital,
                    leverage_enabled=leverage_enabled,
                    leverage=leverage,
                    commission=segment_commission,
                    slippage=segment_slippage,
                    params=params,
                    persist=False,
                    source_id=source_id,
                    strategy_name=str(source.get("name") or ""),
                )
                return segment_result

        result = StrategyEvolutionEngine(evaluate).run(
            parameters=parameters,
            config=config,
            start_date=start_date,
            end_date=end_date,
            commission=commission,
            slippage=slippage,
            constraints=constraints,
            on_progress=on_progress,
        )
        result["source"] = {"id": source_id, "name": str(source.get("name") or "")}
        return result

    @staticmethod
    def parameter_space(*, user_id: int, source_id: int) -> dict[str, Any]:
        source = get_script_source_service().get_source(source_id, user_id=user_id)
        if not source:
            raise ValueError("strategyEvolution.sourceNotFound")
        return build_parameter_space(source)

    @staticmethod
    def estimate(payload: dict[str, Any]) -> dict[str, Any]:
        config = EvolutionConfig.from_payload(payload.get("config"))
        parameters = [SearchParameter.from_payload(item) for item in (payload.get("parameterSpace") or [])]
        parameter_count = len(parameters)
        sampler = ParameterSampler(parameters, seed=config.seed)
        search_space_size = sampler.space_size() if parameters else 0
        trial_count = min(config.trials, search_space_size) if search_space_size else config.trials
        minimum_recommended_trials = _recommended_trial_floor(
            config.method,
            parameter_count,
            search_space_size,
        )
        fold_runs = trial_count
        baseline_is_cached = config.blind_ratio > 0 and 1.0 in config.cost_multipliers
        stress_runs = len(config.cost_multipliers) - (1 if baseline_is_cached else 0)
        total_runs = fold_runs + stress_runs + (1 if config.blind_ratio > 0 else 0)
        universe_size = max(1, min(5000, int(payload.get("universeSize") or 1)))
        relative_units = total_runs * universe_size
        tier = "low" if relative_units < 500 else "medium" if relative_units < 5000 else "high"
        return {
            "backtestRuns": total_runs,
            "relativeUnits": relative_units,
            "resourceTier": tier,
            "parameterCount": parameter_count,
            "trials": trial_count,
            "requestedTrials": config.trials,
            "searchSpaceSize": search_space_size,
            "coveragePercent": round((trial_count / search_space_size) * 100.0, 4) if search_space_size else 0.0,
            "minimumRecommendedTrials": minimum_recommended_trials,
            "sampleSufficient": trial_count >= minimum_recommended_trials,
            "folds": config.folds,
            "walkForwardMode": "singlePass",
            "monteCarloPaths": config.monte_carlo_paths,
            "isUpperBound": False,
        }

    @staticmethod
    def _parameters(source: dict[str, Any], raw: Any) -> list[SearchParameter]:
        return [SearchParameter.from_payload(item) for item in StrategyEvolutionService._parameter_rows(source, raw)]

    @staticmethod
    def _parameter_rows(source: dict[str, Any], raw: Any) -> list[dict[str, Any]]:
        definitions = list(build_parameter_space(source).get("parameters") or [])
        allowed = {str(item.get("name") or ""): item for item in definitions}
        requested = list(
            (item for item in definitions if item.get("optimizable", True))
            if raw is None
            else (raw or [])
        )
        output: list[dict[str, Any]] = []
        for item in requested:
            name = str(item.get("name") or "")
            if name not in allowed:
                raise ValueError("strategyEvolution.parameterNotDeclared")
            declared = dict(allowed[name])
            if not declared.get("optimizable", True):
                raise ValueError("strategyEvolution.parameterNotOptimizable")
            merged = dict(declared)
            kind = str(declared.get("type") or "").lower()
            if kind in {"int", "integer", "float", "number", "percent"}:
                declared_min = declared.get("min")
                declared_max = declared.get("max")
                requested_min = item.get("min", declared_min)
                requested_max = item.get("max", declared_max)
                if declared_min is None or declared_max is None:
                    raise ValueError("strategyEvolution.parameterRangeUndeclared")
                if float(requested_min) < float(declared_min) or float(requested_max) > float(declared_max):
                    raise ValueError("strategyEvolution.parameterRangeOutsideDeclaration")
                requested_step = item.get("step", declared.get("step"))
                step = float(requested_step) if requested_step is not None else None
                span = float(requested_max) - float(requested_min)
                if step is None or step <= 0 or (kind in {"int", "integer"} and not step.is_integer()):
                    raise ValueError("strategyEvolution.parameterStepInvalid")
                if span > 0 and step > span:
                    raise ValueError("strategyEvolution.parameterStepInvalid")
                merged["min"] = requested_min
                merged["max"] = requested_max
                merged["step"] = int(step) if kind in {"int", "integer"} else step
            option_rows = merged.get("options") or merged.get("values")
            if option_rows and not merged.get("choices"):
                merged["choices"] = [option.get("value") if isinstance(option, dict) else option for option in option_rows]
            SearchParameter.from_payload(merged)
            output.append(merged)
        return output


def _recommended_trial_floor(method: str, parameter_count: int, search_space_size: int) -> int:
    if method == "grid":
        return min(search_space_size, 300)
    if method == "random":
        return min(search_space_size, max(48, parameter_count * 24))
    return min(search_space_size, max(32, parameter_count * 16))


def _date(value: Any, error_code: str, *, end_of_day: bool = False) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise ValueError(error_code)
    result = datetime.strptime(text, "%Y-%m-%d")
    return result.replace(hour=23, minute=59, second=59) if end_of_day else result


def _positive_int(value: Any) -> int | None:
    try:
        result = int(value)
        return result if result > 0 else None
    except (TypeError, ValueError):
        return None
