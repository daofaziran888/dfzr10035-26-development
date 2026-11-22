"""采集续跑领域服务。

承载「响应解析异常 → 缺字段续跑 → 投递待办 → 接收确认 → 补齐重定位」全链路状态，
独立于通用模块服务（BaseModuleService），由专用路由 /api/resume/* 调用。

核心约束（与业务题面对齐）：
- 明确的 null 是有值：映射允许空值时保留 null，不得当作缺字段或零额。
- JSON 路径找不到（键缺失）与显式 null 严格区分；找不到路径的行必须保留原响应行
  和未通过的字段，且在字段核验通过前不得作为零额记录交付。
- 接收方已确认的行不可重复投递；续跑明细/补字段只能沿原请求与原响应行重新定位，
  不重新整批解析，因此已确认数据（含 B 的 null）永不被改写。
- 续跑明细随可交付数据一起写进投递待办；接收确认结果同步回解析问题清单。
"""
from __future__ import annotations

from typing import Any

from app.store import store

# 缺值哨兵：区分「路径不存在」(MISSING) 与「显式 null」(None)。
MISSING = object()

# 明细/待办/回执的行状态
ROW_PENDING = "待确认"          # 已投递、等待接收方确认
ROW_BLOCKED = "缺字段待补齐"    # 未核验，禁止交付
ROW_DELIVERABLE = "待投递"      # 已核验、等待进入投递
ROW_CONFIRMED = "已确认"
ROW_REJECTED = "已拒收"         # 接收方明确拒收，不再自动投递

# 问题清单状态
ISSUE_OPEN = "待补齐"
ISSUE_SUPPLEMENTED = "已补齐待核验"
ISSUE_RESOLVED = "已解决"

# 采集请求状态
REQ_OPEN = "待投递"
REQ_DELIVERING = "投递中"
REQ_INTERRUPTED = "投递中断"
REQ_DONE = "投递完成"


