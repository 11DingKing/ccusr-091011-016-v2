"""
可由管理命令或进程内调度器调用的周期任务。
"""
import logging
from .services import sweep_expired_delegations

logger = logging.getLogger('apps')


def process_expired_delegations():
    """
    处理到期的代理授权 - 定时任务
    每小时执行一次，到期授权的未完成事项退回原审批人
    """
    logger.info("开始执行代理授权到期处理定时任务...")

    processed = sweep_expired_delegations()

    logger.info(f"代理授权到期处理完成，共处理: {processed} 条")
