
"""MCP Gateway for the simulated Zaytoun Family Clinic.

Developer B — B1
Connects to the original clinic MCP server over stdio.
No clinic service files are modified.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
import re
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


logger = logging.getLogger(__name__)


# يسجل أربعة حقول من رفض العيادة فقط، مع تنقيح القيم الشخصية من الرسالة قبل تسجيلها.
def sanitized_clinic_error(response: dict, args: dict) -> dict:
    error = response.get("error") or {}
    message = str(error.get("message") or "Clinic rejected the request")
    raw = message.casefold()
    fields = [field for field in ("patient.name", "patient.phone", "patient.guardian_name", "patient.age", "slot_id", "service_id", "notes") if field in raw]
    if "guardian" in raw and "patient.guardian_name" not in fields:
        fields.append("patient.guardian_name")
    if "upcoming appointments" in raw or "maximum per patient" in raw:
        fields.extend(["patient.phone", "policies.max_upcoming_bookings_per_patient"])
    elif any(phrase in raw for phrase in ("in the past", "bookings open", "closed on", "does not work", "must start between")):
        fields.append("slot_id")
    def redact_values(data):
        nonlocal message
        for key, value in data.items():
            if isinstance(value, dict):
                redact_values(value)
            elif key in ("patient_name", "patient_phone", "guardian_name", "name", "phone", "notes") and isinstance(value, str) and value:
                message = re.sub(re.escape(value), "[redacted]", message, flags=re.I)
    redact_values(args)
    message = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[redacted]", message)
    message = re.sub(r"\+?\d[\d\s()./-]{6,}\d", "[redacted]", message)
    code = str(error.get("code") or "clinic_error")
    return {"status": response.get("status"), "code": code if re.fullmatch(r"[a-z_][a-z0-9_]{0,59}", code) else "clinic_error",
            "message": message[:500], "rejected_fields": sorted(set(fields))}


READ_TOOLS = frozenset({
    "clinic_info",
    "list_services",
    "find_slots",
    "get_patient",
    "get_booking",
})

WRITE_TOOLS = frozenset({
    "save_patient",
    "create_booking",
    "cancel_booking",
})


class ClinicGateway:
    """Access clinic tools using the original MCP server."""

    def __init__(self, timeout: float = 20.0):
        self.timeout = timeout
        self.tool_calls: list[dict[str, Any]] = []

    async def call_read(
        self, tool_name: str, args: dict
    ) -> dict:
        """Call an approved read-only MCP tool."""
        if tool_name not in READ_TOOLS:
            raise ValueError(
                f"Tool '{tool_name}' is not an allowed read tool"
            )

        return await self._call_tool(tool_name, args)

    async def call_write(
        self, tool_name: str, args: dict
    ) -> dict:
        """Write transport for the trusted Approval Handler only.

        The LLM and Scheduler must never be given direct
        permission to call this method.
        """
        if tool_name not in WRITE_TOOLS:
            raise ValueError(
                f"Tool '{tool_name}' is not an allowed write tool"
            )

        return await self._call_tool(tool_name, args)

    async def _call_tool(
        self, tool_name: str, args: dict
    ) -> dict:
        started = time.perf_counter()
        success = False

        try:
            if not isinstance(args, dict):
                raise TypeError("MCP arguments must be a dictionary")

            params = StdioServerParameters(
                command=sys.executable,
                args=["-m", "services.clinic_mcp"],
                env=os.environ.copy(),
            )

            async def execute() -> dict:
                async with stdio_client(params) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()

                        result = await session.call_tool(
                            tool_name,
                            arguments=args,
                        )

                        if result.structuredContent is not None:
                            data = result.structuredContent
                        else:
                            data = self._parse_content(result.content)

                        if not isinstance(data, dict):
                            data = {"data": data}

                        if result.isError:
                            data["ok"] = False
                            data.setdefault(
                                "error",
                                {
                                    "code": "mcp_tool_error",
                                    "message": "MCP tool returned an error",
                                },
                            )

                        return data

            response = await asyncio.wait_for(
                execute(), timeout=self.timeout
            )

            success = response.get("ok") is True
            if not success and response.get("status") in (400, 422):
                detail = sanitized_clinic_error(response, args)
                logger.warning("clinic_validation_error status=%s code=%s message=%s rejected_fields=%s",
                               detail["status"], detail["code"], detail["message"], detail["rejected_fields"])
            return response

        except asyncio.TimeoutError:
            return {
                "ok": False,
                "status": 503,
                "error": {
                    "code": "gateway_timeout",
                    "message": "Clinic MCP request timed out",
                    "retryable": True,
                },
            }

        except Exception as exc:
            # نوع الخطأ واسم الأداة فقط؛ traceback قد يحتوي URL هاتف أو بيانات حساسة.
            logger.warning("MCP request failed tool=%s type=%s", tool_name, type(exc).__name__)

            return {
                "ok": False,
                "status": 503,
                "error": {
                    "code": "gateway_error",
                    "message": (
                        "Clinic MCP connection failed: "
                        f"{type(exc).__name__}"
                    ),
                    "retryable": True,
                },
            }

        finally:
            duration_ms = round(
                (time.perf_counter() - started) * 1000, 2
            )

            record = {
                "tool": tool_name,
                "success": success,
                "duration_ms": duration_ms,
            }

            self.tool_calls.append(record)

            logger.info(
                "MCP tool=%s success=%s duration_ms=%.2f",
                tool_name,
                success,
                duration_ms,
            )

    @staticmethod
    def _parse_content(content: list) -> dict:
        """Parse text blocks when structuredContent is absent."""
        for block in content:
            text = getattr(block, "text", None)

            if text:
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return {"ok": False, "raw": text}

        return {
            "ok": False,
            "error": {
                "code": "empty_response",
                "message": "MCP returned no readable content",
            },
        }
