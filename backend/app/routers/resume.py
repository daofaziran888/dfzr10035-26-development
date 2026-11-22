"""采集续跑专用路由：响应解析缺字段续跑、投递待办、接收确认与补齐重定位。

与通用资源路由分开注册，路径前缀 /api/resume 为固定路径，
在 main.py 中先于 /api/{module} 资源路由挂载，避免被动态路径抢占。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.services.resume_delivery import resume_delivery_service

router = APIRouter(prefix="/api/resume", tags=["采集续跑"])


class FieldMapping(BaseModel):
    field: str
    path: str
    required: bool = False
    allow_null: bool = False


class IntakePayload(BaseModel):
    request_no: str
    endpoint: str = ""
    rows: list[dict[str, Any]] = Field(default_factory=list)
    mappings: list[FieldMapping]


class InterruptPayload(BaseModel):
    reason: str = "接收方中断"


class AckPayload(BaseModel):
    receiver: str = "接收方"
    receipt_no: str | None = None


class RejectPayload(BaseModel):
    reason: str
    receiver: str = "接收方"


class SupplementPayload(BaseModel):
    field: str
    value: Any = None
    operator: str = "补齐人"


@router.post("/intake")
def intake(payload: IntakePayload) -> dict[str, Any]:
    """解析一次采集响应，产出续跑明细、投递待办和解析问题清单。"""
    return resume_delivery_service.intake_collection(
        request_no=payload.request_no,
        endpoint=payload.endpoint,
        rows=payload.rows,
        mappings=[m.model_dump() for m in payload.mappings],
    )


@router.post("/requests/{request_no}/start")
def start_delivery(request_no: str) -> dict[str, Any]:
    """启动投递：实际发出所有可交付且未确认的行。"""
    return resume_delivery_service.start_delivery(request_no)


@router.post("/requests/{request_no}/interrupt")
def interrupt_delivery(request_no: str, payload: InterruptPayload) -> dict[str, Any]:
    """标记投递中断，保留确认状态用于后续重试。"""
    return resume_delivery_service.interrupt_delivery(request_no, payload.reason)


@router.post("/requests/{request_no}/retry")
def retry_delivery(request_no: str) -> dict[str, Any]:
    """中断后重试：仅重发未确认行，已确认行不重复交付。"""
    return resume_delivery_service.retry_delivery(request_no)


@router.post("/requests/{request_no}/rows/{row_key}/acknowledge")
def acknowledge(request_no: str, row_key: str, payload: AckPayload) -> dict[str, Any]:
    """接收方确认一行；重复确认幂等，只保留一条回执。"""
    return resume_delivery_service.acknowledge_row(
        request_no, row_key, receiver=payload.receiver, receipt_no=payload.receipt_no)


@router.post("/requests/{request_no}/rows/{row_key}/reject")
def reject(request_no: str, row_key: str, payload: RejectPayload) -> dict[str, Any]:
    """接收方拒收一行并记录原因；拒收行不再自动投递。"""
    return resume_delivery_service.reject_row(
        request_no, row_key, reason=payload.reason, receiver=payload.receiver)


@router.post("/requests/{request_no}/rows/{row_key}/supplement")
def supplement(request_no: str, row_key: str, payload: SupplementPayload) -> dict[str, Any]:
    """按原请求+原响应行定位补交字段，核验通过后自动投递该行。"""
    return resume_delivery_service.supplement_field(
        request_no, row_key, payload.field, payload.value, operator=payload.operator)


@router.post("/requests/{request_no}/reset")
def reset_request(request_no: str) -> dict[str, Any]:
    """清除单个请求的链路数据，供测试与重跑隔离使用。"""
    return resume_delivery_service.reset_request(request_no)


@router.get("/requests/{request_no}/ops-view")
def ops_view(request_no: str) -> dict[str, Any]:
    """运维视图：异常行、缺字段、补录记录、重试次数、最终状态与事件轨迹。"""
    return resume_delivery_service.ops_view(request_no)


@router.get("/requests/{request_no}/receiver-results")
def receiver_results(request_no: str) -> dict[str, Any]:
    """接收结果：确认/未确认/阻断行与全部确认回执。"""
    return resume_delivery_service.receiver_results(request_no)


@router.get("/requests/{request_no}/parse-issues")
def parse_issues(request_no: str) -> dict[str, Any]:
    """解析问题清单：原始响应行、未通过字段、补齐历史与当前状态。"""
    return resume_delivery_service.parse_issues(request_no)
