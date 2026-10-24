from __future__ import annotations

import json
from fnmatch import fnmatch
from typing import Any, Callable

from ..errors import PipelineStageError
from ..mcp_runtime.client import LocalMcpClient
from ..model_client import ModelClient
from ..skills import SkillDefinition


class SingleDatabaseAgent:
    """让模型通过MCP工具完成单数据库查询。"""

    def __init__(
        self,
        model_client: ModelClient,
        mcp_client_factory: Callable[[dict[str, Any]], LocalMcpClient],
        skill: SkillDefinition,
        max_tool_calls: int = 3,
    ) -> None:
        self.model_client = model_client
        self.mcp_client_factory = mcp_client_factory
        self.skill = skill
        self.max_tool_calls = min(max_tool_calls, skill.max_tool_calls)

    def prepare(
        self,
        query: str,
        database: str,
        schema_graph: dict[str, Any],
        schema_context: str,
        retrieval: dict[str, Any],
        workspace: dict[str, Any],
        access_scope: dict[str, Any],
        correction: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        database_tool = f"query_{database}"
        mcp_client = self.mcp_client_factory(access_scope)

        # 每次请求都通过MCP tools/list获取当前工具定义。
        catalog = mcp_client.list_tools()
        tools = [
            tool
            for tool in catalog
            if any(fnmatch(tool["name"], pattern) for pattern in self.skill.allowed_tools)
            and (not tool["name"].startswith("query_") or tool["name"] == database_tool)
        ]
        tool_names = {tool["name"] for tool in tools}
        if database_tool not in tool_names:
            raise PipelineStageError(
                "mcp_tool_discovery",
                f"没有找到数据库工具：{database_tool}",
            )

        system = f"你是单数据库问数智能体。\n\n{self.skill.instructions}"
        if correction:
            system += (
                "\n\n本次是结果校验未通过后的重写：上一次SQL已执行成功，但结果没有回答用户问题。"
                "请依据 previous_attempt.validation_reason 修正查询口径"
                "（字段、聚合方式、过滤条件、时间范围、分组），不要重复上一次的写法。"
            )

        observations: list[dict[str, Any]] = []
        tool_trace: list[dict[str, Any]] = []
        last_execution: dict[str, Any] | None = None
        base_payload = {
            "query": query,
            "database": database,
            "schema_graph": schema_graph,
            "retrieval": {
                "threshold": retrieval.get("threshold"),
                "selected_fields": retrieval.get("hits", []),
                "low_confidence_candidates": retrieval.get("low_confidence_candidates", []),
            },
            "confirmed_fields": workspace.get("schema_fields", []),
            "confirmed_parameters": workspace.get("confirmed_parameters", {}),
            "schema_text": schema_context,
            "mcp_tools": tools,
        }
        if correction:
            base_payload["previous_attempt"] = correction

        try:
            for call_index in range(1, self.max_tool_calls + 1):
                payload = {**base_payload, "tool_results": observations}
                decision = self.model_client.chat_json(
                    system,
                    json.dumps(payload, ensure_ascii=False),
                )

                action = str(decision.get("action") or "")
                if action not in self.skill.output_actions:
                    raise ValueError(f"Skill不允许输出动作：{action}")

                if action == "clarify":
                    clarification = decision.get("clarification")
                    if not isinstance(clarification, dict) or len(
                        clarification.get("options") or []
                    ) < 2:
                        raise ValueError("智能体返回的澄清信息不完整")
                    return {
                        "action": "clarify",
                        "clarification": clarification,
                        "tool_trace": tool_trace,
                    }

                if action != "call_tool":
                    raise ValueError("智能体必须返回call_tool或clarify")

                tool_name = str(decision.get("tool_name") or "")
                arguments = decision.get("arguments")
                if tool_name not in tool_names:
                    raise ValueError(f"智能体选择了未提供的MCP工具：{tool_name}")
                if not isinstance(arguments, dict):
                    raise ValueError("MCP工具参数必须是JSON对象")

                tool_result = mcp_client.call_tool(tool_name, arguments)
                trace = {
                    "call_index": call_index,
                    "tool": tool_name,
                    "arguments": arguments,
                    "result": tool_result,
                    "reason": str(decision.get("reason") or ""),
                }
                tool_trace.append(trace)

                if tool_name == database_tool:
                    if tool_result.get("success"):
                        return {
                            "action": "executed",
                            "execution": tool_result,
                            "tool_trace": tool_trace,
                            "source": "model_mcp",
                        }
                    # SQL 执行失败：把错误回灌给模型，允许它在剩余额度内自我修正后重试。
                    last_execution = tool_result
                    observations.append({
                        "tool": tool_name,
                        "result": tool_result,
                        "correction_hint": (
                            "查询执行失败（success=false）。请阅读 result.error，"
                            "修正 SQL（表名/字段名/语法/日期范围）后重新调用查询工具。"
                        ),
                    })
                    continue

                observations.append({"tool": tool_name, "result": tool_result})

            # 重试额度用尽：若已有执行结果则返回最后一次（失败）结果，交由下游展示错误；
            # 否则说明模型始终未调用查询工具。
            if last_execution is not None:
                return {
                    "action": "executed",
                    "execution": last_execution,
                    "tool_trace": tool_trace,
                    "source": "model_mcp_retry_exhausted",
                }
            raise ValueError(f"MCP工具调用超过上限：{self.max_tool_calls}")
        except PipelineStageError:
            raise
        except (RuntimeError, KeyError, TypeError, ValueError) as exc:
            raise PipelineStageError("single_database_agent", str(exc)) from exc
