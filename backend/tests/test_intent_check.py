import unittest
from types import SimpleNamespace

from app.querying.intent_check import IntentChecker
from app.querying.models import SqlExecution
from app.workflows.query_graph import QueryWorkflow


class IntentCheckCase(unittest.TestCase):
    def setUp(self) -> None:
        self.checker = IntentChecker()

    def check(self, extraction: dict, sql: str, columns: list[str] | None = None):
        return self.checker.check(extraction, sql, columns)

    # ---------------------------------------------------------------- 指标口径

    def test_metric_uses_wrong_amount_column(self) -> None:
        # "销售额"在 Schema 里映射到 paid_amount，SQL 却用了 order_amount。
        result = self.check(
            {"metrics": ["销售额"]},
            "SELECT SUM(order_amount) AS 销售额 FROM orders_current",
            ["销售额"],
        )
        self.assertTrue(result.checked)
        self.assertFalse(result.valid)
        self.assertIn("销售额", result.reason)

    def test_metric_matches_paid_amount(self) -> None:
        result = self.check(
            {"metrics": ["销售额"]},
            "SELECT SUM(paid_amount) AS 销售额 FROM orders_current",
            ["销售额"],
        )
        self.assertTrue(result.valid, result.reason)

    def test_metric_alias_matches_label(self) -> None:
        result = self.check(
            {"metrics": ["实付金额"]},
            'SELECT SUM("paid_amount") FROM orders_current',
            ["sum(paid_amount)"],
        )
        self.assertTrue(result.valid, result.reason)

    def test_count_star_satisfies_order_count(self) -> None:
        # "订单数"基于主键计数，COUNT(*) 在订单粒度上等价，不应误伤。
        result = self.check(
            {"metrics": ["订单数"]},
            "SELECT COUNT(*) AS 订单数 FROM orders_current",
            ["订单数"],
        )
        self.assertTrue(result.valid, result.reason)

    def test_unknown_metric_does_not_trigger(self) -> None:
        # 指标映射不到任何字段时，视为抽取噪声，保持沉默。
        result = self.check(
            {"metrics": ["某个不存在的业务指标"]},
            "SELECT SUM(paid_amount) FROM orders_current",
            ["销售额"],
        )
        self.assertTrue(result.valid, result.reason)

    # ---------------------------------------------------------------- 分组维度

    def test_dimension_without_group_by(self) -> None:
        result = self.check(
            {"dimensions": ["地区"]},
            "SELECT SUM(paid_amount) FROM orders_current",
            ["销售额"],
        )
        self.assertFalse(result.valid)
        self.assertIn("GROUP BY", result.reason)

    def test_dimension_with_group_by(self) -> None:
        result = self.check(
            {"dimensions": ["地区"]},
            "SELECT region, SUM(paid_amount) FROM orders_current GROUP BY region",
            ["region", "销售额"],
        )
        self.assertTrue(result.valid, result.reason)

    def test_total_dimension_needs_no_group_by(self) -> None:
        # "全部地区"是整体口径，缺 GROUP BY 是正常的。
        result = self.check(
            {"dimensions": ["全部地区"]},
            "SELECT SUM(paid_amount) FROM orders_current",
            ["销售额"],
        )
        self.assertTrue(result.valid, result.reason)

    def test_non_aggregate_query_needs_no_group_by(self) -> None:
        result = self.check(
            {"dimensions": ["地区"]},
            "SELECT region, paid_amount FROM orders_current",
            ["region", "paid_amount"],
        )
        self.assertTrue(result.valid, result.reason)

    # ---------------------------------------------------------------- 操作语义

    def test_ranking_without_order_by(self) -> None:
        result = self.check(
            {"operations": ["排名前10"]},
            "SELECT region, SUM(paid_amount) FROM orders_current GROUP BY region LIMIT 10",
            ["region", "销售额"],
        )
        self.assertFalse(result.valid)
        self.assertIn("ORDER BY", result.reason)

    def test_ranking_with_order_by(self) -> None:
        result = self.check(
            {"operations": ["排名前10"]},
            "SELECT region, SUM(paid_amount) FROM orders_current "
            "GROUP BY region ORDER BY 2 DESC LIMIT 10",
            ["region", "销售额"],
        )
        self.assertTrue(result.valid, result.reason)

    def test_detail_request_got_aggregate(self) -> None:
        result = self.check(
            {"operations": ["订单明细"]},
            "SELECT COUNT(*) FROM orders_current",
            ["订单数"],
        )
        self.assertFalse(result.valid)
        self.assertIn("逐条", result.reason)

    # ---------------------------------------------------------------- 时间窗口

    def test_missing_time_filter_without_where(self) -> None:
        result = self.check(
            {"time_expressions": ["2026年8月"]},
            "SELECT SUM(paid_amount) FROM orders_current",
            ["销售额"],
        )
        self.assertFalse(result.valid)
        self.assertIn("2026", result.reason)

    def test_time_filter_present(self) -> None:
        result = self.check(
            {"time_expressions": ["2026年8月"]},
            "SELECT SUM(paid_amount) FROM orders_current "
            "WHERE order_date >= '2026-08-01'",
            ["销售额"],
        )
        self.assertTrue(result.valid, result.reason)

    def test_where_without_time_is_still_a_problem(self) -> None:
        result = self.check(
            {"time_expressions": ["2026年8月"]},
            "SELECT SUM(paid_amount) FROM orders_current WHERE status = 'paid'",
            ["销售额"],
        )
        self.assertFalse(result.valid)
        self.assertIn("时间字段", result.reason)

    def test_relative_time_expression_is_ignored(self) -> None:
        # "最近30天"没有可核对的字面年份，不做判断。
        result = self.check(
            {"time_expressions": ["最近30天"]},
            "SELECT SUM(paid_amount) FROM orders_current",
            ["销售额"],
        )
        self.assertTrue(result.valid, result.reason)

    # ---------------------------------------------------------------- 兜底行为

    def test_empty_extraction_is_not_checked(self) -> None:
        result = self.check({}, "SELECT 1", ["x"])
        self.assertFalse(result.checked)
        self.assertTrue(result.valid)

    def test_filter_without_where_only_records_note(self) -> None:
        result = self.check(
            {"filters": ["华东"]},
            "SELECT region, SUM(paid_amount) FROM orders_current GROUP BY region",
            ["region", "销售额"],
        )
        self.assertTrue(result.valid, "过滤条件缺失不应单独判负")
        self.assertTrue(result.notes)


