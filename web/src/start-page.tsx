import { DatasetResearchWorkspace } from './dataset-research-workspace';
export function StartPage({ canWrite, userId }: { canWrite: boolean; userId: string }) {
  return (
    <>
      <DatasetResearchWorkspace canWrite={canWrite} userId={userId} backtest />
      <p className="muted">
        <a className="text-button" href="#intelligent">打开 AI 研究 →</a>{' '}
        <a className="text-button" href="#my-results">我的结果 →</a>{' '}
        <a className="text-button" href="#results">
          打开回测报告列表 →
        </a>{' '}
        <a className="text-button" href="#research">
          高级回测与模拟练习 →
        </a>
      </p>
    </>
  );
}
