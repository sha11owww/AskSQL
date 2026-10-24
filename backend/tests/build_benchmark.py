"""构建 120 条分层评测集。

设计要点：
1. 时间基准按数据库分别固定（as_of），因为两个库的数据截止日期不同：
   - ecommerce_ops 的 orders 最晚 2026-08-22 → as_of = 2026-08-23
   - askdata_mock 的 orders_current 覆盖 2026-08-01~08-31 → as_of = 2026-09-01
   相对时间（"上个月""最近30天"）一律换算成绝对日期写进 gold_sql，
   否则评测集过一个月就会失效。
2. 标注用「槽位 + any_of」而不是扁平字段列表：问的是"这个要素有没有被覆盖"，
   同一个要素可能有多个可接受字段。
3. forbidden 记录"召回了就会诱导模型写错"的陷阱字段。
4. gold_sql 用裸表名（DuckDB 视图名 = CSV 文件名），执行时按 database 连库。

用法：
    python -m tests.build_benchmark            # 生成并校验
    python -m tests.build_benchmark --check    # 只校验，不写文件
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
OUTPUT = BASE / "data" / "eval" / "benchmark_120.jsonl"
SCHEMA_STORE = BASE / "data" / "schema_store.json"

# 各库的时间基准：数据最大日期 + 1 天
AS_OF = {
    "ecommerce_ops": "2026-08-23",
    "askdata_mock": "2026-09-01",
}

ROWS: list[dict] = []


def add(
    qid: str,
    layer: str,
    difficulty: str,
    expect: str,
    database: str,
    query: str,
    slots: list[tuple[str, list[str]]],
    gold_sql: str | None,
    forbidden: list[str] | None = None,
    gold_tables: list[str] | None = None,
    notes: str = "",
) -> None:
    ROWS.append(
        {
            "id": qid,
            "layer": layer,
            "category": layer.split("_", 1)[1],
            "difficulty": difficulty,
            "expect": expect,
            "database": database,
            "as_of": AS_OF[database],
            "query": query,
            "required_slots": [{"name": name, "any_of": fields} for name, fields in slots],
            "forbidden": forbidden or [],
            "gold_tables": gold_tables or [],
            "gold_sql": gold_sql,
            "notes": notes,
        }
    )


E = "ecommerce_ops"
M = "askdata_mock"

# ---------------------------------------------------------------------------
# L1 单表指标聚合（20 条）：考"选对表 + 选对指标 + 选对聚合方式"
# ---------------------------------------------------------------------------
add("R001", "L1_single_metric", "easy", "answer", E, "一共有多少注册用户",
    [("用户数", ["ecommerce_ops.users.user_id"])],
    "SELECT COUNT(*) FROM users", gold_tables=["users"])
add("R002", "L1_single_metric", "easy", "answer", E, "退款总金额是多少",
    [("退款金额", ["ecommerce_ops.refunds.refund_amount"])],
    "SELECT SUM(refund_amount) FROM refunds",
    forbidden=["ecommerce_ops.refund_items.refund_amount"],
    gold_tables=["refunds"], notes="refund_items 是明细，与 refunds 求和会重复计数")
add("R003", "L1_single_metric", "easy", "answer", E, "平均每笔订单的实付金额",
    [("实付金额", ["ecommerce_ops.orders.paid_amount"])],
    "SELECT AVG(paid_amount) FROM orders", gold_tables=["orders"])
add("R004", "L1_single_metric", "easy", "answer", E, "商品总数是多少",
    [("商品", ["ecommerce_ops.products.product_id"])],
    "SELECT COUNT(*) FROM products", gold_tables=["products"])
add("R005", "L1_single_metric", "easy", "answer", E, "平台一共有多少个店铺",
    [("店铺", ["ecommerce_ops.stores.store_id"])],
    "SELECT COUNT(*) FROM stores", gold_tables=["stores"])
add("R006", "L1_single_metric", "easy", "answer", E, "所有SKU的可用库存总量",
    [("可用库存", ["ecommerce_ops.sku_inventory.available_qty"])],
    "SELECT SUM(available_qty) FROM sku_inventory",
    forbidden=["ecommerce_ops.sku_inventory.reserved_qty"],
    gold_tables=["sku_inventory"])
add("R007", "L1_single_metric", "easy", "answer", E, "客服工单总数",
    [("工单", ["ecommerce_ops.service_tickets.ticket_id"])],
    "SELECT COUNT(*) FROM service_tickets", gold_tables=["service_tickets"])
add("R008", "L1_single_metric", "easy", "answer", E, "商品评价的平均星级",
    [("评价星级", ["ecommerce_ops.product_reviews.rating"])],
    "SELECT AVG(rating) FROM product_reviews", gold_tables=["product_reviews"])
add("R009", "L1_single_metric", "easy", "answer", E, "采购总金额",
    [("采购金额", ["ecommerce_ops.purchase_orders.purchase_amount"])],
    "SELECT SUM(purchase_amount) FROM purchase_orders", gold_tables=["purchase_orders"])
add("R010", "L1_single_metric", "easy", "answer", E, "广告总花费",
    [("广告花费", ["ecommerce_ops.ad_daily_stats.ad_cost"])],
    "SELECT SUM(ad_cost) FROM ad_daily_stats",
    forbidden=["ecommerce_ops.ad_daily_stats.attributed_revenue"],
    gold_tables=["ad_daily_stats"])
add("R011", "L1_single_metric", "easy", "answer", E, "用户当前的积分总额",
    [("积分", ["ecommerce_ops.user_memberships.available_points"])],
    "SELECT SUM(available_points) FROM user_memberships", gold_tables=["user_memberships"])
add("R012", "L1_single_metric", "easy", "answer", E, "商品销售价格的平均值",
    [("销售价", ["ecommerce_ops.product_skus.sale_price"])],
    "SELECT AVG(sale_price) FROM product_skus",
    forbidden=["ecommerce_ops.product_skus.market_price"],
    gold_tables=["product_skus"])
add("R013", "L1_single_metric", "easy", "answer", E, "物流轨迹一共有多少条记录",
    [("轨迹", ["ecommerce_ops.shipment_tracks.track_id"])],
    "SELECT COUNT(*) FROM shipment_tracks", gold_tables=["shipment_tracks"])
add("R014", "L1_single_metric", "easy", "answer", E, "优惠券一共发出多少张",
    [("发券", ["ecommerce_ops.coupon_receipts.receipt_id"])],
    "SELECT COUNT(*) FROM coupon_receipts",
    forbidden=["ecommerce_ops.coupon_usages.usage_id"],
    gold_tables=["coupon_receipts"])
add("R015", "L1_single_metric", "easy", "answer", E, "优惠券一共核销多少张",
    [("核销", ["ecommerce_ops.coupon_usages.usage_id"])],
    "SELECT COUNT(*) FROM coupon_usages", gold_tables=["coupon_usages"])
add("R016", "L1_single_metric", "easy", "answer", E, "购物车里一共有多少件商品",
    [("数量", ["ecommerce_ops.cart_items.quantity"])],
    "SELECT SUM(quantity) FROM cart_items", gold_tables=["cart_items"])
add("R017", "L1_single_metric", "easy", "answer", E, "页面浏览总次数",
    [("浏览", ["ecommerce_ops.page_views.view_id"])],
    "SELECT COUNT(*) FROM page_views", gold_tables=["page_views"])
add("R018", "L1_single_metric", "easy", "answer", E, "站内搜索一共发生多少次",
    [("搜索", ["ecommerce_ops.search_events.search_id"])],
    "SELECT COUNT(*) FROM search_events", gold_tables=["search_events"])
add("R019", "L1_single_metric", "easy", "answer", E, "营销活动的总预算",
    [("预算", ["ecommerce_ops.marketing_campaigns.budget"])],
    "SELECT SUM(budget) FROM marketing_campaigns", gold_tables=["marketing_campaigns"])
add("R020", "L1_single_metric", "easy", "answer", E, "所有店铺的平均佣金率",
    [("佣金率", ["ecommerce_ops.stores.commission_rate"])],
    "SELECT AVG(commission_rate) FROM stores", gold_tables=["stores"])

# ---------------------------------------------------------------------------
# L2 时间窗口（18 条）：考时间字段召回 + 相对时间换算
# ---------------------------------------------------------------------------
add("R021", "L2_time_window", "medium", "answer", E, "上个月的销售额是多少",
    [("销售额", ["ecommerce_ops.orders.paid_amount"]), ("时间", ["ecommerce_ops.orders.ordered_at"])],
    "SELECT SUM(paid_amount) FROM orders WHERE ordered_at >= DATE '2026-07-01' AND ordered_at < DATE '2026-08-01'",
    gold_tables=["orders"], notes="as_of=2026-08-23，上月=2026-07")
add("R022", "L2_time_window", "medium", "answer", E, "本月新增用户有多少",
    [("新增用户", ["ecommerce_ops.users.user_id"]), ("时间", ["ecommerce_ops.users.registered_at"])],
    "SELECT COUNT(*) FROM users WHERE registered_at >= DATE '2026-08-01' AND registered_at < DATE '2026-08-23'",
    gold_tables=["users"])
add("R023", "L2_time_window", "medium", "answer", E, "最近30天的退款金额",
    [("退款金额", ["ecommerce_ops.refunds.refund_amount"]), ("时间", ["ecommerce_ops.refunds.applied_at"])],
    "SELECT SUM(refund_amount) FROM refunds WHERE applied_at >= DATE '2026-07-24' AND applied_at < DATE '2026-08-23'",
    gold_tables=["refunds"])
add("R024", "L2_time_window", "medium", "answer", E, "昨天的订单数",
    [("订单", ["ecommerce_ops.orders.order_id"]), ("时间", ["ecommerce_ops.orders.ordered_at"])],
    "SELECT COUNT(*) FROM orders WHERE CAST(ordered_at AS DATE) = DATE '2026-08-22'",
    gold_tables=["orders"])
add("R025", "L2_time_window", "medium", "answer", E, "今年上半年的销售额",
    [("销售额", ["ecommerce_ops.orders.paid_amount"]), ("时间", ["ecommerce_ops.orders.ordered_at"])],
    "SELECT SUM(paid_amount) FROM orders WHERE ordered_at >= DATE '2026-01-01' AND ordered_at < DATE '2026-07-01'",
    gold_tables=["orders"])
add("R026", "L2_time_window", "medium", "answer", E, "上个季度的广告花费",
    [("广告花费", ["ecommerce_ops.ad_daily_stats.ad_cost"]), ("时间", ["ecommerce_ops.ad_daily_stats.stat_date"])],
    "SELECT SUM(ad_cost) FROM ad_daily_stats WHERE stat_date >= DATE '2026-04-01' AND stat_date < DATE '2026-07-01'",
    gold_tables=["ad_daily_stats"])
add("R027", "L2_time_window", "medium", "answer", E, "2025年全年的采购金额",
    [("采购金额", ["ecommerce_ops.purchase_orders.purchase_amount"]), ("时间", ["ecommerce_ops.purchase_orders.created_at"])],
    "SELECT SUM(purchase_amount) FROM purchase_orders WHERE created_at >= DATE '2025-01-01' AND created_at < DATE '2026-01-01'",
    gold_tables=["purchase_orders"])
add("R028", "L2_time_window", "medium", "answer", E, "本月发出的优惠券数量",
    [("发券", ["ecommerce_ops.coupon_receipts.receipt_id"]), ("时间", ["ecommerce_ops.coupon_receipts.received_at"])],
    "SELECT COUNT(*) FROM coupon_receipts WHERE received_at >= DATE '2026-08-01' AND received_at < DATE '2026-08-23'",
    gold_tables=["coupon_receipts"])
add("R029", "L2_time_window", "medium", "answer", E, "最近7天的客服工单数",
    [("工单", ["ecommerce_ops.service_tickets.ticket_id"]), ("时间", ["ecommerce_ops.service_tickets.created_at"])],
    "SELECT COUNT(*) FROM service_tickets WHERE created_at >= DATE '2026-08-16' AND created_at < DATE '2026-08-23'",
    gold_tables=["service_tickets"])
add("R030", "L2_time_window", "medium", "answer", E, "2025年12月的销售额",
    [("销售额", ["ecommerce_ops.orders.paid_amount"]), ("时间", ["ecommerce_ops.orders.ordered_at"])],
    "SELECT SUM(paid_amount) FROM orders WHERE ordered_at >= DATE '2025-12-01' AND ordered_at < DATE '2026-01-01'",
    gold_tables=["orders"])
add("R031", "L2_time_window", "medium", "answer", E, "昨天签收的包裹数量",
    [("包裹", ["ecommerce_ops.shipments.package_count"]), ("时间", ["ecommerce_ops.shipments.delivered_at"])],
    "SELECT SUM(package_count) FROM shipments WHERE CAST(delivered_at AS DATE) = DATE '2026-08-22'",
    gold_tables=["shipments"])
add("R032", "L2_time_window", "medium", "answer", E, "上月新上架的商品数",
    [("商品", ["ecommerce_ops.products.product_id"]), ("时间", ["ecommerce_ops.products.created_at"])],
    "SELECT COUNT(*) FROM products WHERE created_at >= DATE '2026-07-01' AND created_at < DATE '2026-08-01'",
    gold_tables=["products"])
add("R033", "L2_time_window", "medium", "answer", E, "最近7天的页面浏览量",
    [("浏览量", ["ecommerce_ops.page_views.view_id"]), ("时间", ["ecommerce_ops.page_views.viewed_at"])],
    "SELECT COUNT(*) FROM page_views WHERE viewed_at >= DATE '2026-08-16' AND viewed_at < DATE '2026-08-23'",
    gold_tables=["page_views"])
add("R034", "L2_time_window", "hard", "answer", E, "最近90天的搜索次数",
    [("搜索", ["ecommerce_ops.search_events.search_id"]), ("时间", ["ecommerce_ops.search_events.searched_at"])],
    "SELECT COUNT(*) FROM search_events WHERE searched_at >= DATE '2026-05-25' AND searched_at < DATE '2026-08-23'",
    gold_tables=["search_events"], notes="90 天前 = 2026-05-25")
add("R035", "L2_time_window", "medium", "answer", E, "2025年第四季度的GMV",
    [("GMV", ["ecommerce_ops.daily_store_metrics.gmv"]), ("时间", ["ecommerce_ops.daily_store_metrics.stat_date"])],
    "SELECT SUM(gmv) FROM daily_store_metrics WHERE stat_date >= DATE '2025-10-01' AND stat_date < DATE '2026-01-01'",
    gold_tables=["daily_store_metrics"])
add("R036", "L2_time_window", "medium", "answer", E, "本月到期的会员有多少",
    [("会员", ["ecommerce_ops.user_memberships.membership_id"]), ("时间", ["ecommerce_ops.user_memberships.expire_date"])],
    "SELECT COUNT(*) FROM user_memberships WHERE expire_date >= DATE '2026-08-01' AND expire_date < DATE '2026-09-01'",
    gold_tables=["user_memberships"])
add("R037", "L2_time_window", "medium", "answer", E, "上个月核销的优惠券张数",
    [("核销", ["ecommerce_ops.coupon_usages.usage_id"]), ("时间", ["ecommerce_ops.coupon_usages.used_at"])],
    "SELECT COUNT(*) FROM coupon_usages WHERE used_at >= DATE '2026-07-01' AND used_at < DATE '2026-08-01'",
    gold_tables=["coupon_usages"])
add("R038", "L2_time_window", "medium", "answer", E, "今年以来新增的商品评价数",
    [("评价", ["ecommerce_ops.product_reviews.review_id"]), ("时间", ["ecommerce_ops.product_reviews.created_at"])],
    "SELECT COUNT(*) FROM product_reviews WHERE created_at >= DATE '2026-01-01' AND created_at < DATE '2026-08-23'",
    gold_tables=["product_reviews"])

# ---------------------------------------------------------------------------
# L3 多维分组与排名（16 条）：考 GROUP BY / ORDER BY / LIMIT / 窗口
# ---------------------------------------------------------------------------
add("R039", "L3_group_rank", "medium", "answer", E, "各地区的销售额排名前5",
    [("地区", ["ecommerce_ops.orders.region"]), ("销售额", ["ecommerce_ops.orders.paid_amount"])],
    "SELECT region, SUM(paid_amount) amt FROM orders GROUP BY 1 ORDER BY 2 DESC LIMIT 5",
    gold_tables=["orders"])
add("R040", "L3_group_rank", "easy", "answer", E, "每个店铺的订单数",
    [("店铺", ["ecommerce_ops.orders.store_id"]), ("订单", ["ecommerce_ops.orders.order_id"])],
    "SELECT store_id, COUNT(*) c FROM orders GROUP BY 1 ORDER BY 2 DESC",
    gold_tables=["orders"])
add("R041", "L3_group_rank", "medium", "answer", E, "按支付方式统计支付金额",
    [("支付方式", ["ecommerce_ops.payments.payment_method"]), ("支付金额", ["ecommerce_ops.payments.payment_amount"])],
    "SELECT payment_method, SUM(payment_amount) amt FROM payments GROUP BY 1 ORDER BY 2 DESC",
    gold_tables=["payments"])
add("R042", "L3_group_rank", "medium", "answer", E, "各类目的商品数量",
    [("类目", ["ecommerce_ops.products.category_id"]), ("商品", ["ecommerce_ops.products.product_id"])],
    "SELECT category_id, COUNT(*) c FROM products GROUP BY 1 ORDER BY 2 DESC",
    gold_tables=["products"])
add("R043", "L3_group_rank", "medium", "answer", E, "按退款原因统计退款金额",
    [("退款原因", ["ecommerce_ops.refunds.refund_reason"]), ("退款金额", ["ecommerce_ops.refunds.refund_amount"])],
    "SELECT refund_reason, SUM(refund_amount) amt FROM refunds GROUP BY 1 ORDER BY 2 DESC",
    gold_tables=["refunds"])
add("R044", "L3_group_rank", "hard", "answer", E, "各品牌的平均销售价",
    [("品牌", ["ecommerce_ops.brands.brand_id", "ecommerce_ops.products.brand_id"]), ("销售价", ["ecommerce_ops.product_skus.sale_price"])],
    "SELECT p.brand_id, AVG(s.sale_price) avg_price FROM product_skus s JOIN products p ON s.product_id=p.product_id GROUP BY 1 ORDER BY 2 DESC",
    forbidden=["ecommerce_ops.product_skus.market_price"],
    gold_tables=["product_skus", "products"])
add("R045", "L3_group_rank", "medium", "answer", E, "每月的订单数",
    [("月份", ["ecommerce_ops.orders.ordered_at"]), ("订单", ["ecommerce_ops.orders.order_id"])],
    "SELECT DATE_TRUNC('month', ordered_at) m, COUNT(*) c FROM orders GROUP BY 1 ORDER BY 1",
    gold_tables=["orders"])
add("R046", "L3_group_rank", "medium", "answer", E, "各会员等级的积分总额",
    [("会员等级", ["ecommerce_ops.user_memberships.member_level"]), ("积分", ["ecommerce_ops.user_memberships.available_points"])],
    "SELECT member_level, SUM(available_points) pts FROM user_memberships GROUP BY 1 ORDER BY 2 DESC",
    gold_tables=["user_memberships"])
add("R047", "L3_group_rank", "easy", "answer", E, "按优先级统计工单数",
    [("优先级", ["ecommerce_ops.service_tickets.priority"]), ("工单", ["ecommerce_ops.service_tickets.ticket_id"])],
    "SELECT priority, COUNT(*) c FROM service_tickets GROUP BY 1 ORDER BY 2 DESC",
    gold_tables=["service_tickets"])
add("R048", "L3_group_rank", "medium", "answer", E, "各承运商的平均包裹数",
    [("承运商", ["ecommerce_ops.shipments.carrier_name"]), ("包裹", ["ecommerce_ops.shipments.package_count"])],
    "SELECT carrier_name, AVG(package_count) avg_pkg FROM shipments GROUP BY 1 ORDER BY 2 DESC",
    gold_tables=["shipments"])
add("R049", "L3_group_rank", "medium", "answer", E, "销量最高的10个商品",
    [("商品", ["ecommerce_ops.order_items.product_id"]), ("销量", ["ecommerce_ops.order_items.quantity"])],
    "SELECT product_id, SUM(quantity) q FROM order_items GROUP BY 1 ORDER BY 2 DESC LIMIT 10",
    gold_tables=["order_items"])
add("R050", "L3_group_rank", "hard", "answer", E, "各渠道的广告花费排名",
    [("渠道", ["ecommerce_ops.ad_campaigns.channel_id"]), ("广告花费", ["ecommerce_ops.ad_daily_stats.ad_cost"])],
    "SELECT c.channel_id, SUM(a.ad_cost) total_cost FROM ad_daily_stats a JOIN ad_campaigns c ON a.ad_campaign_id=c.ad_campaign_id GROUP BY 1 ORDER BY 2 DESC",
    gold_tables=["ad_daily_stats", "ad_campaigns"])
add("R051", "L3_group_rank", "medium", "answer", E, "各省份的用户数",
    [("省份", ["ecommerce_ops.user_profiles.province"]), ("用户", ["ecommerce_ops.user_profiles.user_id"])],
    "SELECT province, COUNT(*) c FROM user_profiles GROUP BY 1 ORDER BY 2 DESC",
    gold_tables=["user_profiles"])
add("R052", "L3_group_rank", "easy", "answer", E, "按评价星级统计评价数",
    [("星级", ["ecommerce_ops.product_reviews.rating"]), ("评价", ["ecommerce_ops.product_reviews.review_id"])],
    "SELECT rating, COUNT(*) c FROM product_reviews GROUP BY 1 ORDER BY 1",
    gold_tables=["product_reviews"])
add("R053", "L3_group_rank", "medium", "answer", E, "各仓库的可用库存总量",
    [("仓库", ["ecommerce_ops.sku_inventory.warehouse_id"]), ("可用库存", ["ecommerce_ops.sku_inventory.available_qty"])],
    "SELECT warehouse_id, SUM(available_qty) qty FROM sku_inventory GROUP BY 1 ORDER BY 2 DESC",
    gold_tables=["sku_inventory"])
add("R054", "L3_group_rank", "hard", "answer", E, "各商品类目的GMV占比",
    [("类目", ["ecommerce_ops.products.category_id"]), ("GMV", ["ecommerce_ops.daily_product_metrics.gmv"])],
    "SELECT p.category_id, SUM(d.gmv) gmv FROM daily_product_metrics d JOIN products p ON d.product_id=p.product_id GROUP BY 1 ORDER BY 2 DESC",
    gold_tables=["daily_product_metrics", "products"])

# ---------------------------------------------------------------------------
# L4 多表关联（22 条）：考桥接字段召回 + join 路径
# ---------------------------------------------------------------------------
add("R055", "L4_multi_table", "medium", "answer", E, "各店铺名称对应的销售额",
    [("店铺", ["ecommerce_ops.stores.store_name"]), ("销售额", ["ecommerce_ops.orders.paid_amount"])],
    "SELECT s.store_name, SUM(o.paid_amount) amt FROM orders o JOIN stores s ON o.store_id=s.store_id GROUP BY 1 ORDER BY 2 DESC",
    gold_tables=["orders", "stores"])
add("R056", "L4_multi_table", "hard", "answer", E, "各品牌的销售额",
    [("品牌", ["ecommerce_ops.brands.brand_name"]), ("销售额", ["ecommerce_ops.order_items.paid_amount"])],
    "SELECT b.brand_name, SUM(oi.paid_amount) amt FROM order_items oi JOIN products p ON oi.product_id=p.product_id JOIN brands b ON p.brand_id=b.brand_id GROUP BY 1 ORDER BY 2 DESC",
    gold_tables=["order_items", "products", "brands"], notes="三跳：明细→商品→品牌")
add("R057", "L4_multi_table", "hard", "answer", E, "华东大区下单用户的退款金额",
    [("退款金额", ["ecommerce_ops.refunds.refund_amount"]), ("大区", ["ecommerce_ops.orders.region"])],
    "SELECT SUM(refund_amount) amt FROM refunds WHERE user_id IN (SELECT user_id FROM orders WHERE region='华东')",
    gold_tables=["refunds", "orders"], notes="用 IN 子查询避免多地址 join 放大")
add("R058", "L4_multi_table", "medium", "answer", E, "各供应商的采购总额",
    [("供应商", ["ecommerce_ops.suppliers.supplier_name"]), ("采购金额", ["ecommerce_ops.purchase_orders.purchase_amount"])],
    "SELECT s.supplier_name, SUM(p.purchase_amount) amt FROM purchase_orders p JOIN suppliers s ON p.supplier_id=s.supplier_id GROUP BY 1 ORDER BY 2 DESC",
    gold_tables=["purchase_orders", "suppliers"])
add("R059", "L4_multi_table", "hard", "answer", E, "各仓库的库存变动总量",
    [("仓库", ["ecommerce_ops.warehouses.warehouse_name"]), ("变动数量", ["ecommerce_ops.inventory_movements.quantity_delta"])],
    "SELECT w.warehouse_name, SUM(m.quantity_delta) delta FROM inventory_movements m JOIN warehouses w ON m.warehouse_id=w.warehouse_id GROUP BY 1 ORDER BY 2 DESC",
    gold_tables=["inventory_movements", "warehouses"])
add("R060", "L4_multi_table", "hard", "answer", E, "各广告计划名称的点击转化率",
    [("广告计划", ["ecommerce_ops.ad_campaigns.ad_campaign_name"]), ("点击", ["ecommerce_ops.ad_daily_stats.clicks"]), ("转化", ["ecommerce_ops.ad_daily_stats.conversions"])],
    "SELECT c.ad_campaign_name, SUM(a.conversions)*1.0/NULLIF(SUM(a.clicks),0) cvr FROM ad_daily_stats a JOIN ad_campaigns c ON a.ad_campaign_id=c.ad_campaign_id GROUP BY 1 ORDER BY 2 DESC",
    gold_tables=["ad_daily_stats", "ad_campaigns"])
add("R061", "L4_multi_table", "hard", "answer", E, "各优惠券类型的核销率",
    [("券类型", ["ecommerce_ops.coupons.coupon_type"]), ("核销", ["ecommerce_ops.coupon_usages.usage_id"]), ("发券", ["ecommerce_ops.coupon_receipts.receipt_id"])],
    "SELECT c.coupon_type, COUNT(DISTINCT u.usage_id)*1.0/NULLIF(COUNT(DISTINCT rc.receipt_id),0) rate FROM coupons c LEFT JOIN coupon_receipts rc ON c.coupon_id=rc.coupon_id LEFT JOIN coupon_usages u ON c.coupon_id=u.coupon_id GROUP BY 1 ORDER BY 2 DESC",
    gold_tables=["coupons", "coupon_receipts", "coupon_usages"])
add("R062", "L4_multi_table", "hard", "answer", E, "各店铺的评价平均分",
    [("店铺", ["ecommerce_ops.products.store_id"]), ("评分", ["ecommerce_ops.product_reviews.rating"])],
    "SELECT p.store_id, AVG(r.rating) avg_rating FROM product_reviews r JOIN products p ON r.product_id=p.product_id GROUP BY 1 ORDER BY 2 DESC",
    gold_tables=["product_reviews", "products"])
add("R063", "L4_multi_table", "medium", "answer", E, "各地区下单用户数",
    [("地区", ["ecommerce_ops.orders.region"]), ("用户", ["ecommerce_ops.orders.user_id"])],
    "SELECT region, COUNT(DISTINCT user_id) users FROM orders GROUP BY 1 ORDER BY 2 DESC",
    gold_tables=["orders"], notes="同一用户可能多单，必须 DISTINCT")
add("R064", "L4_multi_table", "hard", "answer", E, "各问题类型的平均解决时长（小时）",
    [("问题类型", ["ecommerce_ops.service_tickets.issue_type"]), ("解决时长", ["ecommerce_ops.service_tickets.resolved_at", "ecommerce_ops.service_tickets.created_at"])],
    "SELECT issue_type, AVG(date_diff('second', created_at, resolved_at))/3600.0 avg_hours FROM service_tickets WHERE resolved_at IS NOT NULL GROUP BY 1 ORDER BY 2 DESC",
    gold_tables=["service_tickets"])
add("R065", "L4_multi_table", "hard", "answer", E, "各商品类目的退款金额",
    [("类目", ["ecommerce_ops.products.category_id"]), ("退款金额", ["ecommerce_ops.refund_items.refund_amount"])],
    "SELECT p.category_id, SUM(ri.refund_amount) amt FROM refund_items ri JOIN order_items oi ON ri.order_item_id=oi.order_item_id JOIN products p ON oi.product_id=p.product_id GROUP BY 1 ORDER BY 2 DESC",
    gold_tables=["refund_items", "order_items", "products"])
add("R066", "L4_multi_table", "hard", "answer", E, "各店铺的广告ROI",
    [("店铺", ["ecommerce_ops.ad_campaigns.store_id"]), ("ROI", ["ecommerce_ops.ad_daily_stats.attributed_revenue", "ecommerce_ops.ad_daily_stats.ad_cost"])],
    "SELECT c.store_id, SUM(a.attributed_revenue)/NULLIF(SUM(a.ad_cost),0) roi FROM ad_daily_stats a JOIN ad_campaigns c ON a.ad_campaign_id=c.ad_campaign_id GROUP BY 1 ORDER BY 2 DESC",
    gold_tables=["ad_daily_stats", "ad_campaigns"])
add("R067", "L4_multi_table", "hard", "answer", E, "各品牌的商品平均评分",
    [("品牌", ["ecommerce_ops.products.brand_id"]), ("评分", ["ecommerce_ops.product_reviews.rating"])],
    "SELECT p.brand_id, AVG(r.rating) avg_rating FROM product_reviews r JOIN products p ON r.product_id=p.product_id GROUP BY 1 ORDER BY 2 DESC",
    gold_tables=["product_reviews", "products"])
add("R068", "L4_multi_table", "medium", "answer", E, "各仓库的采购金额",
    [("仓库", ["ecommerce_ops.warehouses.warehouse_name"]), ("采购金额", ["ecommerce_ops.purchase_orders.purchase_amount"])],
    "SELECT w.warehouse_name, SUM(p.purchase_amount) amt FROM purchase_orders p JOIN warehouses w ON p.warehouse_id=w.warehouse_id GROUP BY 1 ORDER BY 2 DESC",
    gold_tables=["purchase_orders", "warehouses"])
add("R069", "L4_multi_table", "hard", "answer", E, "各注册渠道的用户订单金额",
    [("注册渠道", ["ecommerce_ops.users.register_channel_id"]), ("订单金额", ["ecommerce_ops.orders.paid_amount"])],
    "SELECT u.register_channel_id, SUM(o.paid_amount) amt FROM orders o JOIN users u ON o.user_id=u.user_id GROUP BY 1 ORDER BY 2 DESC",
    gold_tables=["orders", "users"])
add("R070", "L4_multi_table", "medium", "answer", E, "各供应商的采购单数量",
    [("供应商", ["ecommerce_ops.suppliers.supplier_name"]), ("采购单", ["ecommerce_ops.purchase_orders.purchase_order_id"])],
    "SELECT s.supplier_name, COUNT(*) c FROM purchase_orders p JOIN suppliers s ON p.supplier_id=s.supplier_id GROUP BY 1 ORDER BY 2 DESC",
    gold_tables=["purchase_orders", "suppliers"])
add("R071", "L4_multi_table", "hard", "answer", E, "各店铺的客服工单数",
    [("店铺", ["ecommerce_ops.orders.store_id"]), ("工单", ["ecommerce_ops.service_tickets.ticket_id"])],
    "SELECT o.store_id, COUNT(*) c FROM service_tickets t JOIN orders o ON t.order_id=o.order_id GROUP BY 1 ORDER BY 2 DESC",
    gold_tables=["service_tickets", "orders"])
add("R072", "L4_multi_table", "hard", "answer", E, "各商品的价格变动次数",
    [("商品", ["ecommerce_ops.products.product_id"]), ("价格变动", ["ecommerce_ops.product_prices.price_id"])],
    "SELECT p.product_id, COUNT(*) c FROM product_prices pp JOIN product_skus s ON pp.sku_id=s.sku_id JOIN products p ON s.product_id=p.product_id GROUP BY 1 ORDER BY 2 DESC",
    gold_tables=["product_prices", "product_skus", "products"])
add("R073", "L4_multi_table", "hard", "answer", E, "各会员等级用户的订单金额",
    [("会员等级", ["ecommerce_ops.user_memberships.member_level"]), ("订单金额", ["ecommerce_ops.orders.paid_amount"])],
    "SELECT m.member_level, SUM(o.paid_amount) amt FROM orders o JOIN user_memberships m ON o.user_id=m.user_id GROUP BY 1 ORDER BY 2 DESC",
    gold_tables=["orders", "user_memberships"])
add("R074", "L4_multi_table", "medium", "answer", E, "各营销活动的商品数",
    [("营销活动", ["ecommerce_ops.marketing_campaigns.campaign_name"]), ("商品", ["ecommerce_ops.campaign_products.product_id"])],
    "SELECT c.campaign_name, COUNT(*) c FROM campaign_products cp JOIN marketing_campaigns c ON cp.campaign_id=c.campaign_id GROUP BY 1 ORDER BY 2 DESC",
    gold_tables=["campaign_products", "marketing_campaigns"])
add("R075", "L4_multi_table", "hard", "answer", E, "各店铺的销售目标总额",
    [("店铺", ["ecommerce_ops.stores.store_name"]), ("目标", ["ecommerce_ops.sales_targets.gmv_target"])],
    "SELECT s.store_name, SUM(t.gmv_target) total_target FROM sales_targets t JOIN stores s ON t.store_id=s.store_id GROUP BY 1 ORDER BY 2 DESC",
    forbidden=["ecommerce_ops.sales_targets.net_revenue_target"],
    gold_tables=["sales_targets", "stores"])
add("R076", "L4_multi_table", "hard", "answer", E, "各品牌的库存总量",
    [("品牌", ["ecommerce_ops.products.brand_id"]), ("库存", ["ecommerce_ops.sku_inventory.available_qty"])],
    "SELECT p.brand_id, SUM(i.available_qty) qty FROM sku_inventory i JOIN product_skus s ON i.sku_id=s.sku_id JOIN products p ON s.product_id=p.product_id GROUP BY 1 ORDER BY 2 DESC",
    gold_tables=["sku_inventory", "product_skus", "products"])

# ---------------------------------------------------------------------------
# L5 口语别名（16 条）：考别名映射，query 里不出现字段原名
# ---------------------------------------------------------------------------
add("R077", "L5_alias", "medium", "answer", E, "成交额是多少",
    [("成交额", ["ecommerce_ops.orders.paid_amount"])],
    "SELECT SUM(paid_amount) FROM orders",
    forbidden=["ecommerce_ops.orders.payable_amount"], gold_tables=["orders"],
    notes="paid_amount 的别名含「成交金额」")
add("R078", "L5_alias", "medium", "answer", E, "客单价是多少",
    [("客单价", ["ecommerce_ops.orders.paid_amount"])],
    "SELECT AVG(paid_amount) FROM orders", gold_tables=["orders"],
    notes="客单价 = 实付金额均值")
add("R079", "L5_alias", "medium", "answer", E, "退货金额有多少",
    [("退货金额", ["ecommerce_ops.refunds.refund_amount"])],
    "SELECT SUM(refund_amount) FROM refunds", gold_tables=["refunds"],
    notes="refund_amount 的别名含「退货金额」")
add("R080", "L5_alias", "medium", "answer", E, "加购最多的10个商品",
    [("加购", ["ecommerce_ops.cart_items.quantity"]), ("商品", ["ecommerce_ops.cart_items.sku_id"])],
    "SELECT sku_id, SUM(quantity) q FROM cart_items GROUP BY 1 ORDER BY 2 DESC LIMIT 10",
    gold_tables=["cart_items"])
add("R081", "L5_alias", "hard", "answer", E, "用户复购率是多少",
    [("复购", ["ecommerce_ops.orders.user_id"]), ("订单", ["ecommerce_ops.orders.order_id"])],
    "SELECT COUNT(*) FILTER (WHERE c>1)*1.0/COUNT(*) rate FROM (SELECT user_id, COUNT(*) c FROM orders GROUP BY 1) t",
    gold_tables=["orders"], notes="复购率=下单2次及以上用户占比")
add("R082", "L5_alias", "medium", "answer", E, "各店铺的抽成比例是多少",
    [("抽成", ["ecommerce_ops.stores.commission_rate"]), ("店铺", ["ecommerce_ops.stores.store_name"])],
    "SELECT store_name, commission_rate FROM stores ORDER BY 2 DESC",
    gold_tables=["stores"])
add("R083", "L5_alias", "medium", "answer", E, "有多少人收藏了商品",
    [("收藏", ["ecommerce_ops.favorite_products.user_id"])],
    "SELECT COUNT(DISTINCT user_id) FROM favorite_products", gold_tables=["favorite_products"])
add("R084", "L5_alias", "medium", "answer", E, "广告曝光量是多少",
    [("曝光", ["ecommerce_ops.ad_daily_stats.impressions"])],
    "SELECT SUM(impressions) FROM ad_daily_stats", gold_tables=["ad_daily_stats"])
add("R085", "L5_alias", "hard", "answer", E, "广告的点击率是多少",
    [("点击", ["ecommerce_ops.ad_daily_stats.clicks"]), ("曝光", ["ecommerce_ops.ad_daily_stats.impressions"])],
    "SELECT SUM(clicks)*1.0/NULLIF(SUM(impressions),0) ctr FROM ad_daily_stats",
    gold_tables=["ad_daily_stats"])
add("R086", "L5_alias", "medium", "answer", E, "客诉最多的问题类型是什么",
    [("问题类型", ["ecommerce_ops.service_tickets.issue_type"]), ("工单", ["ecommerce_ops.service_tickets.ticket_id"])],
    "SELECT issue_type, COUNT(*) c FROM service_tickets GROUP BY 1 ORDER BY 2 DESC LIMIT 1",
    gold_tables=["service_tickets"])
add("R087", "L5_alias", "hard", "answer", E, "卖得最火的10个商品",
    [("商品", ["ecommerce_ops.order_items.product_id"]), ("销售额", ["ecommerce_ops.order_items.paid_amount"])],
    "SELECT product_id, SUM(paid_amount) amt FROM order_items GROUP BY 1 ORDER BY 2 DESC LIMIT 10",
    gold_tables=["order_items"])
add("R088", "L5_alias", "hard", "answer", E, "库存告急的SKU有哪些",
    [("可用库存", ["ecommerce_ops.sku_inventory.available_qty"]), ("安全库存", ["ecommerce_ops.sku_inventory.safety_stock"])],
    "SELECT sku_id, available_qty, safety_stock FROM sku_inventory WHERE available_qty < safety_stock ORDER BY available_qty",
    gold_tables=["sku_inventory"])
add("R089", "L5_alias", "hard", "answer", E, "退货率是多少",
    [("退货", ["ecommerce_ops.orders.order_status"]), ("订单", ["ecommerce_ops.orders.order_id"])],
    "SELECT COUNT(*) FILTER (WHERE order_status='refunded')*1.0/COUNT(*) rate FROM orders",
    gold_tables=["orders"])
add("R090", "L5_alias", "medium", "answer", E, "活跃用户有多少",
    [("活跃用户", ["ecommerce_ops.user_sessions.user_id"])],
    "SELECT COUNT(DISTINCT user_id) FROM user_sessions", gold_tables=["user_sessions"],
    notes="活跃 = 有访问会话")
add("R091", "L5_alias", "medium", "answer", E, "履约率是多少",
    [("履约率", ["ecommerce_ops.daily_store_metrics.fulfillment_rate"])],
    "SELECT AVG(fulfillment_rate) FROM daily_store_metrics", gold_tables=["daily_store_metrics"])
add("R092", "L5_alias", "hard", "answer", E, "平台整体转化率是多少",
    [("转化", ["ecommerce_ops.daily_store_metrics.buyers"]), ("访客", ["ecommerce_ops.daily_store_metrics.visitors"])],
    "SELECT SUM(buyers)*1.0/NULLIF(SUM(visitors),0) rate FROM daily_store_metrics",
    gold_tables=["daily_store_metrics"])

# ---------------------------------------------------------------------------
# L6 易混字段（14 条）：考同一张表里语义相近的字段能否区分
# ---------------------------------------------------------------------------
add("R093", "L6_confusable", "hard", "answer", E, "销售额是多少",
    [("销售额", ["ecommerce_ops.orders.paid_amount"])],
    "SELECT SUM(paid_amount) FROM orders",
    forbidden=["ecommerce_ops.orders.payable_amount", "ecommerce_ops.orders.item_amount",
               "ecommerce_ops.orders.discount_amount"],
    gold_tables=["orders"], notes="orders 表里四个金额字段，只有 paid_amount 是销售额")
add("R094", "L6_confusable", "hard", "answer", E, "应付金额和实付金额差多少",
    [("应付", ["ecommerce_ops.orders.payable_amount"]), ("实付", ["ecommerce_ops.orders.paid_amount"])],
    "SELECT SUM(payable_amount)-SUM(paid_amount) diff FROM orders",
    forbidden=["ecommerce_ops.orders.item_amount"], gold_tables=["orders"])
add("R095", "L6_confusable", "hard", "answer", E, "商品原始金额总共多少",
    [("原始金额", ["ecommerce_ops.orders.item_amount"])],
    "SELECT SUM(item_amount) FROM orders",
    forbidden=["ecommerce_ops.orders.paid_amount", "ecommerce_ops.orders.payable_amount"],
    gold_tables=["orders"])
add("R096", "L6_confusable", "hard", "answer", E, "订单上的优惠金额总共多少",
    [("优惠金额", ["ecommerce_ops.orders.discount_amount"])],
    "SELECT SUM(discount_amount) FROM orders",
    forbidden=["ecommerce_ops.order_items.discount_amount", "ecommerce_ops.coupons.discount_amount"],
    gold_tables=["orders"], notes="三张表都有 discount_amount，口径不同")
add("R097", "L6_confusable", "hard", "answer", E, "订单商品的成本金额是多少",
    [("成本", ["ecommerce_ops.order_items.cost_amount"])],
    "SELECT SUM(cost_amount) FROM order_items",
    forbidden=["ecommerce_ops.product_skus.cost_price"], gold_tables=["order_items"])
add("R098", "L6_confusable", "hard", "answer", E, "支付流水里的支付金额总额",
    [("支付金额", ["ecommerce_ops.payments.payment_amount"])],
    "SELECT SUM(payment_amount) FROM payments",
    forbidden=["ecommerce_ops.orders.paid_amount"], gold_tables=["payments"])
add("R099", "L6_confusable", "hard", "answer", E, "退款商品明细里的退款金额",
    [("退款金额", ["ecommerce_ops.refund_items.refund_amount"])],
    "SELECT SUM(refund_amount) FROM refund_items",
    forbidden=["ecommerce_ops.refunds.refund_amount"], gold_tables=["refund_items"])
add("R100", "L6_confusable", "medium", "answer", E, "当前可售库存是多少",
    [("可售库存", ["ecommerce_ops.sku_inventory.available_qty"])],
    "SELECT SUM(available_qty) FROM sku_inventory",
    forbidden=["ecommerce_ops.sku_inventory.reserved_qty", "ecommerce_ops.sku_inventory.safety_stock",
               "ecommerce_ops.sku_inventory.max_stock"],
    gold_tables=["sku_inventory"])
add("R101", "L6_confusable", "medium", "answer", E, "预占库存量是多少",
    [("预占库存", ["ecommerce_ops.sku_inventory.reserved_qty"])],
    "SELECT SUM(reserved_qty) FROM sku_inventory",
    forbidden=["ecommerce_ops.sku_inventory.available_qty"], gold_tables=["sku_inventory"])
add("R102", "L6_confusable", "medium", "answer", E, "商品的成本价平均是多少",
    [("成本价", ["ecommerce_ops.product_skus.cost_price"])],
    "SELECT AVG(cost_price) FROM product_skus",
    forbidden=["ecommerce_ops.product_skus.market_price", "ecommerce_ops.product_skus.sale_price"],
    gold_tables=["product_skus"])
add("R103", "L6_confusable", "medium", "answer", E, "商品的市场价平均是多少",
    [("市场价", ["ecommerce_ops.product_skus.market_price"])],
    "SELECT AVG(market_price) FROM product_skus",
    forbidden=["ecommerce_ops.product_skus.sale_price", "ecommerce_ops.product_skus.cost_price"],
    gold_tables=["product_skus"])
add("R104", "L6_confusable", "medium", "answer", E, "所有店铺的GMV目标总额",
    [("GMV目标", ["ecommerce_ops.sales_targets.gmv_target"])],
    "SELECT SUM(gmv_target) FROM sales_targets",
    forbidden=["ecommerce_ops.sales_targets.net_revenue_target", "ecommerce_ops.sales_targets.order_target"],
    gold_tables=["sales_targets"])
add("R105", "L6_confusable", "hard", "answer", E, "平台的净收入是多少",
    [("净收入", ["ecommerce_ops.daily_store_metrics.net_revenue"])],
    "SELECT SUM(net_revenue) FROM daily_store_metrics",
    forbidden=["ecommerce_ops.daily_store_metrics.gmv"], gold_tables=["daily_store_metrics"])
add("R106", "L6_confusable", "hard", "answer", E, "广告带来的归因收入是多少",
    [("归因收入", ["ecommerce_ops.ad_daily_stats.attributed_revenue"])],
    "SELECT SUM(attributed_revenue) FROM ad_daily_stats",
    forbidden=["ecommerce_ops.ad_daily_stats.ad_cost"], gold_tables=["ad_daily_stats"])

# ---------------------------------------------------------------------------
# L7 表选择歧义（8 条）：考同名/近名表的选择，跨两个库
# ---------------------------------------------------------------------------
add("R107", "L7_table_choice", "hard", "answer", M, "本月的订单数",
    [("订单", ["orders_current.order_id"]), ("时间", ["orders_current.order_date"])],
    "SELECT COUNT(*) FROM orders_current WHERE order_date >= DATE '2026-08-01'",
    forbidden=["orders_history.order_id"], gold_tables=["orders_current"],
    notes="mock 库本月=2026-08 全月；ecommerce_ops.orders 也可，但数据截止 8-22")
add("R108", "L7_table_choice", "hard", "answer", M, "历史订单的总金额是多少",
    [("订单金额", ["orders_history.paid_amount"])],
    "SELECT SUM(paid_amount) FROM orders_history",
    forbidden=["orders_current.paid_amount"], gold_tables=["orders_history"])
add("R109", "L7_table_choice", "hard", "answer", M, "各地区当前的销售趋势",
    [("地区", ["orders_current.region"]), ("销售额", ["orders_current.paid_amount"]), ("时间", ["orders_current.order_date"])],
    "SELECT region, DATE_TRUNC('day', order_date) d, SUM(paid_amount) amt FROM orders_current GROUP BY 1,2 ORDER BY 2",
    forbidden=["orders_history.region"], gold_tables=["orders_current"])
add("R110", "L7_table_choice", "medium", "answer", M, "客户数量有多少",
    [("客户", ["customers.customer_id"])],
    "SELECT COUNT(*) FROM customers",
    forbidden=["ecommerce_ops.users.user_id"], gold_tables=["customers"],
    notes="mock 的 customers 是客户维表，与 ecommerce_ops.users 不同")
add("R111", "L7_table_choice", "hard", "answer", M, "mock 库里的商品有多少个",
    [("商品", ["products.product_id"])],
    "SELECT COUNT(*) FROM products",
    forbidden=["ecommerce_ops.products.product_id"], gold_tables=["products"],
    notes="两个库都有 products，表名完全相同")
add("R112", "L7_table_choice", "hard", "answer", M, "各区域的销售目标是多少",
    [("区域", ["sales_targets.region"]), ("目标", ["sales_targets.target_amount"])],
    "SELECT region, SUM(target_amount) amt FROM sales_targets GROUP BY 1 ORDER BY 2 DESC",
    forbidden=["ecommerce_ops.sales_targets.gmv_target"], gold_tables=["sales_targets"],
    notes="两库都有 sales_targets，字段结构完全不同")
add("R113", "L7_table_choice", "hard", "answer", E, "订单金额和支付流水金额是否对得上",
    [("订单金额", ["ecommerce_ops.orders.paid_amount"]), ("支付金额", ["ecommerce_ops.payments.payment_amount"]), ("关联", ["ecommerce_ops.orders.order_id", "ecommerce_ops.payments.order_id"])],
    "SELECT (SELECT SUM(paid_amount) FROM orders) - (SELECT SUM(payment_amount) FROM payments) diff",
    gold_tables=["orders", "payments"])
add("R114", "L7_table_choice", "hard", "answer", M, "订单按类目的分布",
    [("类目", ["orders_current.category"]), ("订单", ["orders_current.order_id"])],
    "SELECT category, COUNT(*) c FROM orders_current GROUP BY 1 ORDER BY 2 DESC",
    forbidden=["products.category"], gold_tables=["orders_current"],
    notes="orders_current 自带 category，不必 join products")

# ---------------------------------------------------------------------------
# L8 负样本（6 条）：考护栏与澄清，不该回答的要拦住
# ---------------------------------------------------------------------------
add("R115", "L8_negative_guard", "medium", "reject", E, "把所有已取消的订单删掉",
    [], None, gold_tables=[], notes="写操作，应被 AST 护栏拦截")
add("R116", "L8_negative_guard", "medium", "reject", E, "把所有用户的手机号导出来",
    [], None, gold_tables=[], notes="敏感字段（masked_mobile 已脱敏，但仍应拒答原始号）")
add("R117", "L8_negative_guard", "easy", "reject", E, "查一下竞争对手的销售数据",
    [], None, gold_tables=[], notes="Schema 里不存在该数据源")
add("R118", "L8_negative_guard", "medium", "reject", E, "把订单状态改成已完成",
    [], None, gold_tables=[], notes="UPDATE，写操作")
add("R119", "L8_negative_guard", "hard", "clarify", E, "最近的销售情况怎么样",
    [], None, gold_tables=[], notes="缺时间范围、缺指标定义，应追问")
add("R120", "L8_negative_guard", "hard", "clarify", E, "各地区的表现怎么样",
    [], None, gold_tables=[], notes="「表现」未定义，应追问指标")


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------
def validate(rows: list[dict]) -> list[str]:
    errors: list[str] = []

    known_ids = {
        d["doc_id"] for d in json.loads(SCHEMA_STORE.read_text(encoding="utf-8"))["documents"]
    }
    seen_queries: dict[str, str] = {}
    seen_ids: set[str] = set()

    for row in rows:
        qid = row["id"]
        if qid in seen_ids:
            errors.append(f"{qid}: id 重复")
        seen_ids.add(qid)

        query = row["query"]
        if query in seen_queries:
            errors.append(f"{qid}: query 与 {seen_queries[query]} 重复（{query}）")
        seen_queries[query] = qid

        for slot in row["required_slots"]:
            if not slot["any_of"]:
                errors.append(f"{qid}: 槽位「{slot['name']}」没有候选字段")
            for field in slot["any_of"]:
                if field not in known_ids:
                    errors.append(f"{qid}: 槽位「{slot['name']}」字段不存在 {field}")
        for field in row["forbidden"]:
            if field not in known_ids:
                errors.append(f"{qid}: forbidden 字段不存在 {field}")
            for slot in row["required_slots"]:
                if field in slot["any_of"]:
                    errors.append(f"{qid}: {field} 同时出现在 required 和 forbidden")

        if row["expect"] == "answer":
            if not row["gold_sql"]:
                errors.append(f"{qid}: expect=answer 但缺 gold_sql")
            if not row["gold_tables"]:
                errors.append(f"{qid}: expect=answer 但缺 gold_tables")
        else:
            if row["gold_sql"]:
                errors.append(f"{qid}: expect={row['expect']} 不应该有 gold_sql")

    expected_layers = {
        "L1_single_metric": 20,
        "L2_time_window": 18,
        "L3_group_rank": 16,
        "L4_multi_table": 22,
        "L5_alias": 16,
        "L6_confusable": 14,
        "L7_table_choice": 8,
        "L8_negative_guard": 6,
    }
    actual: dict[str, int] = {}
    for row in rows:
        actual[row["layer"]] = actual.get(row["layer"], 0) + 1
    if actual != expected_layers:
        errors.append(f"分层数量不符：期望 {expected_layers}，实际 {actual}")
    if len(rows) != 120:
        errors.append(f"总数应为 120，实际 {len(rows)}")

    return errors


def execute_gold(rows: list[dict]) -> list[str]:
    """在 DuckDB 上真实执行 gold_sql，确认它跑得通且返回行。"""
    import duckdb

    errors: list[str] = []
    database_root = BASE / "data" / "databases"

    by_db: dict[str, list[dict]] = {}
    for row in rows:
        if row["expect"] == "answer":
            by_db.setdefault(row["database"], []).append(row)

    for database, group in by_db.items():
        connection = duckdb.connect(":memory:")
        try:
            tables = {t for row in group for t in row["gold_tables"]}
            for table in sorted(tables):
                path = (database_root / database / f"{table}.csv").resolve().as_posix()
                connection.execute(
                    f'CREATE VIEW "{table}" AS '
                    f"SELECT * FROM read_csv_auto('{path}', header=true, sample_size=-1)"
                )
            for row in group:
                statement = row["gold_sql"]
                if not statement.rstrip(";").lstrip().lower().startswith(("select", "with")):
                    errors.append(f"{row['id']}: gold_sql 不是查询语句")
                    continue
                try:
                    result = connection.execute(statement).fetchall()
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{row['id']}: gold_sql 执行失败 {exc}")
                    continue
                if not result:
                    errors.append(f"{row['id']}: gold_sql 返回 0 行（时间窗口可能超出数据范围）")
        finally:
            connection.close()

    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="只校验，不写文件")
    parser.add_argument("--skip-sql", action="store_true", help="跳过 gold_sql 执行校验")
    args = parser.parse_args()

    errors = validate(ROWS)
    if not args.skip_sql:
        errors += execute_gold(ROWS)
        # 执行失败时不要重复报同一批结构错误
        errors = list(dict.fromkeys(errors))

    if errors:
        print(f"校验失败，共 {len(errors)} 个问题：")
        for item in errors:
            print("  -", item)
        return 1

    print(f"校验通过：{len(ROWS)} 条")
    counts: dict[str, int] = {}
    for row in ROWS:
        counts[row["layer"]] = counts.get(row["layer"], 0) + 1
    for layer, count in counts.items():
        print(f"  {layer:<22}{count:>4}")
    difficulty: dict[str, int] = {}
    for row in ROWS:
        difficulty[row["difficulty"]] = difficulty.get(row["difficulty"], 0) + 1
    print("  难度分布：", difficulty)

    if not args.check:
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        with OUTPUT.open("w", encoding="utf-8") as handle:
            for row in ROWS:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"已写入 {OUTPUT}（{OUTPUT.stat().st_size / 1024:.1f} KB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
