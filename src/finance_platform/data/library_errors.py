"""Fixed public data feature messages; no source text or credentials."""
MESSAGES={'NATIVE_DOWNLOAD_TIME_BUDGET': '本次下载时间已用完，已完成股票的数据已保留，可按原参数续传。', 'NATIVE_DOWNLOAD_ATTEMPT_BUDGET': '此下载的累计尝试次数已用完，续传不会重置预算。', 'NATIVE_DOWNLOAD_SDK_BUDGET': '此下载的累计请求预留预算已用完，已完成的数据仍保留。', 'NATIVE_RESUME_MISMATCH': '续传参数与原任务不一致，请使用原连接、证券范围和日期。', 'NATIVE_RESUME_BUSY': '上一下载仍在运行或已完成，请刷新任务状态。', 'DOWNLOAD_RESUME_STORAGE_UNBOUND': '这段历史下载缺少可验证的保存位置绑定，无法安全续传；原记录保持不变。', 'DOWNLOAD_RESUME_UNSUPPORTED': '这类任务不支持续传，请选择支持的数据下载任务。', 'PROVIDER_DOWNLOAD_UNSUPPORTED': '此连接用于本地文件导入，请通过导入入口添加数据。', 'RESEARCH_EXPORT_SCHEMA': '导出参数不完整，请重新选择固定版本和日期范围。', 'RESEARCH_EXPORT_FORMAT': '所选研究导出格式尚不支持。', 'RESEARCH_EXPORT_PAGE_INVALID': '导出分页范围不正确，请从返回的下一页位置继续读取。', 'RESEARCH_INPUT_LIMIT': '所选研究窗口或导出文件过大，请缩小证券和日期范围。', 'OPENBB_FEATURE_DISABLED': 'OpenBB格式适配尚未启用，可使用标准研究格式。', 'OPENBB_DEPENDENCY_MISSING': '尚未安装OpenBB格式适配依赖，可使用标准研究格式。', 'OPENBB_VERSION_UNSUPPORTED': 'OpenBB依赖版本与此适配器不兼容，可使用标准研究格式。', 'OPENBB_MODEL_UNAVAILABLE': '无法载入官方OpenBB数据格式，原始数据保持不变。', 'OPENBB_MODEL_VALIDATION_FAILED': '数据未通过官方OpenBB格式校验，请使用标准研究格式并保留记录。', 'OPENBB_FEATURE_CONFIGURATION_INVALID': 'OpenBB适配器配置无效，请检查本地功能设置。', 'UNIVERSE_REFERENCE_ROW_LIMIT': '筛选名单超过本次安全处理数量，请缩小范围。', 'UNIVERSE_REFERENCE_ORDER': '证券名单返回顺序异常，未保存此次筛选结果。', 'UNIVERSE_REFERENCE_BINDING': '证券名单与所选交易所或类型不一致，未保存此次筛选结果。'}

MESSAGES.update({
 'OPENBB_PRICE_BASIS_MISMATCH':'Cboe的复权口径尚未确认，请使用供应商观测值口径，不能标记为原始价。',
 'OPENBB_RESPONSE_BINDING':'Cboe数据与所选证券或日期不一致，未发布此次数据。',
 'OPENBB_ENDPOINT_REJECTED':'OpenBB请求目标不符合固定数据源限制，已停止采集。',
 'OPENBB_SOURCE_ACCESS_REJECTED':'数据源拒绝访问，已停止此次采集，不会尝试绕过限制。',
 'OPENBB_SOURCE_RATE_LIMITED':'数据源要求限流，已保留完成段，请稍后按原参数续传。',
 'OPENBB_SOURCE_HTTP_FAILED':'数据源未正常返回，已保留完成段，请查看任务状态。',
 'OPENBB_TRANSFORM_MISMATCH':'OpenBB标准化结果与原始响应不一致，未发布此次数据。',
 'OPENBB_FIXTURE_UNSUPPORTED':'此OpenBB采集入口不接受替代执行模块。',
})

MESSAGES.update({'OPENBB_RUNTIME_UNBOUND':'OpenBB独立采集环境尚未绑定，请先完成本地可选组件配置。','OPENBB_EXPORT_SOURCE_UNSUPPORTED':'此官方格式导出需要已保存的Cboe官方模型快照；其他数据请使用标准研究格式。'})

