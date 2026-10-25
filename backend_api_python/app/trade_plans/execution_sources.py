"""Read-only, allowlisted adapters for existing execution and archive facts."""
from .repository import public
from .validation import PlanError

KINDS = ('strategy', 'order', 'quick_order', 'trade', 'history')


def accounts(cur, user_id):
    cur.execute('''SELECT id AS credential_id,name,exchange_id FROM qd_exchange_credentials WHERE user_id=%s
        UNION SELECT credential_id,MAX(exchange_account_name),MAX(exchange_id)
        FROM qd_position_management_trade_history h WHERE user_id=%s AND credential_id>0
        AND NOT EXISTS(SELECT 1 FROM qd_exchange_credentials c WHERE c.id=h.credential_id AND c.user_id=h.user_id)
        GROUP BY credential_id ORDER BY credential_id DESC''', (user_id, user_id))
    return [public(row) for row in cur.fetchall()]


def account(cur, user_id, credential_id):
    result = next((row for row in accounts(cur, user_id) if row['credential_id'] == credential_id), None)
    if not result:
        raise PlanError('record_account_not_found', 404)
    return result


def query(kind):
    # Explicit projections must never include credentials, strategy code, or raw exchange payloads.
    if kind == 'strategy':
        return '''SELECT s.id,s.strategy_name AS name,s.symbol,s.market_category AS market,s.market_type,
          s.status,s.execution_mode,s.created_at AS recorded_at,
          s.trading_config->'position_management'->>'side' AS position_side,
          s.exchange_config->>'credentialId' AS credential_id,
          c.exchange_id,c.name AS account_name
          FROM qd_strategies_trading s LEFT JOIN qd_exchange_credentials c
          ON c.id::text=s.exchange_config->>'credentialId' AND c.user_id=s.user_id
          WHERE s.user_id=%s AND s.trading_config->'position_management'->>'enabled'='true' '''
    if kind == 'order':
        return '''SELECT p.id,p.symbol,s.market_category AS market,p.market_type,p.status,p.execution_mode,
          p.credential_id,p.exchange_id,p.inst_id,p.signal_type AS trade_type,p.amount AS requested_amount,
          p.filled AS filled_amount,p.avg_price AS price,p.exchange_order_id,p.created_at AS recorded_at,
          p.executed_at,p.strategy_id FROM pending_orders p LEFT JOIN qd_strategies_trading s
          ON s.id=p.strategy_id AND s.user_id=p.user_id WHERE p.user_id=%s '''
    if kind == 'quick_order':
        return '''SELECT id,symbol,'Crypto' AS market,market_type,status,credential_id,exchange_id,
          side,amount AS requested_amount,filled_amount,avg_fill_price AS price,exchange_order_id,
          created_at AS recorded_at FROM qd_quick_trades WHERE user_id=%s '''
    if kind == 'trade':
        return '''SELECT t.id,t.id AS trade_id,t.symbol,s.market_category AS market,t.market_type,
          t.type AS trade_type,t.price,t.amount AS quantity,t.profit AS ledger_profit,
          t.commission,t.commission_ccy,t.commission_quote,t.fee_status,t.close_reason,
          t.credential_id,t.inst_id,t.strategy_id,t.pending_order_id,t.execution_event_id,t.exchange_fill_id,
          t.created_at AS recorded_at,s.execution_mode,
          COALESCE(e.exchange_id,c.exchange_id) AS exchange_id,e.occurred_at,
          e.quantity AS event_quantity,e.is_cumulative
          FROM qd_strategy_trades t LEFT JOIN qd_strategies_trading s ON s.id=t.strategy_id AND s.user_id=t.user_id
          LEFT JOIN qd_exchange_credentials c ON c.id=t.credential_id AND c.user_id=t.user_id
          LEFT JOIN qd_execution_events e ON e.id=t.execution_event_id AND e.user_id=t.user_id AND e.credential_id=t.credential_id AND e.market_type=t.market_type AND UPPER(e.symbol)=UPPER(t.symbol)
          WHERE t.user_id=%s '''
    if kind == 'history':
        return '''SELECT h.id,h.source_trade_id AS trade_id,h.symbol,h.market_category AS market,h.market_type,
          h.trade_type,h.exit_price AS price,h.filled_amount AS quantity,h.profit AS ledger_profit,h.profit_ccy,
          h.commission,h.commission_ccy,h.commission_quote,h.fee_status,h.close_reason,
          h.credential_id,h.exchange_id,h.inst_id,h.source_strategy_id AS strategy_id,h.pending_order_id,
          h.execution_event_id,h.exchange_fill_id,h.trade_recorded_at AS recorded_at,h.execution_mode,
          h.funding_fee_total,h.funding_fee_ccy,e.occurred_at,e.quantity AS event_quantity,e.is_cumulative
          FROM qd_position_management_trade_history h LEFT JOIN qd_execution_events e
          ON e.id=h.execution_event_id AND e.user_id=h.user_id AND e.credential_id=h.credential_id AND e.market_type=h.market_type AND UPPER(e.symbol)=UPPER(h.symbol) WHERE h.user_id=%s '''
    raise PlanError('invalid_record_source_kind')


