"""手动运行：使用当前 .env 完成一次端到端查询。"""

from app.database import initialize_database
from app.service import AskSQLService


if __name__ == "__main__":
    initialize_database()
    service = AskSQLService()
    result = service.submit(
        "查询本月各地区销售额",
        "smoke-session",
        {"tables": ["orders_current"]},
    )
    print(
        {
            "status": result.status,
            "route": result.route,
            "coder_calls": len(result.tool_calls),
            "sql_executed": bool(result.sql),
            "row_count": len(result.rows),
            "schema_threshold": (result.retrieval or {}).get("threshold"),
            "embedding_source": (result.retrieval or {}).get("embedding_source"),
            "rerank_source": (result.retrieval or {}).get("rerank_source"),
        }
    )
