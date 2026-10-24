from __future__ import annotations

import json
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from ..config import Settings, settings
from ..database import SCHEMA
from ..errors import PipelineStageError
from ..mcp_runtime import LocalMcpClient, create_local_mcp_server
from ..model_client import ModelClient
from ..models import QueryResult
from ..preprocessing import RequestPreprocessor
from ..querying.duckdb_engine import DuckDbEngine
from ..querying.data_qa_agent import DataQaAgent
from ..querying.intent_check import IntentChecker
from ..querying.models import SqlExecution
from ..querying.response_generator import ResponseGenerator
from ..querying.single_database_agent import SingleDatabaseAgent
from ..retrieval import SchemaGraphBuilder, SchemaIndex
from ..security import AccessScope
from ..skills import SkillRegistry
from .result_builder import ResultBuilder
from .state import QueryState


class QueryWorkflow:
    """基于 LangGraph 的问答和数据库查询工作流。"""

    def __init__(
        self,
        model_client: ModelClient,
        schema_index: SchemaIndex,
        config: Settings | None = None,
    ) -> None:
        self.model_client = model_client
        self.schema_index = schema_index
        self.config = config or settings
        self.preprocessor = RequestPreprocessor(model_client)
        self.skills = SkillRegistry()
        self.graph_builder = SchemaGraphBuilder()
        self.database_engine = DuckDbEngine()
        self.single_database_agent = SingleDatabaseAgent(
            model_client,
            self.mcp_client,
            self.skills.get("database_query"),
            self.config.mcp_max_tool_calls,
        )
        self.response_generator = ResponseGenerator(
            model_client,
            self.config,
        )
        self.intent_checker = IntentChecker()
        self.data_qa_agent = DataQaAgent(
            model_client,
            self.mcp_client,
            self.skills.get("data_qa"),
        )
        self.checkpointer = InMemorySaver()
        self.graph = self._compile()

    def mcp_client(self, access_scope: dict[str, Any]) -> LocalMcpClient:
        scope = AccessScope.from_dict(access_scope)
        return LocalMcpClient(create_local_mcp_server(self.database_engine, scope))

    def _compile(self):
        builder = StateGraph(QueryState)
        builder.add_node("preprocess", self._preprocess)
        builder.add_node("respond_directly", self._respond_directly)
        builder.add_node("answer_qa", self._answer_qa)
        builder.add_node("retrieve_schema", self._retrieve_schema)
        builder.add_node("human_clarification", self._human_clarification)
        builder.add_node("prepare_single_database", self._prepare_single_database)
        builder.add_node("execute_single_database", self._execute_single_database)
        builder.add_node("run_multi_database", self._run_multi_database)
        builder.add_edge(START, "preprocess")
        builder.add_conditional_edges(
            "preprocess",
            lambda state: {
                "direct_response": "respond_directly",
                "data_qa": "answer_qa",
                "database_query": "retrieve_schema",
            }[state["intent"]["action"]],
            {
                "respond_directly": "respond_directly",
                "answer_qa": "answer_qa",
                "retrieve_schema": "retrieve_schema",
            },
        )
        builder.add_edge("respond_directly", END)
        builder.add_edge("answer_qa", END)
        builder.add_conditional_edges(
            "retrieve_schema",
            self._after_retrieval,
            {
                "human_clarification": "human_clarification",
                "prepare_single_database": "prepare_single_database",
                "run_multi_database": "run_multi_database",
            },
        )
        builder.add_edge("human_clarification", "retrieve_schema")
        builder.add_conditional_edges(
            "prepare_single_database",
            lambda state: "human_clarification" if state.get("clarification") else "execute_single_database",
            {
                "human_clarification": "human_clarification",
                "execute_single_database": "execute_single_database",
            },
        )
        builder.add_conditional_edges(
            "execute_single_database",
            self._after_execution,
            {"answer_qa": "answer_qa", "end": END},
        )
        builder.add_conditional_edges(
            "run_multi_database",
            lambda state: "human_clarification" if state.get("clarification") else "end",
            {"human_clarification": "human_clarification", "end": END},
        )
        return builder.compile(checkpointer=self.checkpointer)

    @staticmethod
    def run_config(task_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": task_id}}

    def invoke(self, payload: QueryState | Command, task_id: str) -> QueryResult:
        state = self.graph.invoke(payload, config=self.run_config(task_id))
        return self._state_result(state, task_id)

    def _state_result(self, state: dict[str, Any], task_id: str) -> QueryResult:
        if state.get("result"):
            return QueryResult.model_validate(state["result"])
        state = {**state, "task_id": state.get("task_id") or task_id}
        return ResultBuilder.waiting(state)

    def _preprocess(self, state: QueryState) -> dict[str, Any]:
        """生成路由、独立查询和 Schema 检索参数。"""
        decision = self.preprocessor.prepare(state["query"], state.get("route_context", ""))
        execution_log = list(state.get("execution_log") or [])
        if decision.source == "model_unavailable_fallback":
            execution_log.append({
                "stage": "route_fallback",
                "success": True,
                "source": decision.source,
                "action": decision.action,
                "reason": decision.reason,
            })
        return {
            "intent": {
                "action": decision.action,
                "confidence": decision.confidence,
                "reason": decision.reason,
                "response_type": decision.response_type,
                "source": decision.source,
            },
            "direct_response": decision.response,
            "standalone_query": decision.standalone_query,
            "rewritten": decision.rewritten,
            "needs_analysis": decision.needs_analysis,
            "analysis_goal": decision.analysis_goal,
            "extraction": decision.retrieval.public(),
            "execution_log": execution_log,
        }

    def _respond_directly(self, state: QueryState) -> dict[str, Any]:
        """返回预处理模型生成的普通回答或自然语言澄清。"""
        result = ResultBuilder.direct_response(
            state["task_id"],
            state.get("direct_response", ""),
            state["intent"],
            list(state.get("execution_log") or []),
        )
        return {
            "workflow_mode": result.workflow_mode,
            "result": result.model_dump(mode="json"),
        }

    def _answer_qa(self, state: QueryState) -> dict[str, Any]:
        chained = state.get("query_result") or {}
        analysis_query = state["query"]
        if chained:
            analysis_query = state.get("analysis_goal") or state["query"]
        try:
            qa_result = self.data_qa_agent.run(
                analysis_query,
                {
                    "short_term": state.get("short_term_context", ""),
                    "recent_result": state.get("recent_result_context", ""),
                    "selected_tables": state.get("analysis_context", ""),
                },
                state.get("access_scope") or {},
            )
        except PipelineStageError as exc:
            # 分析是增值环节：失败时保留已成功的查询结果，不让整轮请求失败。
            if not chained:
                raise
            log = [
                *(state.get("execution_log") or []),
                {"stage": exc.stage, "success": False, "error": exc.message},
            ]
            degraded = dict(chained)
            degraded["analysis"] = (
                f"{chained.get('analysis') or ''}（分析环节失败：{exc.message}）"
            ).strip()
            degraded["workflow_mode"] = "query_then_analysis_failed"
            return {
                "execution_log": log,
                "workflow_mode": "query_then_analysis_failed",
                "result": degraded,
            }

        if chained:
            result = ResultBuilder.qa_followup(
                state["task_id"],
                qa_result,
                state["intent"],
                state.get("analysis_sources") or [],
                chained,
            )
        else:
            result = ResultBuilder.qa(
                state["task_id"], qa_result, state["intent"], state.get("analysis_sources") or []
            )
        return {
            "workflow_mode": result.workflow_mode or ("qa_report" if qa_result.report else "qa"),
            "result": result.model_dump(mode="json"),
        }

    def _retrieve_schema(self, state: QueryState) -> dict[str, Any]:
        standalone_query = state["standalone_query"]
        extraction = state.get("extraction") or {}
        retrieval = self.schema_index.retrieve(
            standalone_query,
            retrieval_terms=list(extraction.get("retrieval_terms") or []),
            access_scope=state.get("access_scope"),
        )
        workspace = state.get("workspace") or {}
        query_workspace = {
            "schema_fields": list(workspace.get("schema_fields") or []),
            "confirmed_schema_tables": list(workspace.get("confirmed_schema_tables") or []),
            "confirmed_parameters": dict(workspace.get("confirmed_parameters") or {}),
        }
        retrieval = self.schema_index.include_workspace(
            retrieval,
            query_workspace,
            state.get("access_scope"),
        )
        retrieval["extraction"] = extraction
        schema_graph = self.graph_builder.build(
            retrieval["hits"],
            state.get("access_scope"),
        )
        retrieval["schema_graph"] = schema_graph
        databases = sorted({
            str(table.get("database") or schema_graph.get("database") or "askdata_mock")
            for table in schema_graph.get("tables", [])
        })
        return {
            "standalone_query": standalone_query,
            "extraction": extraction,
            "retrieval": retrieval,
            "schema_graph": schema_graph,
            "schema_context": self.graph_builder.context_text(schema_graph),
            "database_names": databases,
            "clarification": None,
            "direct_sql": "",
        }

    @staticmethod
    def _after_retrieval(state: QueryState) -> str:
        if state.get("clarification"):
            return "human_clarification"
        return "prepare_single_database" if len(state.get("database_names") or []) <= 1 else "run_multi_database"

    @staticmethod
    def _after_execution(state: QueryState) -> str:
        """复合请求在查询成功且有数据时继续进入分析节点，其余情况直接结束。"""
        result = state.get("result") or {}
        if (
            state.get("needs_analysis")
            and result.get("status") == "completed"
            and result.get("rows")
        ):
            return "answer_qa"
        return "end"

    def _human_clarification(self, state: QueryState) -> dict[str, Any]:
        # LangGraph 将 Command(resume=...) 的值作为 interrupt 返回值。
        response = interrupt(state.get("clarification") or {})
        option_id = str(response.get("option_id") if isinstance(response, dict) else response)
        workspace = dict(state.get("workspace") or {})
        payload = state.get("clarification") or {}
        parameter = str(payload.get("parameter") or "other")
        tables = list(workspace.get("confirmed_schema_tables") or [])
        if any(table["id"] == option_id for table in SCHEMA):
            tables = list(dict.fromkeys([*tables, option_id]))
        workspace["confirmed_schema_tables"] = tables
        workspace["confirmed_parameters"] = {
            **dict(workspace.get("confirmed_parameters") or {}),
            parameter: option_id,
        }
        return {"workspace": workspace, "clarification": None, "direct_sql": "", "result": {}}

    def _prepare_single_database(self, state: QueryState) -> dict[str, Any]:
        workspace = dict(state.get("workspace") or {})
        database = (state.get("database_names") or ["askdata_mock"])[0]
        decision = self.single_database_agent.prepare(
            state["standalone_query"],
            database,
            state["schema_graph"],
            state["schema_context"],
            state["retrieval"],
            workspace,
            state.get("access_scope") or {},
        )
        if decision["action"] == "clarify":
            return {
                "workflow_mode": "single_database_agent",
                "clarification": decision["clarification"],
                "mcp_tool_trace": decision.get("tool_trace", []),
                "direct_sql": "",
            }
        execution = decision["execution"]
        return {
            "workflow_mode": "single_database_agent",
            "clarification": None,
            "mcp_execution": execution,
            "mcp_tool_trace": decision.get("tool_trace", []),
            "direct_sql": str(execution.get("sql") or ""),
            "sql_source": decision.get("source", "model"),
        }

    def _execute_single_database(self, state: QueryState) -> dict[str, Any]:
        database = (state.get("database_names") or ["askdata_mock"])[0]
        execution = self._to_execution(state.get("mcp_execution") or {}, state)
        trace = list(state.get("mcp_tool_trace") or [])
        merged_trace = list(trace)
        log = [
            {
                "stage": "mcp_tool_call",
                "success": not bool(item.get("result", {}).get("error")),
                **item,
            }
            for item in trace
        ]
        log.append({
            "stage": "execute_duckdb",
            "success": execution.success,
            "error": execution.error,
            "via": "mcp",
        })
        calls = [self._query_call(state, execution, database, trace, attempt=1)]

        if not execution.success:
            result = ResultBuilder.failed(state, execution, log)
            return {
                "execution_log": log,
                "tool_calls": calls,
                "validation_retries": 0,
                "result": result.model_dump(mode="json"),
            }

        final = self._finalize_result(state, execution, log)
        final = self._apply_intent_check(state, execution, final, log, attempt=1)
        rewrites = 0
        attempt = 0
        last_raw_execution: dict[str, Any] | None = None
        while self._should_rewrite(final, attempt):
            attempt += 1
            log.append({
                "stage": "result_validation",
                "success": False,
                "attempt": attempt,
                "reason": final.get("reason"),
                "rewriting": True,
            })
            try:
                decision = self.single_database_agent.prepare(
                    state["standalone_query"],
                    database,
                    state["schema_graph"],
                    state["schema_context"],
                    state["retrieval"],
                    dict(state.get("workspace") or {}),
                    state.get("access_scope") or {},
                    correction=self._correction_payload(execution, final),
                )
            except PipelineStageError as exc:
                log.append({
                    "stage": "validation_rewrite",
                    "success": False,
                    "attempt": attempt,
                    "error": exc.message,
                })
                break

            if decision.get("action") != "executed":
                # 重写阶段模型认为需要澄清：停止重试，保留已有结果并转达澄清问题。
                clarification = decision.get("clarification") or {}
                question = str(clarification.get("question") or "").strip()
                if question:
                    final = {
                        **final,
                        "analysis": (
                            f"{final.get('analysis') or ''} 重写需要补充信息：{question}"
                        ).strip(),
                    }
                log.append({
                    "stage": "validation_rewrite",
                    "success": False,
                    "attempt": attempt,
                    "error": "重写阶段请求澄清，已停止重试",
                })
                break

            last_raw_execution = decision["execution"]
            execution = self._to_execution(last_raw_execution, state)
            rewrites += 1
            retry_trace = list(decision.get("tool_trace") or [])
            for item in retry_trace:
                log.append({
                    "stage": "mcp_tool_call",
                    "success": not bool(item.get("result", {}).get("error")),
                    "attempt": attempt + 1,
                    **item,
                })
            log.append({
                "stage": "execute_duckdb",
                "success": execution.success,
                "error": execution.error,
                "via": "mcp",
                "attempt": attempt + 1,
            })
            merged_trace.extend(retry_trace)
            calls.append(self._query_call(state, execution, database, retry_trace, attempt=attempt + 1))
            if not execution.success:
                break
            final = self._finalize_result(state, execution, log)
            final = self._apply_intent_check(state, execution, final, log, attempt=attempt + 1)

        final = self._settle_intent_check(final)
        result = ResultBuilder.completed(
            {**state, "validation_retries": rewrites},
            [execution],
            execution,
            final,
            calls,
            log,
        )
        payload = result.model_dump(mode="json")
        updates: dict[str, Any] = {
            "execution_log": log,
            "tool_calls": calls,
            "validation_retries": rewrites,
            "result": payload,
        }
        if last_raw_execution is not None:
            updates["mcp_execution"] = last_raw_execution
            updates["mcp_tool_trace"] = merged_trace
            updates["direct_sql"] = execution.sql
            updates["sql_source"] = "model_mcp_validation_retry"
        if state.get("needs_analysis") and execution.success:
            # 此时 recent_result_context 仍是上一轮结果，必须用本轮执行结果覆盖，
            # 否则分析节点会分析错数据；同时保留查询结果快照供合并时使用。
            updates["query_result"] = payload
            updates["recent_result_context"] = self._result_context(result)
        return updates

    def _finalize_result(
        self,
        state: QueryState,
        execution: SqlExecution,
        log: list[dict[str, Any]],
    ) -> dict[str, Any]:
        try:
            return self.response_generator.finalize(
                state["standalone_query"], execution, state["schema_context"],
                state.get("analysis_context", ""),
            )
        except PipelineStageError as exc:
            # 结果说明失败时仍保留已成功执行的查询结果。
            log.append({"stage": exc.stage, "success": False, "error": exc.message})
            return {
                "valid": True,
                "retryable": False,
                "reason": f"{exc.stage}失败",
                "title": "查询结果（文字说明生成失败）",
                "analysis": f"SQL已成功执行，但{exc.stage}失败：{exc.message}",
            }

    def _apply_intent_check(
        self,
        state: QueryState,
        execution: SqlExecution,
        final: dict[str, Any],
        log: list[dict[str, Any]],
        attempt: int,
    ) -> dict[str, Any]:
        """结果校验的第二道闸门：用预处理抽出的意图结构核对 SQL 实际算的东西。

        只在模型自评通过时介入，作为 AND 条件而不是替代项，避免两套判断互相打架。
        """
        if not self.config.intent_check_enabled or not final.get("valid", True):
            return final
        check = self.intent_checker.check(
            state.get("extraction"),
            execution.sql,
            execution.columns,
        )
        if not check.checked:
            return final
        if check.notes:
            log.append({
                "stage": "intent_check",
                "success": True,
                "attempt": attempt,
                "notes": check.notes,
            })
        if check.valid:
            return final
        log.append({
            "stage": "intent_check",
            "success": False,
            "attempt": attempt,
            "problems": check.problems,
            "rewriting": True,
        })
        return {
            **final,
            "valid": False,
            "retryable": bool(check.retryable),
            "reason": check.reason,
            "intent_check": {"model_valid": True, "problems": check.problems},
        }

    @staticmethod
    def _settle_intent_check(final: dict[str, Any]) -> dict[str, Any]:
        """规则校验只负责触发一次重写，不单独把可用结果判成失败。

        重写之后规则仍然判负，说明要么数据本身不满足，要么规则误伤；两种情况都不该
        吞掉已经查出来的数据，因此恢复模型的原有判断，并把疑点写进查询说明。
        """
        marker = final.get("intent_check") or {}
        if not marker.get("model_valid"):
            return final
        problems = "；".join(marker.get("problems") or [])
        note = f"注意：系统检测到查询口径可能与问题不完全一致（{problems}），以上结果仅供参考。"
        analysis = str(final.get("analysis") or "").strip()
        return {
            **final,
            "valid": True,
            "retryable": False,
            "analysis": f"{analysis} {note}".strip(),
            "intent_check": {**marker, "recovered": True},
        }

    def _should_rewrite(self, final: dict[str, Any], attempts: int) -> bool:
        """只有"不通过且可通过改写SQL修正"的结果才重试。"""
        return (
            not final.get("valid", True)
            and bool(final.get("retryable", False))
            and attempts < max(0, self.config.validation_max_retries)
        )

    @staticmethod
    def _to_execution(raw: dict[str, Any], state: QueryState) -> SqlExecution:
        return SqlExecution(
            sql=str(raw.get("sql") or state.get("direct_sql") or ""),
            success=bool(raw.get("success")),
            columns=list(raw.get("columns") or []),
            rows=list(raw.get("rows") or []),
            error=raw.get("error"),
        )

    @staticmethod
    def _correction_payload(execution: SqlExecution, final: dict[str, Any]) -> dict[str, Any]:
        return {
            "sql": execution.sql,
            "validation_reason": str(final.get("reason") or "结果没有回答用户问题"),
            "columns": execution.columns,
            "row_count": len(execution.rows),
            "sample_rows": execution.rows[:5],
        }

    @staticmethod
    def _query_call(
        state: QueryState,
        execution: SqlExecution,
        database: str,
        trace: list[dict[str, Any]],
        attempt: int,
    ) -> dict[str, Any]:
        database_call = next(
            (item for item in reversed(trace) if item.get("tool") == f"query_{database}"),
            {},
        )
        return {
            "call_index": int(database_call.get("call_index") or 1),
            "attempt": attempt,
            "database": database,
            "arguments": {
                "mode": "single_database_agent",
                "transport": "mcp_in_process",
                "tool_name": database_call.get("tool"),
                "schema_graph_version": state.get("schema_graph", {}).get("graph_version"),
                "sql_source": (
                    state.get("sql_source", "model")
                    if attempt == 1
                    else "model_mcp_validation_retry"
                ),
            },
            "sql": execution.sql,
            "success": execution.success,
            "row_count": len(execution.rows),
            "error": execution.error,
        }

    def _result_context(self, result: QueryResult) -> str:
        """把本次执行结果整理成数据问答可直接消费的上下文。"""
        return json.dumps({
            "task_id": result.task_id,
            "title": result.result_title or "本次查询结果",
            "row_count": len(result.rows),
            "analysis": result.analysis,
            "columns": result.columns,
            "rows": result.rows[: self.config.context_table_row_limit],
            "sql": result.sql,
        }, ensure_ascii=False)

    def _run_multi_database(self, state: QueryState) -> dict[str, Any]:
        """返回尚未实现的多数据库查询结果。"""
        failure = SqlExecution(
            sql="",
            success=False,
            error="当前仅支持单库直接查询；多库 Handoff 尚未启用。",
        )
        result = ResultBuilder.failed(state, failure, [])
        return {"workflow_mode": "multi_database_pending", "result": result.model_dump(mode="json")}
