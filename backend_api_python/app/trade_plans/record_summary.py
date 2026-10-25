"""Conservative facts and return calculations; missing evidence stays unknown."""
from decimal import Decimal
from .validation import timestamp


def dec(value):
    return Decimal(str(value or 0))


def summarize(attempt, links, plan, as_of=None):
    active = [link for link in links if not link['revoked']]
    facts = [link['snapshot'] for link in active]
    gaps = []
    if any(link['attribution'] != 'confirmed' for link in active):
        gaps.append('attribution_pending')
    if any(not fact.get('available', True) for fact in facts):
        gaps.append('source_unavailable')
    fills = [fact for fact in facts if fact['source_kind'] in ('trade', 'history')]
    orders = [fact for fact in facts if fact['source_kind'] in ('order', 'quick_order')]
    opening, closing = [], []
    direction = plan.get('direction')
    for fact in fills:
        kind = fact.get('trade_type')
        if kind == 'open_' + direction:
            opening.append(fact)
        elif kind == 'close_' + direction:
            closing.append(fact)
        else:
            gaps.append('trade_side_unknown')
    opened = sum((dec(f.get('quantity')) for f in opening), Decimal(0))
    closed = sum((dec(f.get('quantity')) for f in closing), Decimal(0))
    unresolved_orders = any(f.get('status') not in ('filled', 'cancelled', 'canceled', 'failed', 'rejected', 'expired', 'completed', 'executed') for f in orders)
    uncovered_orders = any(dec(f.get('filled_amount')) > 0 and (f['source_kind'] == 'quick_order' or
        sum((dec(fill.get('quantity')) for fill in fills if fill.get('pending_order_id') == f['source_id']), Decimal(0)) != dec(f['filled_amount'])) for f in orders)
    if unresolved_orders or uncovered_orders:
        gaps.append('order_facts_incomplete')
    if gaps:
        status = 'unknown'
    elif not fills:
        status = 'unknown' if any(f['source_kind'] == 'strategy' for f in facts) else ('no_fills' if orders else 'not_linked')
    elif opened <= 0 or closed > opened:
        status = 'unknown'
        gaps.append('fill_ownership_incomplete')
    else:
        status = 'closed' if opened == closed else ('partially_closed' if closed > 0 else 'holding')
    credible_times = bool(opening) and all(f.get('occurred_at') and f.get('event_quantity') is not None and
        not f.get('is_cumulative') and dec(f['event_quantity']) == dec(f.get('quantity')) and
        (as_of is None or timestamp(f['occurred_at']) <= as_of) for f in fills)
    entry_times = bool(opening) and all(f.get('occurred_at') and f.get('event_quantity') is not None and
        not f.get('is_cumulative') and dec(f['event_quantity']) == dec(f.get('quantity')) and
        (as_of is None or timestamp(f['occurred_at']) <= as_of) for f in opening)
    pretrade = 'unverifiable'
    if plan.get('retrospective') or attempt['risk_snapshot'].get('recorded_after_terminal'):
        pretrade = 'retrospective'
    elif entry_times:
        first = min(timestamp(f['occurred_at']) for f in opening)
        pretrade = 'verified' if timestamp(attempt['recorded_at']) < first else 'retrospective'
        if plan.get('expires_at') and timestamp(plan['expires_at']) <= first:
            pretrade = 'unverifiable'
    if pretrade != 'verified':
        gaps.append('pretrade_' + pretrade)
    risk = attempt['risk_snapshot']
    if risk.get('risk_status') != 'calculable' or not risk.get('one_r'):
        gaps.append('risk_unavailable')
    entry_avg = str(sum((dec(f['price']) * dec(f['quantity']) for f in opening), Decimal(0)) / opened) if opened else None
    exit_avg = str(sum((dec(f['price']) * dec(f['quantity']) for f in closing), Decimal(0)) / closed) if closed else None
    net, net_r = None, None
    if status != 'closed':
        gaps.append('fills_not_closed')
    if not credible_times:
        gaps.append('fill_time_unknown')
    instrument = plan.get('instrument', {})
    currency = risk.get('risk_currency')
    quote = instrument.get('settlement_ccy')
    if instrument.get('market') != 'Crypto' or instrument.get('market_type') != 'spot' or direction != 'long':
        gaps.append('funding_or_contract_costs_unverified')
    if not quote or currency != quote:
        gaps.append('cost_currency_unknown')
    fees = Decimal(0)
    for fact in fills:
        if fact.get('fee_status') not in ('actual', 'actual_zero'):
            gaps.append('fees_incomplete')
        elif fact.get('commission_quote') is not None:
            fees += dec(fact['commission_quote'])
        elif fact.get('commission_ccy') == quote:
            fees += dec(fact.get('commission'))
        elif fact.get('fee_status') != 'actual_zero':
            gaps.append('fee_currency_unknown')
    if credible_times:
        balance = Decimal(0)
        for fact in sorted(fills, key=lambda f: timestamp(f['occurred_at'])):
            balance += dec(fact['quantity']) * (1 if fact in opening else -1)
            if balance < 0:
                gaps.append('fill_sequence_invalid')
    if not gaps:
        net = str(sum((dec(f['price']) * dec(f['quantity']) for f in closing), Decimal(0)) -
                  sum((dec(f['price']) * dec(f['quantity']) for f in opening), Decimal(0)) - fees)
        net_r = str(dec(net) / dec(risk['one_r']))
    return {'trade_status': status, 'pretrade_evidence': pretrade, 'opened_quantity': str(opened),
            'closed_quantity': str(closed), 'actual_entry_price': entry_avg, 'actual_exit_price': exit_avg,
            'exit_reasons': list(dict.fromkeys(f.get('close_reason') for f in closing if f.get('close_reason'))),
            'net_profit': net, 'net_r': net_r, 'currency': currency,
            'gaps': list(dict.fromkeys(gaps)), 'phase': 'final' if status == 'closed' else 'stage'}
