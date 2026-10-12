from __future__ import annotations

from core.quant.strategies.configurable.configurable_strategy import ConfigurableStrategy
from core.quant.strategies.strategy_factory import StrategyFactory


def test_config_strategy_id_is_executable_when_listed() -> None:
    factory = StrategyFactory()

    listed = {item["id"] for item in factory.list_strategies()}
    strategy = factory.create_strategy("dual_ma_v1")

    assert "dual_ma_v1" in listed
    assert "dual_ma_v1" in factory.get_available_strategies()
    assert isinstance(strategy, ConfigurableStrategy)


def test_factory_excludes_examples_and_test_strategies() -> None:
    factory = StrategyFactory()

    modules = {item["module"] for item in factory.list_strategies()}

    assert not any(
        module.rsplit(".", 1)[-1].startswith(("example", "test", "simple_test"))
        or ".templates." in module
        or ".sandbox." in module
        for module in modules
    )
