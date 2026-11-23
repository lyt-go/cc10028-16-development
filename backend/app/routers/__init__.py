"""业务路由汇总：固定前缀路由先于通用 `/{module}` 资源路由注册。"""
from __future__ import annotations

from app.routers import layout_rerun, resource

ROUTERS = [layout_rerun, resource]
