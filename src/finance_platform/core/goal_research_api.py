"""HTTP and curated MCP operations over the same goal service."""
from fastapi import APIRouter, Query, Request

from .goal_research import ControlGoal, CreateGoal, SaveGoalDefaults
from .mcp_operations import Empty, ID, Operation, Page, model


def create_router(runtime, service):
    router = APIRouter(prefix="/research/goals", tags=["goal research"])

    @router.get("/readiness")
    def readiness(request: Request):
        return service.readiness(runtime.require_principal(request))

    @router.get("/defaults")
    def defaults(request: Request):
        return service.get_defaults(runtime.require_principal(request))

    @router.put("/defaults")
    def save_defaults(body: SaveGoalDefaults, request: Request):
        return service.save_defaults(runtime.require_principal(request,write=True),body.model_dump())

    @router.post("", status_code=202)
    def create(body: CreateGoal, request: Request):
        principal = runtime.require_principal(request, write=True)
        return service.create(principal, body.goal, body.idempotency_key)

    @router.get("")
    def listing(request: Request, limit: int = Query(50, ge=1, le=100),
                offset: int = Query(0, ge=0, le=10000)):
        return service.list(runtime.require_principal(request), limit=limit, offset=offset)

    @router.get("/{task_id}")
    def read(task_id: str, request: Request):
        return service.get(runtime.require_principal(request), task_id)

    @router.get("/{task_id}/model-usage")
    def model_usage(task_id: str, request: Request, limit: int = Query(50, ge=1, le=100),
                    offset: int = Query(0, ge=0, le=10000)):
        return service.model_usage(runtime.require_principal(request), task_id, limit=limit, offset=offset)

    @router.post("/{task_id}/control")
    def control(task_id: str, body: ControlGoal, request: Request):
        principal = runtime.require_principal(request, write=True)
        return service.control(principal, task_id, body.action, body.expected_revision, body.idempotency_key)

    return router


def operations():
    path = "/research/goals"
    task = model("GoalResearchTaskInput", task_id=ID)
    usage = model("GoalResearchModelUsageInput", base=Page, task_id=ID)
    control = model("GoalResearchControlInput", base=ControlGoal, task_id=ID)
    return [
        Operation("goal_research_readiness", "查看目标研究就绪状态和保存的默认引用。", "GET", path+"/readiness", Empty),
        Operation("goal_research_defaults", "读取本人的目标研究默认设置。", "GET", path+"/defaults", Empty),
        Operation("goal_research_save_defaults", "按版本保存已有模型、数据连接及预算引用。", "PUT",
                  path+"/defaults", SaveGoalDefaults, body_rest=True, idempotent=True),
        Operation("goal_research_create", "保存目标并提交受预算约束的研究。", "POST", path, CreateGoal,
                  body_rest=True, idempotent=True),
        Operation("goal_research_list", "列出本人的目标研究。", "GET", path, Page, query_fields=("limit","offset")),
        Operation("goal_research_read", "重开本人目标研究及已保存的子任务引用。", "GET", path+"/{task_id}",
                  task, path_fields=("task_id",)),
        Operation("goal_research_model_usage_read", "分页读取本人目标规划和复核的已保存实际模型用量。",
                  "GET", path+"/{task_id}/model-usage", usage, path_fields=("task_id",),
                  query_fields=("limit","offset")),
        Operation("goal_research_control", "按当前版本暂停、取消或继续本人的目标研究。", "POST",
                  path+"/{task_id}/control", control, path_fields=("task_id",), body_rest=True, idempotent=True),
    ]
