"""Protocol-specific error encoders for the adapters package."""

from agy_proxy_core.json_codec import JSONResponse


def openai_error(status_code: int, message: str, err_type: str, code: str | None = None,
                 *, param: str | None = None) -> JSONResponse:
    body: dict = {"error": {"message": message, "type": err_type, "param": param, "code": code}}
    return JSONResponse(status_code=status_code, content=body)
