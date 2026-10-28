
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
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


logger = logging.getLogger(__name__)


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
            logger.exception(
                "MCP request failed: %s", tool_name
            )

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