class WorkflowGateCase(unittest.TestCase):
    """验证规则校验只作为第二道闸门，不会单独把可用结果判成失败。"""

    def make_workflow(self, enabled: bool = True) -> QueryWorkflow:
        workflow = object.__new__(QueryWorkflow)
        workflow.config = SimpleNamespace(
            intent_check_enabled=enabled,
            validation_max_retries=1,
        )
        workflow.intent_checker = IntentChecker()
        return workflow

    @staticmethod
    def state(extraction: dict) -> dict:
        return {"extraction": extraction, "standalone_query": "本月各地区销售额"}

    def test_model_valid_but_intent_mismatch_opens_rewrite(self) -> None:
        workflow = self.make_workflow()
        execution = SqlExecution(
            sql="SELECT SUM(order_amount) FROM orders_current",
            success=True,
            columns=["销售额"],
        )
        model_final = {"valid": True, "retryable": False, "reason": "", "analysis": "查询完成"}
        log: list[dict] = []
        final = workflow._apply_intent_check(
            self.state({"metrics": ["销售额"]}), execution, model_final, log, attempt=1,
        )
        self.assertFalse(final["valid"])
        self.assertTrue(final["retryable"])
        self.assertTrue(final["intent_check"]["model_valid"])
        self.assertEqual(log[-1]["stage"], "intent_check")
        # 触发已有的重写循环，而不是另起一条链路。
        self.assertTrue(workflow._should_rewrite(final, 0))

    def test_model_invalid_is_not_overwritten(self) -> None:
        workflow = self.make_workflow()
        execution = SqlExecution(sql="SELECT 1", success=True, columns=["x"])
        model_final = {"valid": False, "retryable": False, "reason": "模型认为口径错误"}
        final = workflow._apply_intent_check(
            self.state({"metrics": ["销售额"]}), execution, model_final, [], attempt=1,
        )
        self.assertEqual(final["reason"], "模型认为口径错误")
        self.assertNotIn("intent_check", final)

    def test_disabled_switch_keeps_model_verdict(self) -> None:
        workflow = self.make_workflow(enabled=False)
        execution = SqlExecution(
            sql="SELECT SUM(order_amount) FROM orders_current", success=True, columns=["销售额"],
        )
        final = workflow._apply_intent_check(
            self.state({"metrics": ["销售额"]}), execution,
            {"valid": True, "retryable": False}, [], attempt=1,
        )
        self.assertTrue(final["valid"])

    def test_settle_restores_usable_result_with_note(self) -> None:
        final = {
            "valid": False,
            "retryable": True,
            "reason": "指标缺失",
            "analysis": "本月销售额为 100 万",
            "title": "查询结果",
            "intent_check": {"model_valid": True, "problems": ["指标缺失"]},
        }
        settled = QueryWorkflow._settle_intent_check(final)
        self.assertTrue(settled["valid"])
        self.assertIn("本月销售额为 100 万", settled["analysis"])
        self.assertIn("注意", settled["analysis"])
        self.assertEqual(settled["title"], "查询结果")
        self.assertTrue(settled["intent_check"]["recovered"])

    def test_settle_leaves_model_failure_alone(self) -> None:
        # 模型自己判负的结果（没有 intent_check 标记）不应被恢复。
        final = {"valid": False, "retryable": False, "reason": "模型认为口径错误"}
        settled = QueryWorkflow._settle_intent_check(final)
        self.assertFalse(settled["valid"])

    def test_clean_result_passes_through(self) -> None:
        workflow = self.make_workflow()
        execution = SqlExecution(
            sql="SELECT region, SUM(paid_amount) FROM orders_current "
                "WHERE order_date >= '2026-08-01' GROUP BY region ORDER BY 2 DESC",
            success=True,
            columns=["region", "销售额"],
        )
        final = {"valid": True, "retryable": False, "analysis": "完成"}
        out = workflow._apply_intent_check(
            self.state({"metrics": ["销售额"], "dimensions": ["地区"], "time_expressions": ["2026年8月"]}),
            execution, final, [], attempt=1,
        )
        self.assertTrue(out["valid"], out.get("reason"))
        self.assertEqual(QueryWorkflow._settle_intent_check(out)["analysis"], "完成")


