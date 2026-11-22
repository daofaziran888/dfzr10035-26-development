"""业务路由汇总：固定路径的采集续跑路由在前，通用模块资源路由在后。"""
from __future__ import annotations

from app.routers import resource, resume

ROUTERS = [resume, resource]