class ResumeDeliveryService:
    """采集续跑与投递状态机；所有状态只在本服务内流转。"""

    # ---------- 基础工具 ----------

    @staticmethod
    def _next_id(table: str) -> int:
        return max((int(r.get("id", 0)) for r in store.rows(table)), default=0) + 1

    @staticmethod
    def _extract_path(payload: Any, path: str) -> Any:
        """按点分 JSON 路径取值；任一键不存在返回 MISSING 哨兵，显式 null 返回 None。"""
        current = payload
        for part in str(path).split("."):
            if isinstance(current, dict) and part in current:
                current = current[part]
                continue
            return MISSING
        return current

    @staticmethod
    def _null_fields(data: dict[str, Any]) -> list[str]:
        """值显式为 null（键存在且值为 None）的字段，用于视图展示 null 保留情况。"""
        return sorted(key for key, value in data.items() if value is None)

    def _log_event(
        self,
        request_no: str,
        row_key: str | None,
        action: str,
        from_status: str | None,
        to_status: str | None,
        **extra: Any,
    ) -> None:
        """追加式状态事件日志：只增不改，供两个视图追溯每一行的状态变化。"""
        events = store.rows("resume_event")
        events.append({
            "id": self._next_id("resume_event"),
            "seq": len(events) + 1,
            "request_no": request_no,
            "row_key": row_key,
            "action": action,
            "from_status": from_status,
            "to_status": to_status,
            "extra": extra,
        })

    # ---------- 第一步：采集解析，产出续跑明细 + 投递待办 + 问题清单 ----------

    def intake_collection(
        self,
        *,
        request_no: str,
        endpoint: str,
        rows: list[dict[str, Any]],
        mappings: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """解析一次采集响应（多行），生成请求、续跑明细、投递待办和问题清单。

        mappings 每项：{"field": 输出字段, "path": 源 JSON 路径,
                       "required": 是否必填, "allow_null": 映射是否允许空值}
        每个映射字段在一行上有三种取值结果：
        - 正常有值（含 0）：直接映射；
        - 显式 null 且 allow_null：保留 null，视为已核验；
        - 显式 null 但不允许空值，或路径找不到：记解析问题，行被阻断。
        """
        req_table = store.rows("resume_request")
        if any(r.get("request_no") == request_no for r in req_table):
            return {"ok": False, "message": f"采集请求 {request_no} 已存在，禁止重复建批"}
        request_id = self._next_id("resume_request")
        req = {
            "id": request_id,
            "request_no": request_no,
            "endpoint": endpoint,
            "mappings": [dict(m) for m in mappings],
            "status": REQ_OPEN,
        }
        req_table.append(req)

        detail_table = store.rows("resume_detail")
        issue_table = store.rows("parse_issue")
        todo_table = store.rows("delivery_todo")

        results: list[dict[str, Any]] = []
        for line_no, raw_row in enumerate(rows, start=1):
            row_key = str(raw_row.get("row_id") or chr(ord("A") + line_no - 1))
            mapped: dict[str, Any] = {}
            failed_fields: list[dict[str, str]] = []
            for m in mappings:
                field, path = m["field"], m["path"]
                required = bool(m.get("required"))
                allow_null = bool(m.get("allow_null"))
                value = self._extract_path(raw_row, path)

                if value is MISSING:
                    # 找不到路径：记录未通过字段，行不得按零额/默认值交付。
                    failed_fields.append({
                        "field": field, "path": path, "reason": "路径不存在",
                    })
                    continue
                if value is None:
                    if allow_null:
                        mapped[field] = None  # 明确空值按映射保留
                    else:
                        failed_fields.append({
                            "field": field, "path": path, "reason": "空值未被映射允许",
                        })
                    continue
                mapped[field] = value

            # 必填字段最终缺失（路径找不到，或 null 未被允许）即阻断该行。
            unresolved = [
                f for f in failed_fields
                if any(mm.get("required") and mm["field"] == f["field"] for mm in mappings)
            ]
            blocked = bool(unresolved)
            detail_id = self._next_id("resume_detail")
            detail = {
                "id": detail_id,
                "request_id": request_id,
                "request_no": request_no,
                "line_no": line_no,
                "row_key": row_key,
                # 原响应行原样留存，供补字段时按原请求+原行重新定位，永不改写。
                "raw_response_row": dict(raw_row),
                "mapped_data": dict(mapped),
                "failed_fields": [dict(f) for f in failed_fields],
                "status": ROW_BLOCKED if blocked else ROW_DELIVERABLE,
                "verified": not blocked,
            }
            detail_table.append(detail)

            if blocked:
                issue_table.append({
                    "id": self._next_id("parse_issue"),
                    "request_id": request_id,
                    "request_no": request_no,
                    "line_no": line_no,
                    "row_key": row_key,
                    "detail_id": detail_id,
                    "raw_response_row": dict(raw_row),
                    "failed_fields": [dict(f) for f in unresolved],
                    "status": ISSUE_OPEN,
                    "supplement_history": [],
                })
            else:
                # 续跑明细随可交付数据写进投递待办；被阻断的行不进待办。
                todo_table.append({
                    "id": self._next_id("delivery_todo"),
                    "request_id": request_id,
                    "request_no": request_no,
                    "line_no": line_no,
                    "row_key": row_key,
                    "detail_id": detail_id,
                    "payload": dict(mapped),
                    "deliver_attempts": 0,
                    "status": ROW_DELIVERABLE,
                })
            results.append({
                "row_key": row_key,
                "line_no": line_no,
                "status": detail["status"],
                "mapped_data": detail["mapped_data"],
                "failed_fields": detail["failed_fields"],
            })
            self._log_event(
                request_no, row_key, "采集解析",
                None, detail["status"],
                line_no=line_no,
                null_fields=self._null_fields(mapped),
                failed_fields=[dict(f) for f in unresolved],
                raw_response_row=dict(raw_row),
            )

        return {
            "ok": True,
            "message": f"采集请求 {request_no} 解析完成，共 {len(rows)} 行",
            "request": req,
            "rows": results,
        }

    # ---------- 投递：只发可交付且未确认的行，已确认行天然排除 ----------

    def _flush_todo(self, request_id: int, *, redeliver_inflight: bool) -> list[dict[str, Any]]:
        """发出请求下的待办行。

        - 默认只发「待投递」（从未发出、或补齐后新就绪）的行；
        - redeliver_inflight=True（中断重试）时，连「待确认」的在途行一并重发，
          因为中断意味着上一次发出未必到达接收方；
        - 「已确认」行任何情况下都不在选取范围内，保证不重复交付。
        """
        sent: list[dict[str, Any]] = []
        for todo in store.rows("delivery_todo"):
            if todo.get("request_id") != request_id:
                continue
            status = todo.get("status")
            if status in (ROW_CONFIRMED, ROW_REJECTED):
                continue  # 已确认/已拒收行不可重复交付
            if status == ROW_PENDING and not redeliver_inflight:
                continue  # 已在途等待确认：仅显式重试才可重发
            todo["status"] = ROW_PENDING
            todo["deliver_attempts"] = int(todo.get("deliver_attempts", 0)) + 1
            sent.append(dict(todo))
            detail = store.find("resume_detail", int(todo["detail_id"]))
            if detail is not None:
                detail["status"] = ROW_PENDING
            self._log_event(
                todo["request_no"], todo["row_key"], "实际投递",
                ROW_DELIVERABLE, ROW_PENDING,
                attempt=todo["deliver_attempts"],
                redelivery=redeliver_inflight,
                payload=dict(todo["payload"]),
            )
        return sent

    def start_delivery(self, request_no: str) -> dict[str, Any]:
        req = self._find_request(request_no)
        if req is None:
            return {"ok": False, "message": f"采集请求 {request_no} 不存在"}
        prev_status = req.get("status")
        sent = self._flush_todo(int(req["id"]), redeliver_inflight=False)
        req["status"] = REQ_DELIVERING
        self._log_event(request_no, None, "启动投递", prev_status, REQ_DELIVERING,
                        sent_count=len(sent))
        return {
            "ok": True,
            "message": f"请求 {request_no} 启动投递，实际发出 {len(sent)} 行（已确认行已排除）",
            "request_no": request_no,
            "sent_rows": sent,
        }

    def interrupt_delivery(self, request_no: str, reason: str = "接收方中断") -> dict[str, Any]:
        req = self._find_request(request_no)
        if req is None:
            return {"ok": False, "message": f"采集请求 {request_no} 不存在"}
        prev_status = req.get("status")
        req["status"] = REQ_INTERRUPTED
        req["interrupt_reason"] = reason
        self._log_event(request_no, None, "投递中断", prev_status, REQ_INTERRUPTED, reason=reason)
        return {"ok": True, "message": f"请求 {request_no} 投递中断：{reason}"}

    def retry_delivery(self, request_no: str) -> dict[str, Any]:
        """中断后重试：重发在途待确认行与新就绪行，已确认/已拒收行绝不重复交付。"""
        req = self._find_request(request_no)
        if req is None:
            return {"ok": False, "message": f"采集请求 {request_no} 不存在"}
        if req.get("status") not in (REQ_INTERRUPTED, REQ_DELIVERING):
            return {"ok": False, "message": f"请求 {request_no} 当前状态 {req.get('status')}，不可重试"}
        prev_status = req.get("status")
        before = {
            t["row_key"]: int(t.get("deliver_attempts", 0))
            for t in store.rows("delivery_todo") if t.get("request_no") == request_no
        }
        sent = self._flush_todo(int(req["id"]), redeliver_inflight=True)
        req["status"] = REQ_DELIVERING
        self._log_event(request_no, None, "重试投递", prev_status, REQ_DELIVERING,
                        sent_count=len(sent), attempts_before=before)
        return {
            "ok": True,
            "message": f"请求 {request_no} 重试投递，重发 {len(sent)} 行，已确认行未重复发送",
            "request_no": request_no,
            "sent_rows": sent,
            "attempts_before": before,
        }

    # ---------- 接收确认：回执幂等，结果同步解析问题清单 ----------

    def acknowledge_row(
        self,
        request_no: str,
        row_key: str,
        *,
        receiver: str = "接收方",
        receipt_no: str | None = None,
    ) -> dict[str, Any]:
        req = self._find_request(request_no)
        if req is None:
            return {"ok": False, "message": f"采集请求 {request_no} 不存在"}
        todo = self._find_todo(int(req["id"]), row_key)
        if todo is None:
            return {"ok": False, "message": f"行 {row_key} 不在投递待办（可能尚未补齐核验），无法确认"}
        if todo.get("status") == ROW_REJECTED:
            return {"ok": False, "message": f"行 {row_key} 已被拒收，不能确认（须先解除拒收）"}

        receipts = store.rows("delivery_receipt")
        existing = next(
            (r for r in receipts
             if r.get("request_no") == request_no and r.get("row_key") == row_key),
            None,
        )
        if existing is not None:
            # 幂等：重复确认只回原回执，不产生第二条确认、不重复计数。
            return {
                "ok": True,
                "message": f"行 {row_key} 已确认过，本次为重复确认，回执保持唯一",
                "duplicate": True,
                "receipt": existing,
            }

        receipt = {
            "id": self._next_id("delivery_receipt"),
            "receipt_no": receipt_no or f"RCPT-{request_no}-{row_key}",
            "request_id": int(req["id"]),
            "request_no": request_no,
            "line_no": todo["line_no"],
            "row_key": row_key,
            "detail_id": todo["detail_id"],
            # 回执快照来自实际投递载荷；后续补字段/重解析都不能改写它。
            "confirmed_payload": dict(todo["payload"]),
            "receiver": receiver,
        }
        receipts.append(receipt)
        todo["status"] = ROW_CONFIRMED
        detail = store.find("resume_detail", int(todo["detail_id"]))
        prev_status = detail.get("status") if detail is not None else None
        if detail is not None:
            detail["status"] = ROW_CONFIRMED

        # 接收确认结果同步到解析问题清单：该行若曾有问题则标记解决。
        for issue in store.rows("parse_issue"):
            if issue.get("request_no") == request_no and issue.get("row_key") == row_key:
                issue["status"] = ISSUE_RESOLVED
                issue["resolved_receipt"] = receipt["receipt_no"]

        self._log_event(request_no, row_key, "接收确认", prev_status, ROW_CONFIRMED,
                        receipt_no=receipt["receipt_no"], receiver=receiver,
                        null_fields=self._null_fields(receipt["confirmed_payload"]))
        self._maybe_complete(req)
        return {
            "ok": True,
            "message": f"行 {row_key} 已由{receiver}确认，回执 {receipt['receipt_no']}",
            "duplicate": False,
            "receipt": receipt,
        }

    def reject_row(
        self,
        request_no: str,
        row_key: str,
        *,
        reason: str,
        receiver: str = "接收方",
    ) -> dict[str, Any]:
        """接收方明确拒收：标记行不再自动投递，拒收行与原因进入接收视图和事件日志。"""
        req = self._find_request(request_no)
        if req is None:
            return {"ok": False, "message": f"采集请求 {request_no} 不存在"}
        if not str(reason or "").strip():
            return {"ok": False, "message": "拒收必须填写原因"}
        todo = self._find_todo(int(req["id"]), row_key)
        if todo is None:
            return {"ok": False, "message": f"行 {row_key} 不在投递待办，无法拒收"}
        if todo.get("status") == ROW_CONFIRMED:
            return {"ok": False, "message": f"行 {row_key} 已确认，不可再拒收"}

        prev_status = todo.get("status")
        todo["status"] = ROW_REJECTED
        todo["reject_reason"] = reason
        detail = store.find("resume_detail", int(todo["detail_id"]))
        if detail is not None:
            detail["status"] = ROW_REJECTED
        self._log_event(request_no, row_key, "接收拒收", prev_status, ROW_REJECTED,
                        reason=reason, receiver=receiver)
        return {
            "ok": True,
            "message": f"行 {row_key} 已被{receiver}拒收：{reason}（不再自动投递）",
            "row_key": row_key,
            "status": ROW_REJECTED,
        }

    # ---------- 补字段：沿原请求 + 原响应行重新定位，不动已确认数据 ----------

    def supplement_field(
        self,
        request_no: str,
        row_key: str,
        field: str,
        value: Any,
        *,
        operator: str = "补齐人",
    ) -> dict[str, Any]:
        req = self._find_request(request_no)
        if req is None:
            return {"ok": False, "message": f"采集请求 {request_no} 不存在"}
        detail = next(
            (d for d in store.rows("resume_detail")
             if d.get("request_no") == request_no and d.get("row_key") == row_key),
            None,
        )
        if detail is None:
            return {"ok": False, "message": f"行 {row_key} 不存在于请求 {request_no}"}
        if detail.get("status") == ROW_CONFIRMED:
            # 收货方已确认的数据不可改写。
            return {"ok": False, "message": f"行 {row_key} 已确认，禁止改写已交付数据"}
        if detail.get("status") == ROW_REJECTED:
            return {"ok": False, "message": f"行 {row_key} 已被拒收，须先由接收方解除拒收才能补录"}

        mapping = next((m for m in req["mappings"] if m["field"] == field), None)
        if mapping is None:
            return {"ok": False, "message": f"字段 {field} 不在请求 {request_no} 的映射内"}
        if field in detail["mapped_data"]:
            return {"ok": False, "message": f"行 {row_key} 的字段 {field} 已有值（含显式 null），不得覆盖"}
        if value is None and not mapping.get("allow_null"):
            return {"ok": False, "message": f"字段 {field} 的映射不允许空值，补交 null 不予核验"}

        # 补来的字段沿原请求与原响应行定位：记录定位坐标与原始值，不重解析整批。
        detail["mapped_data"][field] = value
        detail["failed_fields"] = [f for f in detail["failed_fields"] if f["field"] != field]

        issue = next(
            (i for i in store.rows("parse_issue")
             if i.get("request_no") == request_no and i.get("row_key") == row_key),
            None,
        )
        supplement_record = {
            "field": field,
            "path": mapping["path"],
            "value": value,
            "operator": operator,
            "located_by": {
                "request_no": request_no,
                "line_no": detail["line_no"],
                "row_key": row_key,
                "raw_response_row": dict(detail["raw_response_row"]),
            },
        }
        if issue is not None:
            issue["failed_fields"] = [f for f in issue["failed_fields"] if f["field"] != field]
            issue["supplement_history"].append(supplement_record)

        # 用原映射重新核验该行：仍有必填字段未通过则继续阻断。
        still_missing = {f["field"] for f in detail["failed_fields"]}
        required_fields = {m["field"] for m in req["mappings"] if m.get("required")}
        if required_fields & still_missing:
            if issue is not None:
                issue["status"] = ISSUE_SUPPLEMENTED
            self._log_event(request_no, row_key, "字段补录",
                            ROW_BLOCKED, ROW_BLOCKED, field=field, value=value,
                            note="仍有必填字段缺失，保持阻断")
            return {
                "ok": True,
                "message": f"行 {row_key} 已补交 {field}，仍有必填字段缺失，保持阻断",
                "detail": dict(detail),
            }

        # 核验通过：解除阻断，按原行坐标新写入投递待办，进入下一次投递
        # （本接口不实际发出，避免补录动作隐式触发投递、也保证在途行不被重复发送）。
        detail["status"] = ROW_DELIVERABLE
        detail["verified"] = True
        if issue is not None:
            issue["status"] = ISSUE_SUPPLEMENTED
        store.rows("delivery_todo").append({
            "id": self._next_id("delivery_todo"),
            "request_id": int(req["id"]),
            "request_no": request_no,
            "line_no": detail["line_no"],
            "row_key": row_key,
            "detail_id": detail["id"],
            "payload": dict(detail["mapped_data"]),
            "deliver_attempts": 0,
            "status": ROW_DELIVERABLE,
        })
        self._log_event(
            request_no, row_key, "字段补录",
            ROW_BLOCKED, ROW_DELIVERABLE,
            field=field, value=value, operator=operator,
            located_by=supplement_record["located_by"],
            note="已进入下一次投递待办，原始响应行未改写",
        )
        return {
            "ok": True,
            "message": f"行 {row_key} 补齐 {field} 并核验通过，已进入下一次投递待办",
            "detail": dict(detail),
        }

    # ---------- 查询：接收方视图 / 运维视图 / 问题清单 ----------

    def receiver_results(self, request_no: str) -> dict[str, Any]:
        """接收方视图：已确认/未确认/拒收行、每条回执与 null 字段保留情况。"""
        req = self._find_request(request_no)
        if req is None:
            return {"ok": False, "message": f"采集请求 {request_no} 不存在"}
        receipts = [
            dict(r) for r in store.rows("delivery_receipt")
            if r.get("request_no") == request_no
        ]
        todos = [t for t in store.rows("delivery_todo") if t.get("request_no") == request_no]
        confirmed = {r["row_key"] for r in receipts}
        rejected_rows = sorted(t["row_key"] for t in todos if t.get("status") == ROW_REJECTED)
        unconfirmed = sorted(
            t["row_key"] for t in todos
            if t["row_key"] not in confirmed and t.get("status") != ROW_REJECTED
        )
        details = [d for d in store.rows("resume_detail") if d.get("request_no") == request_no]
        blocked = sorted(d["row_key"] for d in details if d.get("status") == ROW_BLOCKED)

        row_view = []
        todo_by_key = {t["row_key"]: t for t in todos}
        for detail in sorted(details, key=lambda d: d["line_no"]):
            key = detail["row_key"]
            todo = todo_by_key.get(key)
            row_view.append({
                "row_key": key,
                "line_no": detail["line_no"],
                "status": detail.get("status"),
                "null_fields": self._null_fields(detail.get("mapped_data", {})),
                "deliver_attempts": int(todo.get("deliver_attempts", 0)) if todo else 0,
                "reject_reason": todo.get("reject_reason") if todo else None,
                "receipt_no": next(
                    (r["receipt_no"] for r in receipts if r["row_key"] == key), None),
            })
        return {
            "ok": True,
            "request_no": request_no,
            "request_status": req.get("status"),
            "confirmed_rows": sorted(confirmed),
            "unconfirmed_rows": unconfirmed,
            "rejected_rows": rejected_rows,
            "blocked_rows": blocked,
            "row_view": row_view,
            "receipts": receipts,
            "delivery_todo": [dict(t) for t in todos],
            "summary": {
                "receipt_count": len(receipts),
                "confirmed_count": len(confirmed),
                "unconfirmed_count": len(unconfirmed),
                "rejected_count": len(rejected_rows),
                "blocked_count": len(blocked),
            },
        }

    def ops_view(self, request_no: str) -> dict[str, Any]:
        """运维视图：异常行、缺失字段、补录记录、每行重试次数与最终状态、完整事件轨迹。"""
        req = self._find_request(request_no)
        if req is None:
            return {"ok": False, "message": f"采集请求 {request_no} 不存在"}
        rid = int(req["id"])
        details = sorted(
            (d for d in store.rows("resume_detail") if d.get("request_id") == rid),
            key=lambda d: d["line_no"],
        )
        todos = {t["row_key"]: t for t in store.rows("delivery_todo") if t.get("request_id") == rid}
        issues = {i["row_key"]: i for i in store.rows("parse_issue") if i.get("request_id") == rid}
        events = [dict(e) for e in store.rows("resume_event") if e.get("request_no") == request_no]

        rows = []
        for detail in details:
            key = detail["row_key"]
            todo = todos.get(key)
            issue = issues.get(key)
            rows.append({
                "row_key": key,
                "line_no": detail["line_no"],
                "final_status": detail.get("status"),
                "verified": bool(detail.get("verified")),
                "missing_fields": [f["field"] for f in detail.get("failed_fields", [])],
                "null_fields": self._null_fields(detail.get("mapped_data", {})),
                "deliver_attempts": int(todo.get("deliver_attempts", 0)) if todo else 0,
                "raw_response_row_immutable": dict(detail.get("raw_response_row", {})),
                "supplement_records": list(issue.get("supplement_history", [])) if issue else [],
            })
        return {
            "ok": True,
            "request_no": request_no,
            "endpoint": req.get("endpoint"),
            "request_status": req.get("status"),
            # 异常行以持久化问题清单为准：即使字段补齐、问题已解决，该行仍计入历史异常。
            "abnormal_rows": sorted(issues.keys()),
            "rows": rows,
            "events": events,
            "summary": {
                "total_rows": len(rows),
                "abnormal_count": len(issues),
                "supplemented_count": sum(1 for r in rows if r["supplement_records"]),
                "max_deliver_attempts": max((r["deliver_attempts"] for r in rows), default=0),
                "event_count": len(events),
            },
        }

    def parse_issues(self, request_no: str) -> dict[str, Any]:
        req = self._find_request(request_no)
        if req is None:
            return {"ok": False, "message": f"采集请求 {request_no} 不存在"}
        issues = [
            dict(i) for i in store.rows("parse_issue")
            if i.get("request_no") == request_no
        ]
        outstanding = [i for i in issues if i["status"] != ISSUE_RESOLVED]
        return {
            "ok": True,
            "request_no": request_no,
            "request_status": req.get("status"),
            "issues": issues,
            "outstanding_row_keys": sorted(i["row_key"] for i in outstanding),
            "summary": {
                "total": len(issues),
                "open": sum(1 for i in issues if i["status"] == ISSUE_OPEN),
                "supplemented": sum(1 for i in issues if i["status"] == ISSUE_SUPPLEMENTED),
                "resolved": sum(1 for i in issues if i["status"] == ISSUE_RESOLVED),
            },
        }

    # ---------- 内部辅助 ----------

    def reset_request(self, request_no: str) -> dict[str, Any]:
        """按请求编号清除该请求的全部链路数据（测试/重跑隔离用，不动其他请求）。"""
        req = self._find_request(request_no)
        if req is None:
            return {"ok": False, "message": f"采集请求 {request_no} 不存在"}
        rid = int(req["id"])
        for table, key in (
            ("resume_detail", "request_id"),
            ("delivery_todo", "request_id"),
            ("delivery_receipt", "request_id"),
            ("parse_issue", "request_id"),
        ):
            store.rows(table)[:] = [
                row for row in store.rows(table) if row.get(key) != rid
            ]
        store.rows("resume_event")[:] = [
            row for row in store.rows("resume_event") if row.get("request_no") != request_no
        ]
        store.rows("resume_request")[:] = [
            row for row in store.rows("resume_request") if row.get("request_no") != request_no
        ]
        return {"ok": True, "message": f"采集请求 {request_no} 链路数据已清除"}

    def _find_request(self, request_no: str) -> dict[str, Any] | None:
        return next(
            (r for r in store.rows("resume_request") if r.get("request_no") == request_no),
            None,
        )

    def _find_todo(self, request_id: int, row_key: str) -> dict[str, Any] | None:
        return next(
            (t for t in store.rows("delivery_todo")
             if t.get("request_id") == request_id and t.get("row_key") == row_key),
            None,
        )

    def _maybe_complete(self, req: dict[str, Any]) -> None:
        """全部明细核验、且所有待办行到达终态（已确认/已拒收）后，请求自动完成。"""
        rid = int(req["id"])
        details = [d for d in store.rows("resume_detail") if d.get("request_id") == rid]
        todos = [t for t in store.rows("delivery_todo") if t.get("request_id") == rid]
        if not details or any(not d.get("verified") for d in details):
            return
        terminal = (ROW_CONFIRMED, ROW_REJECTED)
        if todos and all(t.get("status") in terminal for t in todos):
            prev = req.get("status")
            req["status"] = REQ_DONE
            self._log_event(req["request_no"], None, "请求完成", prev, REQ_DONE)


resume_delivery_service = ResumeDeliveryService()
