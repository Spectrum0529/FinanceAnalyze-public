import { lazy, Suspense } from 'react';
import {createRoot} from 'react-dom/client';
import type { SessionRotation, WorkbenchSession } from './platform-account-session';
import { reauthenticateAccount } from './platform-account-session';
import {PageBoundary} from './page-boundary';
import {Notice} from './components';
import {ResultPreviewProvider} from './result-preview';
import {WorkspaceSelection} from './workspace-selection';
const Backtest=lazy(()=>import('./dataset-research-workspace').then(m=>({default:m.DatasetResearchWorkspace})));
const FirstExample=lazy(()=>import('./first-example').then(m=>({default:m.FirstExample})));
const AI=lazy(()=>import('./ai-research-workspace').then(m=>({default:m.AiResearchWorkspace})));
const Data=lazy(()=>import('./data-library-workspace').then(m=>({default:m.DataLibraryWorkspace})));
const Results=lazy(()=>import('./unified-results-workspace').then(m=>({default:m.UnifiedResultsWorkspace})));
const Jobs=lazy(()=>import('./pages').then(m=>({default:m.Jobs})));
const Settings=lazy(()=>import('./settings-page').then(m=>({default:m.Settings})));
const Migration=lazy(()=>import('./migration-workspace').then(m=>({default:m.MigrationWorkspace})));
const Research=lazy(()=>import('./research-page').then(m=>({default:m.Research})));
const Strategies=lazy(()=>import('./strategy-page').then(m=>({default:m.Strategies})));
const Models=lazy(()=>import('./research-page').then(m=>({default:m.Models})));
const Simulation=lazy(()=>import('./trading-page').then(m=>({default:m.Simulation})));
const Accounts=lazy(()=>import('./trading-page').then(m=>({default:m.Accounts})));
const Operations=lazy(()=>import('./settings-page').then(m=>({default:m.Operations})));
export function mountBusiness(host:HTMLElement,page:string,session:WorkbenchSession|null,onSignedOut:()=>void,onSessionRotated:(receipt:SessionRotation,signal:AbortSignal)=>Promise<void>,motion:()=>boolean){
 if(page==='security'&&session?.accountMode){
  let active=true,dispose:(()=>void)|undefined;
  host.textContent='正在打开账户安全设置…';
  void import('./account-security').then(({mountAccountSecurity})=>{
   if(!active)return;
   host.replaceChildren();dispose=mountAccountSecurity(host,session,onSignedOut,onSessionRotated,motion);
  }).catch(()=>{if(active)host.textContent='无法打开账户安全设置。请刷新后重试。';});
  return ()=>{active=false;dispose?.();};
 }
 const root=createRoot(host);const userId=session?.user.user_id??'',canWrite=!!session&&session.user.role!=='viewer';
 const query = new URLSearchParams(location.hash.split('?')[1]);
 const example = page === 'module-a' && query.get('first_example') === '1';
 const migration = page === 'settings' && query.get('section') === 'migration';
 const view= !session ? <Notice>当前可浏览界面。要使用回测、WRDS 或 AI，请在本机打开 FinanceAnalyze.exe，或登录你的研究服务。</Notice> : page==='module-a'?(example?<FirstExample key={userId} canWrite={canWrite}/>:<Backtest userId={userId} canWrite={canWrite} backtest/>):page==='module-b'?<AI userId={userId} canWrite={canWrite} accountMode={session.accountMode}
  reauthenticate={(data,signal)=>reauthenticateAccount(data,signal,onSessionRotated)} motion={motion}/>:page==='module-c'?<Data userId={userId} canWrite={canWrite}/>:page==='module-d'?<Results userId={userId} canWrite={canWrite}/>:page==='jobs'?<Jobs canWrite={canWrite}/>:page==='settings'?(migration?(session.accountMode&&canWrite?<Migration key={userId} ownerId={userId}/>:<Notice>请使用有写入权限的工作台账户打开迁机助手。</Notice>):<Settings isAdmin={session.user.role==='admin'} storageCanWrite={!session.accountMode||session.user.role==='admin'} cacheCanWrite={canWrite} storageOwner={userId} showMigration={session.accountMode&&canWrite}/>):page==='results'?<Research userId={userId} canWrite={canWrite} resultsOnly/>:page==='strategies'?<Strategies canWrite={canWrite} ownerId={userId} migrationAdmin={session.accountMode&&session.user.role==='admin'} reauthenticate={(data,signal)=>reauthenticateAccount(data,signal,onSessionRotated)} motion={motion}/>:page==='models'?<Models canWrite={canWrite}/>:page==='simulation'?<Simulation canWrite={canWrite}/>:page==='accounts'?<Accounts canWrite={canWrite}/>:page==='operations'?<Operations isAdmin={session.user.role==='admin'}/>:<Notice>请从菜单选择要打开的页面。</Notice>;
 root.render(<PageBoundary key={page}><WorkspaceSelection><ResultPreviewProvider key={userId} motion={motion}>
  <Suspense fallback={<p className="workbench-loading" role="status">正在打开页面…</p>}>{view}</Suspense>
 </ResultPreviewProvider></WorkspaceSelection></PageBoundary>);return ()=>root.unmount();
}
