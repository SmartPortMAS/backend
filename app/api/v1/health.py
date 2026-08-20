from fastapi import APIRouter

router = APIRouter(prefix="/health", tags=["health"])


@router.get("", summary="서비스 상태 확인")
def health() -> dict[str, str]:
    return {"status": "ok"}
