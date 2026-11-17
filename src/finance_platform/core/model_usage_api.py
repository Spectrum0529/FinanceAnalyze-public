"""Ordinary model readers remain available without an autonomous executor."""
from fastapi import APIRouter,Query,Request
from .model_usage import ModelUsageService
from .model_usage_models import UsageSummary,TracePage


def create_router(runtime):
    router=APIRouter(prefix='/research/tasks',tags=['model telemetry'])
    @router.get('/{job_id}/model-usage',response_model=UsageSummary)
    def task_usage(job_id:str,request:Request,limit:int=Query(50,ge=1,le=100),offset:int=Query(0,ge=0)):
        return ModelUsageService(runtime).summary(runtime.require_principal(request),'job',job_id,limit,offset)
    @router.get('/{job_id}/model-trace',response_model=TracePage)
    def task_trace(job_id:str,request:Request,limit:int=Query(50,ge=1,le=100),offset:int=Query(0,ge=0)):
        return ModelUsageService(runtime).trace(runtime.require_principal(request),job_id,limit,offset)
    return router
