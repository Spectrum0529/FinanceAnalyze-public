"""Public errors use fixed messages, never exception strings or user input."""
import re

MESSAGES = {
    "HOSTED_OPERATION_DISABLED": "此本机运维操作在托管模式下不可用。",
    "RESTORED_COPY_OFFLINE_ONLY": "恢复副本仅用于离线核验，请勿直接作为服务数据库。",
    "RESTORED_DATABASE_REQUIRED": "请选择已登记且与恢复回执一致的离线恢复副本。",

    "PASSWORD_POLICY": "请使用 15–128 个字符的非通用口令。",
    "RECOVERY_REJECTED": "无法验证恢复凭据。",
    "REAUTH_REQUIRED": "请再次验证身份后继续。",
    "LAST_ADMIN_REQUIRED": "必须保留至少一位已启用的管理员。",
    "MFA_REQUIRED": "请输入身份验证器的动态验证码。",
    "MFA_INVALID": "动态验证码无效、已使用或已过期。",
    "MFA_UNAVAILABLE": "本机动态验证码服务暂不可用。",

    "CREDENTIAL_GENERATION_CHANGED": "WRDS 凭据已更新，请重新提交任务。",
    "WRDS_CREDENTIAL_LOCAL_ONLY": "请在 FinanceAnalyze 本机设置中管理 WRDS 凭据。",
    'WRDS_LOCAL_MODEL_TOOL_REQUIRED': 'WRDS 固定数据可用单助手分析或本地工具。当前未开放模型小组、团队和模型迭代搜索。',
    'WRDS_BENCHMARK_UNSUPPORTED': 'WRDS 固定数据当前支持本机价格回测，请先清除对照基准。',
    'WRDS_NATIVE_INPUT_UNAVAILABLE': '当前隔离自主研究执行器还不能接收 WRDS 固定数据，请使用 AI 研究的助手分析或本地回测。',
    'SEARCH_PROVIDER_TOOL_RUNTIME_UNSUPPORTED':'此模型提供方尚未验证策略搜索工具流程，请选OpenAI或DeepSeek，或使用已有研究模式。',
    'SEARCH_ENGINE_BUDGET_TOO_SMALL':'证券数量超出当前引擎预算，请缩小证券范围或提高预算。',
    'SEARCH_ENGINE_BUDGET_EXCEEDED':'已达到逐证券回测次数上限，已完成的尝试保留在任务记录中。',
    'SEARCH_RESULT_BYTES_EXCEEDED':'已达到报告保存大小上限，请减少候选或证券范围。',
    'SEARCH_WALL_BUDGET_EXCEEDED':'已达到研究总时间上限，已完成的尝试保留在任务记录中。',
    'SEARCH_SDK_CLEANUP_UNPROVEN':'模型请求未能确认本地资源回收，任务已停止；请检查任务记录。',
    'IDEMPOTENCY_KEY_REQUIRED':'请为这次操作提供调用标识；重复发送同一操作请沿用同一标识。',
    'MODEL_PROFILE_OWNER_REQUIRED':'请先选择自己的模型配置。',
    'MODEL_PROFILE_HASH_MISMATCH':'保存的模型版本校验失败，请保留记录并重新选择配置。',
    'MODEL_TARGET_CHANGED':'模型版本所绑定的实际连接已变化，请明确保存新版本或重新固定团队快照。',
    'MODEL_ENDPOINT_INVALID':'服务连接地址不符合要求，请选择受信任的连接配置。',
    'INPUT_BUDGET_EXCEEDED':'提示词和上下文超过所选长度，请精简内容或调整允许的预算。',
    'ENDPOINT_PROFILE_REQUIRED':'请先选择已配置的服务连接。',
    'CREDENTIAL_BINDING_SERVICE_UNAVAILABLE':'密钥保险库尚未接入，请完成本地配置后重试。',
    'CREDENTIAL_NOT_CONFIGURED':'请先在模型设置中配置对应提供方的API密钥，或选择已配置的模型。',
    'MODEL_SECRET_NOT_CONFIGURED':'请先在模型设置中配置对应提供方的API密钥，或选择已配置的模型。',
    'CREDENTIAL_TARGET_UNVERIFIED':'这个环境引用尚未绑定供应商和连接，请先配置受信任的引用。',
    'CREDENTIAL_TARGET_MISMATCH':'密钥绑定的供应商或连接不匹配，请选择对应密钥或重新配置。',
    'VERIFIED_CHECKPOINT_REQUIRED':'没有可验证的恢复断点；如需重新运行，请明确选择重新执行。',
    'RESULT_EXPORT_UNAVAILABLE':'这类报告暂不支持导出；原有数据权限限制仍然有效。',
    'RESULT_EXPORT_INVALID':'报告导出内容校验失败，请保留原结果后重试。',
    'RESULT_EXPORT_TOO_LARGE':'报告过大，无法一次导出；可以先分段查看结果。',
    'RESULT_EXPORT_CHANGED':'报告分页信息不一致，已停止导出以避免遗漏。',
    'RESULT_READ_FAILED':'读取报告失败，请稍后重试并保留任务标识。',
    'RESULT_SECTION_UNAVAILABLE':'报告中没有这个分区，请使用摘要返回的分区名称。',
    'RESULT_VIEW_CHANGED':'报告视图已变化，请重新获取摘要后继续分页。',
    'RESULT_VIEW_TOO_DEEP':'报告结构超出可安全展示的层级，已停止读取。',
    'RESULT_PAGE_TOO_LARGE':'这一页内容过大，请减小分页数量。',
    'MCP_LOCAL_ORIGIN_REQUIRED':'MCP 只能连接指定的本机应用地址。',
    'MCP_RESPONSE_TOO_LARGE':'返回内容过大，请缩小范围或分页查看。',
    'MCP_PRODUCT_RESPONSE_INVALID':'应用返回内容无法识别，请检查本地应用版本。',
    'MCP_REDIRECT_REFUSED':'本地应用地址发生跳转，请使用启动器显示的实际地址。',
    'MCP_PRODUCT_ACTION_FAILED':'应用未完成操作，请检查对应任务的状态与说明。',
    'MCP_PRODUCT_UNAVAILABLE':'无法连接本地应用，请先打开 FinanceAnalyze。',
    'MCP_PERSONAL_MODE_REQUIRED':'请使用本机个人模式启动 FinanceAnalyze。',
    'MCP_AUTHORIZATION_REQUIRED':'请在本机应用中授权此 MCP 客户端。',
    'MCP_PAIRING_REJECTED':'配对码无效、已过期或已使用。',
    'MCP_SCOPE_REQUIRED':'当前 MCP 授权未开放此操作。',
    'MCP_LOCAL_DATA_ONLY':'此数据或报告只能在本机工作台查看或使用。',
    'MCP_REPLAY_REJECTED':'MCP 请求证明已经使用。',
    'MCP_GRANT_LIMIT':'有效的 MCP 授权已达上限，请撤销不用的授权。',
    'MCP_MANAGED_LISTENER_REQUIRED':'请通过 FinanceAnalyze 启动器打开此安装后重试。',
    'MCP_INPUT_BINDING_INVALID':'操作字段不符合当前接口，请重新查看工具说明。',
    'MCP_REQUEST_TOO_LARGE':'提交内容过大，请分批导入；供应商下载请使用下载任务。',
    'MCP_PRODUCT_UNAVAILABLE':'暂时无法连接 FinanceAnalyze 本机服务。请检查工作台是否已打开，再检查连接状态。',
    'MARKET_REUSE_LIBRARY_REFERENCE_ONLY':'复用已保存数据集只需原引用和幂等标识。请移除 name 和 connection_id，再重试。',
    'BENCHMARK_PROXY_DATASET_MISMATCH':'请选择对应 ETF 的专用 Yahoo 价格对照数据集。下载时请选择「对照数据集」。',
    "ACCOUNT_ALREADY_EXISTS": "账户标识已存在，请使用其他标识",
    "INVALID_INPUT": "输入不符合契约", "NOT_AUTHENTICATED": "请先登录", "FORBIDDEN": "没有此操作权限",
    "NOT_FOUND": "资源不存在或不可访问", "CONFLICT": "操作与当前状态冲突", "RATE_LIMITED": "请求过于频繁，请稍后重试",
    "BAD_CREDENTIALS": "用户名或密码错误", "CSRF_REJECTED": "请求安全校验失败", "ORIGIN_REJECTED": "请求来源未获允许",
    "SETUP_REQUIRED": "请先完成本地初始化", "SETUP_REJECTED": "初始化凭证无效或已使用", "LIVE_DISABLED": "真实交易模式未启用",
    "HANDLER_UNAVAILABLE": "任务处理器未注册", "LEASE_LOST": "任务租约失效，操作已阻止", "JOB_CANCELLED": "任务已取消",
    "HANDLER_FAILED": "任务执行失败", "LEASE_EXPIRED": "任务执行中断，租约已过期", "INTERNAL_ERROR": "服务暂时无法完成请求",
    "DATABASE_UNAVAILABLE": "平台数据库暂不可用", "PROFILE_BLOCKED": "原研究数据批准门尚未通过",
    "MIGRATION_REJECTED": "数据库迁移不符合版本约束", "UNSUPPORTED_CAPABILITY": "此能力尚不支持",
}


class PlatformError(ValueError):
    def __init__(self, code: str, status: int = 400):
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", code):
            code = "INTERNAL_ERROR"
        self.code, self.status = code, status
        self.message = MESSAGES.get(code, "操作未通过服务端校验")
        super().__init__(self.message)

    def response(self, request_id: str):
        return {"code": self.code, "message": self.message, "request_id": request_id, "details": {}}
