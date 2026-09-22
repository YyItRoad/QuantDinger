"""行情分析模块对外请求错误。"""


class RequestValidationError(ValueError):
    """客户端请求不符合行情分析接口约定。"""


class TaskConflictError(ValueError):
    """同一用户已经存在相同的有效分析任务。"""
