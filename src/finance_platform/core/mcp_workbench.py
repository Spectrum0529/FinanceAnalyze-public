"""Small discoverable workflow over named business tools, no execution port."""
def guide():
    return {
      'contract':'finance-workbench-guide/v1','transport':'LOCAL_STDIO',
      'notice':'工作台研究仅将明确选定的资料和本次工作流自身的历史纳入上下文。授权外部 AI 使用完整工作台 MCP 后，它可调用相应的读取工具，访问当前个人账户已有权限的已保存资料。',
      'steps':[
        {'step':'发现实际能力和数据','tools':['application_capabilities','data_connections_list','data_datasets_list','data_dataset_read'],'instruction':'只选择已成功保存的数据集ID/版本。没有合适数据时列出缺口；下载需显式选择已配置提供方、日期与范围，不编造行情。'},
        {'step':'选择支持的策略或搜索范围','tools':['backtest_catalog','strategy_search_plan'],'instruction':'普通回测用buy_hold/sma20/sma_cross_5_20；有界搜索候选用buy_hold/sma(slow5..30)/sma_cross(fast2..15,slow5..30,fast<slow)。max_trials<=12，训练70%/验证30%，各证券训练至少80/验证至少40观察点；不发送代码/公式，不把训练最优当市场验证。'},
        {'step':'开始真实工作','tools':['backtest_start','ai_task_plan','ai_task_start'],'instruction':'单次回测选择保存的数据集、单一总本金和费用。研究先形成READY计划再提交。一个明确任务一个调用标识；回执不明先查状态，不生成新标识重复下单。'},
        {'step':'观察或取消','tools':['job_status','job_events','job_cancel'],'instruction':'返回job_id/run_id后轮询；QUEUED/RUNNING不是成功，取消请求也不是完成。FAILED时展示固定error_code。'},
        {'step':'读保存报告和比较证据','tools':['results_list','strategy_search_report','result_read','result_section','result_export'],'instruction':'仅SUCCEEDED且已保存报告可作为完成证据；遵循分区和next_offset读取实际曲线。训练比较与一次验证分开显示，不能根据验证表现继续筛选；同数据/资金/费用/时间口径，不足数据、不可比和未验证PIT明确保留。'}],
      'model_modes':{'tools':'确定数据工具，不调用模型','single_assistant':'一个保存模型配置','model_panel':'多个保存模型分别报告','role_team':'明确固定十二角色快照，不是选一个模型就默认全团队'},
      'constraints':['不运行shell、SQL、任意文件路径、外部URL或LIVE','不返回或索要现有密钥值；模型配置只用引用','真实模型调用可能付费，只在用户配置后明确启动；本批仅离线SDK验证','ChatGPT网页远程连接属于未来版本，本地stdio不能被网页直接调用'],
      'prompts':[
        '先列出工作台已有固定数据集和策略能力，说明数据覆盖与费用口径。不要下载或开始运行；等我选定数据集。',
        '使用我选定的固定数据集，以总本金10000美元、每股0.005美元且每笔最低1美元比较支持的策略候选；先展示有界试验数量和时间划分，再执行实际工具。保存真实结果，未运行或数据不足请明确说明。',
        '查询刚才返回的job_id；仍运行就显示实际进度，失败则显示错误码。完成后读取保存报告和实际成本，不根据等待时间宣称完成。',
        '取消这个job_id并核对最终状态。不要自动重试，不把取消说成断点已保存。']}
