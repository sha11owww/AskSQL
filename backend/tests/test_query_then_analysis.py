import unittest
from types import SimpleNamespace

from app.errors import PipelineStageError
from app.querying.intent_check import IntentChecker
from app.workflows.query_graph import QueryWorkflow


class _StubResponseGenerator:
    def finalize(self, query, execution, schema_context, analysis_context=""):
        return {"valid": True, "retryable": False, "reason": "", "title": "查询结果", "analysis": "查询说明"}


class _StubSingleDatabaseAgent:
    def prepare(self, *args, **kwargs):
        raise AssertionError("本组用例不应触发 SQL 重写")


class _StubQaAgent:
    def __init__(self, answer: str = "分析结论", error: str | None = None) -> None:
        self.answer = answer
        self.error = error
        self.calls: list[dict] = []

    def run(self, query, contexts, access_scope):
        self.calls.append({"query": query, "contexts": contexts})
        if self.error:
            raise PipelineStageError("qa_answer", self.error)
        return SimpleNamespace(answer=self.answer, report=None, tool_calls=[])


def _workflow(qa_agent: _StubQaAgent) -> QueryWorkflow:
    workflow = object.__new__(QueryWorkflow)
    workflow.config = SimpleNamespace(
        intent_check_enabled=False,
        validation_max_retries=0,
        context_table_row_limit=50,
    )
    workflow.intent_checker = IntentChecker()
    workflow.response_generator = _StubResponseGenerator()
    workflow.single_database_agent = _StubSingleDatabaseAgent()
    workflow.data_qa_agent = qa_agent
    return workflow