def normalize(kind, raw):
    row = public(raw)
    row['source_kind'] = kind
    row['source_id'] = row.pop('id')
    row['market'] = row.get('market') or ''
    row['symbol'] = str(row.get('symbol') or '').upper()
    try:
        row['credential_id'] = int(row.get('credential_id') or 0)
    except (TypeError, ValueError):
        row['credential_id'] = 0
    row['execution_mode'] = row.get('execution_mode') or ''
    row['source_key'] = 'trade:' + str(row['trade_id']) if kind in ('trade', 'history') else kind + ':' + str(row['source_id'])
    event_id = row.get('execution_event_id')
    row['execution_event_id'] = event_id if event_id else None
    row['fill_identity'] = None
    if row.get('exchange_fill_id') and row['credential_id'] and row.get('exchange_id'):
        row['fill_identity'] = '|'.join(str(row.get(key) or '') for key in
            ('credential_id', 'exchange_id', 'market_type', 'symbol', 'exchange_fill_id'))
    row['available'] = True
    return row


def read_source(cur, user_id, kind, source_id):
    column = {'strategy': 's.id', 'order': 'p.id', 'quick_order': 'id', 'trade': 't.id', 'history': 'h.id'}.get(kind)
    sql = query(kind)
    cur.execute(sql + ' AND ' + column + '=%s', (user_id, source_id))
    raw = cur.fetchone()
    return normalize(kind, raw) if raw else None


def list_sources(cur, user_id, kind, credential_id, symbol, page, page_size, scope, source_id=None):
    sql = query(kind)
    alias = {'strategy': 's', 'order': 'p', 'quick_order': '', 'trade': 't', 'history': 'h'}[kind]
    prefix = alias + '.' if alias else ''
    account_field = "s.exchange_config->>'credentialId'" if kind == 'strategy' else prefix + 'credential_id'
    base = sql + ' AND ' + account_field + '=%s AND UPPER(' + prefix + 'symbol)=%s'
    params = (user_id, str(credential_id) if kind == 'strategy' else credential_id, symbol.upper())
    if source_id is not None:
        base += ' AND ' + prefix + 'id=%s'
        params = (*params, source_id)
    visible, scope_params = scope.sql("jsonb_build_object('instrument',jsonb_build_object('market',candidates.market,'symbol',candidates.symbol))")
    base = 'SELECT * FROM (' + base + ') candidates WHERE ' + visible
    if scope._restricted(scope.markets):
        base += " AND COALESCE(candidates.market,'') <> ''"
    if scope._restricted(scope.instruments):
        base += " AND COALESCE(candidates.symbol,'') <> ''"
    params = (*params, *scope_params)
    cur.execute('SELECT COUNT(*) AS total FROM (' + base + ') visible_candidates', params)
    total = cur.fetchone()['total']
    cur.execute(base + ' ORDER BY id DESC LIMIT %s OFFSET %s', (*params, page_size, (page-1)*page_size))
    return {'items': [normalize(kind, row) for row in cur.fetchall()], 'total': total, 'page': page, 'page_size': page_size}
