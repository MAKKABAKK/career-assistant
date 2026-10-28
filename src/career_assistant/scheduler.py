"""APScheduler 调度器（T03）：轮询 + 日报 + 反馈轮询 三个定时任务。

防呆三件套（架构第 8 节，缺一不可）：
- misfire_grace_time=300：任务错过触发点（如电脑睡眠）后 5 分钟内仍可补跑；
- max_instances=1：上一轮没跑完绝不叠加启动新一轮（防浏览器进程堆积）；
- coalesce=True：积压多次触发时合并为一次。
另一个铁律：任何任务异常都不允许让调度进程退出——统一由 _safe 包装兜底。
"""
from __future__ import annotations

from datetime import datetime

from apscheduler.schedulers.blocking import BlockingScheduler
from loguru import logger

from career_assistant.config import AppConfig
from career_assistant.models import HK_TZ

# 任务默认参数：所有 add_job 共用（写成 job_defaults 防止某个 add_job 忘配）
_JOB_DEFAULTS = {
    "misfire_grace_time": 300,
    "max_instances": 1,
    "coalesce": True,
}


def _safe(fn, name: str):
    """把任务函数包成"永不抛出"的版本——调度器只管触发，异常全部就地消化。"""
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            logger.error(f"调度任务 [{name}] 异常（已吞掉，进程继续运行）：{exc}")
            return None
    wrapper.__name__ = name   # APScheduler 要求 job 有合法函数名
    return wrapper


class Scheduler:
    """Shared monitoring trigger; execution stays in the durable worker."""

    def __init__(self, config, pipeline):
        self.config = config
        self.pipeline = pipeline

    def setup_monitoring_job(self, sched: BlockingScheduler) -> None:
        """监控轮询任务。next_run_time=now：启动后立即跑第一轮，
        不用干等一个完整间隔才见效果（对新用户的第一印象很重要）。"""
        sched.add_job(
            _safe(self.pipeline.run_monitoring_cycle, "monitoring"),
            trigger="interval",
            minutes=self.config.poll_interval_minutes,
            id="monitoring",
            next_run_time=datetime.now(HK_TZ),
        )