class QueryThenAnalysisCase(unittest.TestCase):
    """覆盖复合请求串联分析时踩过的三个坑。"""

    @staticmethod
    def executed_state(needs_analysis: bool = True) -> dict:
        return {
            "task_id": "t1",
            "query": "查本月各地区销售额并分析趋势",
            "standalone_query": "查询本月各地区销售额",
            "database_names": ["askdata_mock"],
            "schema_graph": {"tables": [], "fields": [], "joins": [], "graph_version": "g1"},
            "schema_context": "",
            "retrieval": {},
            "workspace": {},
            "access_scope": {},
            "extraction": {},
            "intent": {"reason": "复合请求"},
            "needs_analysis": needs_analysis,
            "analysis_goal": "分析各地区销售额差异",
            "recent_result_context": '{"title":"上一轮的旧结果"}',
            "mcp_execution": {
                "sql": "SELECT region, SUM(paid_amount) FROM orders_current GROUP BY region",
                "success": True,
                "columns": ["地区", "销售额"],
                "rows": [{"地区": "华东", "销售额": 100}, {"地区": "华南", "销售额": 60}],
            },
            "mcp_tool_trace": [],
        }

    # ---------------------------------------------------------------- 坑 1

    def test_this_turn_result_overwrites_previous_context(self) -> None:
        workflow = _workflow(_StubQaAgent())
        updates = workflow._execute_single_database(self.executed_state())

        context = updates["recent_result_context"]
        self.assertNotIn("上一轮的旧结果", context)
        self.assertIn("华东", context)
        self.assertIn("地区", context)

    def test_query_result_snapshot_is_saved_for_merging(self) -> None:
        workflow = _workflow(_StubQaAgent())
        updates = workflow._execute_single_database(self.executed_state())

        snapshot = updates["query_result"]
        self.assertEqual(snapshot["route"], "database_query")
        self.assertEqual(len(snapshot["rows"]), 2)

    def test_context_not_touched_without_analysis(self) -> None:
        # 非复合请求不应改写上下文快照。
        workflow = _workflow(_StubQaAgent())
        updates = workflow._execute_single_database(self.executed_state(needs_analysis=False))

        self.assertNotIn("recent_result_context", updates)
        self.assertNotIn("query_result", updates)

    # ---------------------------------------------------------------- 坑 2

    def test_merged_result_keeps_table_and_route(self) -> None:
        workflow = _workflow(_StubQaAgent(answer="华东领先，华南次之"))
        state = {
            **self.executed_state(),
            "query_result": {
                "task_id": "t1",
                "status": "completed",
                "route": "database_query",
                "message": "查询完成",
                "columns": ["地区", "销售额"],
                "rows": [{"地区": "华东", "销售额": 100}, {"地区": "华南", "销售额": 60}],
                "sql": "SELECT region, SUM(paid_amount) FROM orders_current GROUP BY region",
                "analysis": "查询说明",
                "steps": ["一次预处理"],
                "route_reason": "复合请求",
            },
            "short_term_context": "",
            "recent_result_context": "{}",
            "analysis_context": "",
            "analysis_sources": [],
        }
        updates = workflow._answer_qa(state)
        result = updates["result"]

        # 表格必须还在，route 不能被改成 data_qa，否则前端不渲染表格卡片。
        self.assertEqual(result["route"], "database_query")
        self.assertEqual(result["columns"], ["地区", "销售额"])
        self.assertEqual(len(result["rows"]), 2)
        self.assertEqual(result["analysis"], "华东领先，华南次之")
        self.assertEqual(updates["workflow_mode"], "query_then_analysis")

    def test_analysis_receives_this_turn_result(self) -> None:
        qa = _StubQaAgent()
        workflow = _workflow(qa)
        state = {
            **self.executed_state(),
            "query_result": {"task_id": "t1", "status": "completed", "route": "database_query",
                             "message": "完成", "columns": ["地区"], "rows": [{"地区": "华东"}],
                             "steps": [], "route_reason": "x"},
            "short_term_context": "",
            "recent_result_context": '{"title":"本轮结果"}',
            "analysis_context": "",
            "analysis_sources": [],
        }
        workflow._answer_qa(state)

        self.assertEqual(qa.calls[0]["query"], "分析各地区销售额差异")
        self.assertIn("本轮结果", qa.calls[0]["contexts"]["recent_result"])

    # ---------------------------------------------------------------- 坑 3

    def test_analysis_failure_keeps_query_result(self) -> None:
        workflow = _workflow(_StubQaAgent(error="模型超时"))
        state = {
            **self.executed_state(),
            "query_result": {
                "task_id": "t1",
                "status": "completed",
                "route": "database_query",
                "message": "查询完成",
                "columns": ["地区", "销售额"],
                "rows": [{"地区": "华东", "销售额": 100}],
                "sql": "SELECT ...",
                "analysis": "查询说明",
                "steps": [],
                "route_reason": "复合请求",
            },
            "short_term_context": "",
            "recent_result_context": "{}",
            "analysis_context": "",
            "analysis_sources": [],
        }
        updates = workflow._answer_qa(state)
        result = updates["result"]

        self.assertEqual(updates["workflow_mode"], "query_then_analysis_failed")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["rows"], [{"地区": "华东", "销售额": 100}])
        self.assertEqual(result["sql"], "SELECT ...")
        self.assertIn("分析环节失败", result["analysis"])

    def test_standalone_qa_failure_still_raises(self) -> None:
        # 没有查询结果兜底时，问答失败应正常抛出，不能被静默吞掉。
        workflow = _workflow(_StubQaAgent(error="模型超时"))
        state = {
            "task_id": "t3",
            "query": "分析这个表格",
            "intent": {"reason": "数据问答"},
            "short_term_context": "",
            "recent_result_context": "{}",
            "analysis_context": "",
            "analysis_sources": [],
        }
        with self.assertRaises(PipelineStageError):
            workflow._answer_qa(state)

    # ---------------------------------------------------------------- 路由

    def test_empty_result_skips_analysis(self) -> None:
        # 零行结果不应进入分析，避免对空数据编造结论。
        self.assertEqual(
            QueryWorkflow._after_execution(
                {"needs_analysis": True, "result": {"status": "completed", "rows": []}}
            ),
            "end",
        )

    def test_failed_query_skips_analysis(self) -> None:
        self.assertEqual(
            QueryWorkflow._after_execution(
                {"needs_analysis": True, "result": {"status": "failed", "rows": [{"a": 1}]}}
            ),
            "end",
        )

    def test_completed_query_enters_analysis(self) -> None:
        self.assertEqual(
            QueryWorkflow._after_execution(
                {"needs_analysis": True, "result": {"status": "completed", "rows": [{"a": 1}]}}
            ),
            "answer_qa",
        )


if __name__ == "__main__":
    unittest.main()
