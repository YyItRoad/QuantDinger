"""纪律关键事件的有限通知任务，不承担持仓轮询、下单或策略生命周期。"""

from app.celery_app import celery_app


@celery_app.task(name="quantdinger.tasks.trade_discipline_notice", ignore_result=True,
                 soft_time_limit=90, time_limit=120)
def trade_discipline_notice(user_id, notice_id):
    from app.trade_discipline.notice_delivery import deliver_notice

    return deliver_notice(int(user_id), int(notice_id))
