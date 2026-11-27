"""交易纪律的只读系统订单归属查询，由 Scheduler 判定及 Worker 复核调用。

复用已有发单前记录和绑定表，不补写绑定，不改变策略状态。完整订单号码按账户、
交易所、产品及合约限定范围，查询异常交给调用方转为待复核。
"""

from app.services.execution_streams.repository import ExecutionEventRepository
from app.services.live_trading.binance import BinanceFuturesClient
from app.services.live_trading.symbols import to_binance_futures_symbol
from app.trade_discipline.position_evidence import _BINANCE_IDS
from app.utils.db import get_db_connection


def read_system_orders(inventory, instrument, order_id, client_order_id):
    """返回命中的系统记录以及无法排除的提交，不修改任何业务数据。"""
    scope = (inventory["user_id"], inventory["credential_id"], _BINANCE_IDS,
             inventory["market_type"], instrument)
    # 币安客户端发单时会为本地号码添加 broker 前缀，并按 36 字符截断。
    # 兼容既有发单前保存的原始号码：按同一格式化规则核对实际发出的完整号码。
    # 此处的前缀转换只用于匹配真实本地记录，不能凭前缀直接判定系统或人工。
    prefix = f"x-{BinanceFuturesClient._BROKER_ID}"
    budget = 36 - len(prefix)
    local_prefix = client_order_id[len(prefix):] if client_order_id.startswith(prefix) else ""
    identifiers = (order_id, client_order_id, local_prefix, local_prefix)
    matches = []
    ambiguous = False
    with get_db_connection() as db:
        cur = db.cursor()
        try:
            # 历史绑定即使对应策略已经删除仍证明系统归属。多个发单阶段的号码还可能
            # 保存在 identifiers 表中，必须一起查，避免限价转市价后漏认早先订单。
            cur.execute(f"""
                SELECT b.id, b.owner_type, b.strategy_id
                FROM qd_live_order_bindings b
                WHERE b.user_id = %s AND b.credential_id = %s AND b.exchange_id IN %s
                  AND b.market_type = %s
                  AND regexp_replace(upper(split_part(b.symbol, ':', 1)), '[-/_]', '', 'g') = %s
                  AND (b.exchange_order_id = %s OR b.client_order_id = NULLIF(%s, '')
                    OR (%s <> '' AND LEFT(b.client_order_id, {budget}) = %s)
                    OR EXISTS (SELECT 1 FROM qd_live_order_binding_identifiers i
                        WHERE i.binding_id = b.id AND
                          ((i.identifier_type = 'exchange_order_id' AND i.identifier_value = %s)
                            OR (i.identifier_type = 'client_order_id' AND (i.identifier_value = NULLIF(%s, '')
                              OR (%s <> '' AND LEFT(i.identifier_value, {budget}) = %s))))))
                """, (*scope, *identifiers, *identifiers))
            matches.extend(dict(row, source="binding") for row in cur.fetchall())

            # 发单前记录是绑定缺失、回写订单号失败及接单后超时的恢复依据。
            # 不用状态筛掉历史匹配。未匹配但身份缺失的处理中开仓需留待对账。
            for table, entry_filter in (
                ("pending_orders", "execution_mode = 'live' AND signal_type NOT ILIKE '%%close%%'"),
                ("qd_quick_trades", "COALESCE(raw_result->'_quick_trade'->>'is_close', 'false') <> 'true'"),
            ):
                cur.execute(f"""
                    SELECT id, exchange_order_id, client_order_id, status
                    FROM {table}
                    WHERE user_id = %s AND credential_id = %s AND exchange_id IN %s
                      AND market_type = %s
                      AND regexp_replace(upper(split_part(symbol, ':', 1)), '[-/_]', '', 'g') = %s
                      AND (exchange_order_id = %s OR client_order_id = NULLIF(%s, '')
                        OR (%s <> '' AND LEFT(client_order_id, {budget}) = %s)
                        OR ({entry_filter} AND status IN ('processing', 'sent', 'pending')
                            AND COALESCE(client_order_id, '') = '' AND COALESCE(exchange_order_id, '') = ''))
                    """, (*scope, *identifiers))
                for row in cur.fetchall():
                    if str(row.get("exchange_order_id") or "") == order_id or (
                        client_order_id and (row.get("client_order_id") == client_order_id
                            or (local_prefix and str(row.get("client_order_id") or "")[:budget] == local_prefix))
                    ):
                        matches.append(dict(row, source=table))
                    else:
                        ambiguous = True

            # 网格发单前已有 resting_orders 记录，归属匹配复用原系统的纯校验函数。
            # 此处不会调用会发现旧记录并写库的 resolve_binding()。
            cur.execute(f"""
                SELECT gro.*, st.user_id, st.market_type, st.exchange_config, st.trading_config
                FROM qd_grid_resting_orders gro
                JOIN qd_strategies_trading st ON st.id = gro.strategy_id
                WHERE st.user_id = %s
                  AND (gro.exchange_order_id = %s OR gro.client_order_id = NULLIF(%s, '')
                    OR (%s <> '' AND LEFT(gro.client_order_id, {budget}) = %s))
                """, (inventory["user_id"], *identifiers))
            event = dict(inventory, symbol=instrument)
            for row in cur.fetchall():
                if to_binance_futures_symbol(row.get("symbol") or "") != instrument:
                    continue
                # 原系统按标准交易所 ID 校验网格配置，这里统一币安别名。
                event["exchange_id"] = "binance"
                if ExecutionEventRepository._matching_grid_binding(dict(row), event):
                    matches.append(dict(row, source="qd_grid_resting_orders"))
        finally:
            cur.close()
    return matches, ambiguous
