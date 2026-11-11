"""RFC 7807 error handling (API-001).

A single set of exception handlers guarantees every error — raised `ProblemException`, plain
`HTTPException`, or request-validation failure — is returned as `application/problem+json` with
the correlating `request_id` (API-007). Never fail silently (P6).
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.auth.passwords import WeakPasswordError
from app.auth.service import AuthError
from app.billing.budget import BudgetExceededError
from app.schemas.common import FieldError, Problem
from app.utils.logging import get_logger, log_event

logger = get_logger(__name__)

PROBLEM_MEDIA_TYPE = "application/problem+json"


class ProblemException(Exception):
    """Raise to return an RFC 7807 problem response."""

    def __init__(
        self,
        status_code: int,
        title: str,
        detail: str | None = None,
        *,
        type: str = "about:blank",
        errors: list[FieldError] | None = None,
    ) -> None:
        self.status_code = status_code
        self.title = title
        self.detail = detail
        self.type = type
        self.errors = errors
        super().__init__(title)


def _request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


def _problem_response(request: Request, problem: Problem) -> JSONResponse:
    return JSONResponse(
        status_code=problem.status,
        media_type=PROBLEM_MEDIA_TYPE,
        content=problem.model_dump(exclude_none=True),
    )


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ProblemException)
    async def _handle_problem(request: Request, exc: ProblemException) -> JSONResponse:
        problem = Problem(
            type=exc.type,
            title=exc.title,
            status=exc.status_code,
            detail=exc.detail,
            instance=str(request.url),
            request_id=_request_id(request),
            errors=exc.errors,
        )
        return _problem_response(request, problem)

    @app.exception_handler(AuthError)
    async def _handle_auth(request: Request, exc: AuthError) -> JSONResponse:
        problem = Problem(
            title="Authentication Error",
            status=exc.status_code,
            detail=exc.message,
            instance=str(request.url),
            request_id=_request_id(request),
        )
        return _problem_response(request, problem)

    @app.exception_handler(WeakPasswordError)
    async def _handle_weak_password(request: Request, exc: WeakPasswordError) -> JSONResponse:
        problem = Problem(
            title="Validation Error",
            status=422,
            detail=str(exc),
            instance=str(request.url),
            request_id=_request_id(request),
        )
        return _problem_response(request, problem)

    @app.exception_handler(BudgetExceededError)
    async def _handle_budget(request: Request, exc: BudgetExceededError) -> JSONResponse:
        problem = Problem(
            title="AI Budget Exceeded",
            status=exc.status_code,
            detail=exc.message,
            instance=str(request.url),
            request_id=_request_id(request),
        )
        return _problem_response(request, problem)

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        problem = Problem(
            title=str(exc.detail),
            status=exc.status_code,
            instance=str(request.url),
            request_id=_request_id(request),
        )
        return _problem_response(request, problem)

    @app.exception_handler(RequestValidationError)
    async def _handle_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            FieldError(field=".".join(str(p) for p in e["loc"]), message=e["msg"])
            for e in exc.errors()
        ]
        problem = Problem(
            title="Validation Error",
            status=422,
            detail="One or more fields failed validation.",
            instance=str(request.url),
            request_id=_request_id(request),
            errors=errors,
        )
        return _problem_response(request, problem)

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        """Catch-all (ERR-003): every unhandled exception still returns RFC 7807, never a stack
        trace or internal detail. The real exception is logged server-side only, correlated by
        `request_id` so it's debuggable without ever reaching the client (spec §28.2 "AI failure"
        / generic degradation: the system must never silently fail *or* leak internals).
        """
        log_event(
            logger,
            logging.ERROR,
            "api.unhandled_exception",
            path=str(request.url),
            method=request.method,
            error=repr(exc),
            exc_info=True,
        )
        problem = Problem(
            title="Internal Server Error",
            status=500,
            detail="An unexpected error occurred. Your data is unaffected; please try again.",
            instance=str(request.url),
            request_id=_request_id(request),
        )
        return _problem_response(request, problem)