MESSAGES['OPENBB_CAPTURE_REQUIRED']='Cboe数据须由受控的官方采集任务生成，不能把手工导入标为已采集。'

MESSAGES.update({
    'WRDS_INPUT_INVALID': '请核对 WRDS 证券、日期和本次任务的数量上限。',
    'WRDS_CREDENTIAL_REQUIRED': '请先配置本机 WRDS 凭据，再选择对应的数据连接。',
    'WRDS_UNAVAILABLE': 'WRDS 连接组件尚未就绪。',
    'WRDS_AUTHENTICATION_FAILED': 'WRDS 拒绝身份验证，请检查本机凭据和 Duo 通知。',
    'WRDS_ACCESS_DENIED': '当前账号无权读取所需的 CRSP 表。',
    'WRDS_SCHEMA_UNSUPPORTED': '当前账号可见的 CRSP 表未提供所需的 CIZ 字段。',
    'WRDS_CONNECTION_FAILED': '无法建立经过验证的 WRDS 连接，请检查网络或 Duo 通知。',
    'WRDS_TIMEOUT': '本次 WRDS 查询超时，请缩小证券或日期范围后重试。',
    'WRDS_DATA_LIMIT': '本次 WRDS 查询超过数据上限，请缩小证券或日期范围。',
    'WRDS_SECURITY_BINDING': 'CRSP 证券身份未能覆盖所选窗口，请核对 PERMNO 或缩短窗口。',
    'WRDS_OHLC_UNAVAILABLE': '部分 CRSP 记录缺少有效的真实成交 OHLCV，本次未保存数据版本。',
    'WRDS_RESPONSE_INVALID': 'WRDS 返回的数据与请求不一致，本次未保存数据版本。',
    'WRDS_HISTORY_EMPTY': 'WRDS 在所选日期范围内未返回历史数据。',
    'WRDS_CAPTURE_REQUIRED': 'WRDS 数据版本必须由受控的历史采集任务保存。',
    'WRDS_LOCAL_RESEARCH_REQUIRED': '此 WRDS 版本仅供已准入的本机研究读取。',
})

MESSAGES.update({
    'SDK_DNS_FAILED': '找不到行情网站。请检查网络连接，稍后重试。',
    'SDK_TRANSPORT_FAILED': '与行情网站的连接未完成。请打开这次下载任务查看详情。',
    'SDK_TIMEOUT': '等待行情网站响应超时。请打开这次下载任务查看进度，选择可用的重试或继续方式。',
    'SDK_RESPONSE_LIMIT_EXCEEDED': '行情网站这次返回的数据超出下载限制。请查看任务详情。若网站总是返回完整历史，可尝试另一个可用来源。',
    'SDK_RUNTIME_BINDING_CHANGED': '本机的行情组件与当前安装版本不匹配。请检查软件安装。',
    'SECURITY_IDENTITY_UNAVAILABLE': '无法确认这个代码对应哪只股票或基金。请检查代码和所选市场。',
    'BENCHMARK_IDENTITY_UNVERIFIED': '无法确认下载的行情对应这只 ETF。请核对来源、代码、上市交易所和币种。',
    'MARKET_CONNECTION_ARCHIVED': '这个连接已归档。恢复或更换连接后再提交新下载。',
    'MARKET_CONNECTION_REQUIRED': '请选择同一来源下未归档的连接。',
    'MARKET_MERGE_ADJUSTMENT_UNVERIFIED': '这两份价格可能使用了不同的复权基准。请用同一来源下载完整区间后重新预览。',
    'MARKET_MERGE_VERIFICATION_CHANGED': '完整区间有更新或新增的行情。请核对变化，再确认是否采用完整区间。',
    'MARKET_MERGE_VERIFICATION_COVERAGE': '完整区间缺少原先已有的交易日，请选择覆盖齐全的数据。',
    'MARKET_MERGE_INCOMPATIBLE': '两份数据的证券、来源、币种、交易日或价格规则不同，无法合并。',
    'MARKET_MERGE_CONFLICT': '重叠交易日的行情有差异。请核对后选择适用的处理方式。',
    'MARKET_MERGE_PREVIEW_CHANGED': '当前选择与保存前的预览不同，请重新预览。',
})
