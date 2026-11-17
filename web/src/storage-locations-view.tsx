import { useId } from 'react';
import { AreaHelp } from './area-help';
import { Panel } from './components';

export type StorageCategory = 'market_data' | 'reports' | 'artifacts';

// Private presentation model only. The common storage API adapter will supply
// observed state and original-request reconciliation; no HTTP fields are chosen here.
export type StorageLocationRowView = {
 category: StorageCategory;
 effectiveDirectory: string | null;
 defaultDirectory: string | null;
 usesDefault: boolean | null;
 draftDirectory: string;
 canEdit: boolean;
 readyToSave: boolean;
 busy: 'check' | 'save' | 'reset' | 'confirm' | null;
 resultUncertain: boolean;
 publicMessage?: string;
 unavailableMessage?: string;
 errorMessage?: string;
 edit: (directory: string) => void;
 check: () => void;
 save: () => void;
 reset: () => void;
 confirmOriginal?: () => void;
};
const categories = [
 { id: 'market_data', title: '行情与数据集', purpose: '导入的 CSV、下载的行情和固定数据集。', example: 'D:\\FinanceAnalyze\\MarketData', help: 'storage-market-data' },
 { id: 'reports', title: '结果报告', purpose: '回测曲线与报告、数据研究或 AI 研究报告。', example: 'E:\\FinanceAnalyze\\Reports', help: 'storage-reports' },
 { id: 'artifacts', title: '模型与研究文件', purpose: '训练出的模型、策略或因子代码，以及保存的实验。', example: 'E:\\FinanceAnalyze\\Research', help: 'storage-artifacts' },
] as const;

export function StorageLocationsView({ rows, loading, publicError, refresh }: { rows: StorageLocationRowView[]; loading: boolean; publicError?: string; refresh: () => void }) {
 return <Panel title="保存位置">
  <p>新位置只对之后提交的任务生效。旧文件会留在原处，仍可打开。</p>
  <p>先创建文件夹，再粘贴完整路径并点“检查目录”。路径可以包含中文和空格。</p>
  <button type="button" className="text-button" disabled={loading} onClick={refresh}>刷新保存位置</button>
  {publicError && <p role="alert">{publicError}</p>}
  {categories.map(category => <StorageLocationRow key={category.id} category={category} row={rows.find(row => row.category === category.id)} loading={loading} />)}
 </Panel>;
}

function StorageLocationRow({ category, row, loading }: { category: typeof categories[number]; row: StorageLocationRowView | undefined; loading: boolean }) {
 const id = useId();
 const blocked = loading || !row || !row.canEdit || row.busy !== null || row.resultUncertain;
 return <fieldset aria-labelledby={`${id}-title`}>
  <legend id={`${id}-title`}>{category.title}</legend>
  <AreaHelp topic={category.help} title={`${category.title}保存位置`} />
  <p>{category.purpose}</p>
  <dl>
   <div><dt>实际使用的目录</dt><dd style={{overflowWrap:'anywhere'}}><output>{row?.effectiveDirectory ?? (loading ? '正在读取…' : '尚未提供')}</output></dd></div>
   <div><dt>当前设置</dt><dd>{row?.usesDefault === true ? '使用默认位置' : row?.usesDefault === false ? '使用自定义位置' : '尚未提供'}</dd></div>
  </dl>
  <label htmlFor={`${id}-directory`}>新文件夹的完整路径</label>
  <input id={`${id}-directory`} type="text" maxLength={4096} value={row?.draftDirectory ?? ''} placeholder={category.example} disabled={blocked} onChange={event => row?.edit(event.target.value)} aria-describedby={`${id}-example ${id}-status`} autoComplete="off" spellCheck={false} />
  <p id={`${id}-example`}>例如 {category.example}</p>
  <div className="form-actions">
   <button type="button" disabled={blocked} onClick={() => row?.check()}>{row?.busy === 'check' ? '检查中…' : '检查目录'}</button>
   <button type="button" disabled={blocked || !row?.readyToSave} onClick={() => row?.save()}>{row?.busy === 'save' ? '保存中…' : '保存'}</button>
   <button type="button" disabled={blocked} onClick={() => row?.reset()}>{row?.busy === 'reset' ? '恢复中…' : '恢复默认'}</button>
  </div>
  <div id={`${id}-status`} aria-live="polite">
   {row?.unavailableMessage && <p role="alert">当前目录不可用：{row.unavailableMessage}</p>}
   {row?.errorMessage && <p role="alert">{row.errorMessage}</p>}
   {row?.publicMessage && <p>{row.publicMessage}</p>}
   {row?.resultUncertain && <><p>上次操作的结果还不确定。请先确认，再进行新的修改。</p>{row.confirmOriginal && <button type="button" disabled={!row.canEdit || row.busy !== null} onClick={row.confirmOriginal}>{row.busy === 'confirm' ? '确认中…' : '确认上次操作结果'}</button>}</>}
   {!row && !loading && <p>暂时无法读取这一类文件的保存位置。</p>}
  </div>
  <details><summary>查看默认位置</summary><p style={{overflowWrap:'anywhere'}}>{row?.defaultDirectory ?? '尚未提供'}</p></details>
 </fieldset>;
}
