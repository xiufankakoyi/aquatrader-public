"""P3.2 行业模型注册表。

按 P3 计划 Task P3.2：维护已注册的行业模型，支持按行业/公司画像查找。
"""
from __future__ import annotations

from core.research.domain_models.base import (
    CompanyProfile,
    DomainModelError,
    DomainModelBase,
)


class DomainModelRegistry:
    """行业专用模型注册表。

    按 ``model_id`` 索引；``select_for(profile)`` 返回所有适用模型。
    """

    def __init__(self) -> None:
        self._models: dict[str, DomainModelBase] = {}

    def register(self, model: DomainModelBase) -> None:
        """登记行业模型。

        Args:
            model: 行业模型实例

        Raises:
            DomainModelError: model_id 为空或重复
        """
        if not isinstance(model, DomainModelBase):
            raise DomainModelError("行业模型类型无效")
        model_id = model.model_id
        if not isinstance(model_id, str) or not model_id.strip():
            raise DomainModelError("model_id 不能为空")
        if model_id in self._models:
            raise DomainModelError(f"行业模型已存在：{model_id}")
        self._models[model_id] = model

    def get(self, model_id: str) -> DomainModelBase | None:
        """按 model_id 获取模型。"""
        return self._models.get(model_id) if isinstance(model_id, str) else None

    def list_all(self) -> list[DomainModelBase]:
        """列出全部模型。"""
        return list(self._models.values())

    def select_for(self, profile: CompanyProfile) -> list[DomainModelBase]:
        """返回适用于指定公司画像的全部模型。

        Args:
            profile: 公司画像

        Returns:
            适用模型列表（按 model_id 排序）；空列表表示无适用模型
        """
        if not isinstance(profile, CompanyProfile):
            return []
        applicable = [
            model for model in self._models.values() if model.supports(profile)
        ]
        return sorted(applicable, key=lambda m: m.model_id)

    def clear(self) -> None:
        """清空注册表（仅供测试）。"""
        self._models.clear()


# 全局默认注册表
_DEFAULT_REGISTRY = DomainModelRegistry()


def get_default_registry() -> DomainModelRegistry:
    """获取全局默认注册表。"""
    return _DEFAULT_REGISTRY


def register_default_models() -> DomainModelRegistry:
    """登记第一批 3 个行业模型到默认注册表。

    Returns:
        已登记的注册表
    """
    from core.research.domain_models.manufacturing import ManufacturingModel
    from core.research.domain_models.consumer import ConsumerModel
    from core.research.domain_models.cyclical import CyclicalModel

    _DEFAULT_REGISTRY.clear()
    _DEFAULT_REGISTRY.register(ManufacturingModel())
    _DEFAULT_REGISTRY.register(ConsumerModel())
    _DEFAULT_REGISTRY.register(CyclicalModel())
    return _DEFAULT_REGISTRY