class _StubResponseGenerator:
    def __init__(self) -> None:
        self.calls = 0

    def finalize(self, query, execution, schema_context, analysis_context=""):
        self.calls += 1
        return {"valid": True, "retryable": False, "reason": "", "title": "查询结果", "analysis": "完成"}


class _StubAgent:
    """按顺序吐出预设的 SQL 执行结果，并记录每次重写收到的问题说明。"""

    def __init__(self, executions: list[dict]) -> None:
        self.executions = list(executions)
        self.corrections: list[dict | None] = []

    def prepare(self, *args, correction=None, **kwargs):
        self.corrections.append(correction)
        return {
            "action": "executed",
            "execution": self.executions.pop(0),
            "tool_trace": [],
            "source": "model",
        }


class ExecutionWiringCase(unittest.TestCase):
    """验证规则校验确实接进了已有的重写循环。"""

    def make_workflow(self, executions: list[dict]) -> tuple[QueryWorkflow, _StubAgent]:
        workflow = object.__new__(QueryWorkflow)
        workflow.config = SimpleNamespace(
            intent_check_enabled=True,
            validation_max_retries=1,
            context_table_row_limit=50,
        )
        workflow.intent_checker = IntentChecker()
        workflow.response_generator = _StubResponseGenerator()
        agent = _StubAgent(executions)
        workflow.single_database_agent = agent
        return workflow, agent

    @staticmethod
    def base_state(sql: str) -> dict:
        return {
            "task_id": "t1",
            "query": "本月各地区销售额",
            "standalone_query": "本月各地区销售额",
            "database_names": ["askdata_mock"],
            "schema_graph": {"tables": [], "fields": [], "joins": [], "graph_version": "g1"},
            "schema_context": "",
            "retrieval": {},
            "workspace": {},
            "access_scope": {},
            "extraction": {"metrics": ["销售额"]},
            "intent": {"reason": "问数"},
            "mcp_execution": {
                "sql": sql, "success": True, "columns": ["销售额"], "rows": [{"销售额": 1}],
            },
            "mcp_tool_trace": [],
        }

    def test_wrong_column_triggers_one_rewrite(self) -> None:
        workflow, agent = self.make_workflow([{
            "sql": "SELECT SUM(paid_amount) AS 销售额 FROM orders_current",
            "success": True,
            "columns": ["销售额"],
            "rows": [{"销售额": 100}],
        }])
        state = self.base_state("SELECT SUM(order_amount) AS 销售额 FROM orders_current")
        updates = workflow._execute_single_database(state)

        self.assertEqual(len(agent.corrections), 1)
        correction = agent.corrections[0]
        self.assertIsNotNone(correction)
        self.assertIn("销售额", correction["validation_reason"])
        self.assertEqual(updates["validation_retries"], 1)
        self.assertEqual(updates["result"]["status"], "completed")
        self.assertIn("paid_amount", updates["result"]["sql"])
        self.assertEqual(updates["sql_source"], "model_mcp_validation_retry")
        stages = [item["stage"] for item in updates["execution_log"]]
        self.assertIn("intent_check", stages)

    def test_still_wrong_after_rewrite_keeps_rows(self) -> None:
        # 重写后规则仍判负：恢复为成功，把疑点写进说明，不能吞掉已查出的数据。
        workflow, agent = self.make_workflow([{
            "sql": "SELECT SUM(order_amount) AS 销售额 FROM orders_current",
            "success": True,
            "columns": ["销售额"],
            "rows": [{"销售额": 100}],
        }])
        state = self.base_state("SELECT SUM(order_amount) AS 销售额 FROM orders_current")
        updates = workflow._execute_single_database(state)

        self.assertEqual(updates["validation_retries"], 1)
        self.assertEqual(updates["result"]["status"], "completed")
        self.assertEqual(updates["result"]["rows"], [{"销售额": 100}])
        self.assertIn("注意", updates["result"]["analysis"])

    def test_correct_sql_needs_no_rewrite(self) -> None:
        workflow, agent = self.make_workflow([])
        state = self.base_state(
            "SELECT SUM(paid_amount) AS 销售额 FROM orders_current WHERE order_date >= '2026-08-01'"
        )
        state["extraction"] = {"metrics": ["销售额"], "time_expressions": ["2026年8月"]}
        updates = workflow._execute_single_database(state)

        self.assertEqual(agent.corrections, [])
        self.assertEqual(updates["validation_retries"], 0)
        self.assertEqual(updates["result"]["status"], "completed")


if __name__ == "__main__":
    unittest.main()