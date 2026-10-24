from __future__ import annotations

from typing import Any

from ..config import Settings, settings
from ..errors import PipelineStageError
from ..model_client import ModelClient
from .models import SqlExecution


class ResponseGenerator:
    """生成问答内容和查询结果说明。"""

    def __init__(
        self,
        model_client: ModelClient,
        config: Settings | None = None,
    ) -> None:
        self.model_client = model_client
        self.table_row_limit = max(1, (config or settings).context_table_row_limit)

    def finalize(
        self,
        query: str,
        execution: SqlExecution,
        schema_context: str,
        analysis_context: str,
    ) -> dict[str, Any]:
        system = """你是查询结果整理器。先判断结果能否回答问题，再判断问题能否靠改写SQL修正。
判定口径：
1. 结果能回答问题：valid=true，retryable=false。
2. 结果不能回答问题，但可通过改写SQL修正（字段选错、聚合方式错、过滤条件过严、
   时间范围错、漏了分组）：valid=false，retryable=true，并在reason中写明要改什么。
3. 结果不能回答问题，且改写SQL也无法修正（数据确实不存在、超出可见范围、
   问题与数据无关、结果为空且确认无数据）：valid=false，retryable=false。

只能使用结果中真实存在的数值。只返回JSON：
{"valid":true,"retryable":false,"reason":"...","title":"...","analysis":"..."}。"""
        user = (
            f"问题：{query}\nSQL：{execution.sql}\n列：{execution.columns}\n"
            f"结果数据：{execution.rows[: self.table_row_limit]}\nSchema：{schema_context}\n"
            f"用户保存的分析表格：{analysis_context or '无'}"
        )
        try:
            payload = self.model_client.chat_json(system, user)
            valid = bool(payload.get("valid", True))
            # 校验通过时不存在"是否需要重试"，避免模型输出自相矛盾的组合。
            retryable = bool(payload.get("retryable", False)) and not valid
            return {
                "valid": valid,
                "retryable": retryable,
                "reason": str(payload.get("reason") or ("结果检查通过" if valid else "结果未通过校验")),
                "title": str(payload.get("title") or "查询结果"),
                "analysis": str(payload.get("analysis") or f"查询返回{len(execution.rows)}行。"),
            }
        except RuntimeError as exc:
            raise PipelineStageError("result_analysis", str(exc)) from exc
