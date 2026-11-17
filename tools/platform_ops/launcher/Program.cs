using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Net;
using System.Net.NetworkInformation;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;

namespace FinanceAnalyzeLauncher {
    sealed class Failure : Exception { public string Code; public Failure(string code) { Code=code; } }
    static class Data {
        internal static string Hash(byte[] value) { using(var h=SHA256.Create()) return BitConverter.ToString(h.ComputeHash(value)).Replace("-", "").ToLowerInvariant(); }
        internal static Dictionary<string,object> Json(string value) {
            try { var j=new JavaScriptSerializer(); j.MaxJsonLength=524288; j.RecursionLimit=32; return (Dictionary<string,object>)j.DeserializeObject(value); }
            catch { throw new Failure("METADATA"); }
        }
        internal static string S(Dictionary<string,object> x,string k) { if(!x.ContainsKey(k)||!(x[k] is string)) throw new Failure("METADATA"); return (string)x[k]; }
        internal static int I(Dictionary<string,object> x,string k) { try { return checked(Convert.ToInt32(x[k])); } catch { throw new Failure("METADATA"); } }
        internal static Dictionary<string,object> Obj(Dictionary<string,object> x,string k) { try { return (Dictionary<string,object>)x[k]; } catch { throw new Failure("METADATA"); } }
        internal static string FileText(string p) {
            var f=new FileInfo(p); if(!f.Exists || f.Length>524288 || (f.Attributes&FileAttributes.ReparsePoint)!=0) throw new Failure("METADATA");
            return File.ReadAllText(p,Encoding.UTF8);
        }
        internal static bool SamePath(string a,string b) { return String.Equals(Path.GetFullPath(a).TrimEnd('\\'),Path.GetFullPath(b).TrimEnd('\\'),StringComparison.OrdinalIgnoreCase); }
        internal static string Quote(string value) {
            var b=new StringBuilder("\""); int slash=0;
            foreach(char c in value) { if(c=='\\') { slash++; continue; } if(c=='\"') b.Append('\\',slash*2+1); else b.Append('\\',slash); b.Append(c); slash=0; }
            b.Append('\\',slash*2); return b.Append('"').ToString();
        }
        internal static void Binding(Dictionary<string,object> x,Dictionary<string,object> expected) {
            foreach(string k in new[]{"root_hash","source_hash","lock_sha256"})
                if(!Regex.IsMatch(S(x,k),"^[a-f0-9]{64}$") || S(x,k)!=S(expected,k)) throw new Failure("IDENTITY");
        }
    }
    static class Native {
        [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)] internal static extern IntPtr CreateFile(string p,uint a,uint s,IntPtr sa,uint c,uint f,IntPtr t);
        [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern uint GetFinalPathNameByHandle(IntPtr h,StringBuilder b,uint n,uint flags);
        [DllImport("kernel32.dll",SetLastError=true)] internal static extern bool CloseHandle(IntPtr h);
        [DllImport("kernel32.dll",SetLastError=true)] internal static extern IntPtr OpenProcess(uint a,bool inherit,int id);
        [DllImport("kernel32.dll",SetLastError=true)] static extern bool GetProcessTimes(IntPtr h,out long c,out long e,out long k,out long u);
        [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern bool QueryFullProcessImageName(IntPtr h,int f,StringBuilder b,ref int n);
        [DllImport("kernel32.dll")] static extern uint WaitForSingleObject(IntPtr h,uint ms);
        [StructLayout(LayoutKind.Sequential)] struct Basic { public IntPtr a,b,c,d,id,parent; }
        [DllImport("ntdll.dll")] static extern int NtQueryInformationProcess(IntPtr h,int cls,out Basic b,int len,out int used);
        [DllImport("iphlpapi.dll",SetLastError=true)] static extern uint GetExtendedTcpTable(IntPtr b,ref int size,bool sort,int af,int cls,uint reserved);
        [DllImport("shell32.dll",CharSet=CharSet.Unicode,SetLastError=true)] internal static extern IntPtr CommandLineToArgvW(string command,out int argc);
        [DllImport("kernel32.dll")] internal static extern IntPtr LocalFree(IntPtr h);
        internal static string Root(string value) {
            if(!Regex.IsMatch(value,"^[A-Za-z]:[\\\\]")) throw new Failure("ROOT");
            string p=Path.GetFullPath(value).TrimEnd('\\');
            if(p.Length<4 || !Directory.Exists(p) || new DriveInfo(Path.GetPathRoot(p)).DriveType!=DriveType.Fixed) throw new Failure("ROOT");
            IntPtr h=CreateFile(p,0x80,3,IntPtr.Zero,3,0x02200000,IntPtr.Zero);
            if(h==new IntPtr(-1)) throw new Failure("ROOT");
            try { var b=new StringBuilder(32768); uint n=GetFinalPathNameByHandle(h,b,(uint)b.Capacity,0); if(n==0 || n>=b.Capacity) throw new Failure("ROOT");
                string final=b.ToString(); if(final.StartsWith("\\\\?\\")) final=final.Substring(4);
                if(!Data.SamePath(final,p) || (File.GetAttributes(p)&FileAttributes.ReparsePoint)!=0) throw new Failure("ROOT"); return final.TrimEnd('\\');
            } finally { CloseHandle(h); }
        }
        internal sealed class Identity : IDisposable {
            internal IntPtr Handle; internal int Id,Parent; internal long Ticks; internal string Image;
            internal bool Exited { get { return WaitForSingleObject(Handle,0)==0; } }
            public void Dispose() { if(Handle!=IntPtr.Zero) { CloseHandle(Handle); Handle=IntPtr.Zero; } }
        }
        internal static Identity Process(int id,bool missing) {
            if(id<=0) throw new Failure("IDENTITY");
            IntPtr h=OpenProcess(0x101400,false,id);
            if(h==IntPtr.Zero) { if(missing && Marshal.GetLastWin32Error()==87) return null; throw new Failure("IDENTITY"); }
            try {
                long c,e,k,u; if(!GetProcessTimes(h,out c,out e,out k,out u)) throw new Failure("IDENTITY");
                var b=new StringBuilder(32768); int size=b.Capacity; if(!QueryFullProcessImageName(h,0,b,ref size)) throw new Failure("IDENTITY");
                Basic basic; int used; if(NtQueryInformationProcess(h,0,out basic,Marshal.SizeOf(typeof(Basic)),out used)!=0) throw new Failure("IDENTITY");
                return new Identity { Handle=h,Id=id,Parent=basic.parent.ToInt32(),Ticks=DateTime.FromFileTimeUtc(c).Ticks,Image=b.ToString() };
            } catch { CloseHandle(h); throw; }
        }
        internal static Dictionary<int,List<int>> Listeners() {
            int size=0; uint code=GetExtendedTcpTable(IntPtr.Zero,ref size,false,2,3,0);
            if(code!=122 || size<4 || size>4194304) throw new Failure("PORT");
            IntPtr b=Marshal.AllocHGlobal(size);
            try {
                if(GetExtendedTcpTable(b,ref size,false,2,3,0)!=0) throw new Failure("PORT");
                int count=Marshal.ReadInt32(b); if(count<0 || count>(size-4)/24) throw new Failure("PORT");
                var ports=new Dictionary<int,List<int>>();
                for(int i=0;i<count;i++) {
                    int offset=4+i*24; uint addr=unchecked((uint)Marshal.ReadInt32(b,offset+4));
                    int raw=Marshal.ReadInt32(b,offset+8); int port=((raw&255)<<8)|((raw>>8)&255); int pid=Marshal.ReadInt32(b,offset+20);
                    // Startup is conservative about all interfaces; matched readiness requires literal IPv4 loopback below.
                    int key=(addr==0x0100007fU)?port:-port;
                    if(!ports.ContainsKey(key)) ports[key]=new List<int>(); ports[key].Add(pid);
                } return ports;
            } finally { Marshal.FreeHGlobal(b); }
        }
    }
    sealed class Managed : IDisposable {
        internal List<Native.Identity> Held=new List<Native.Identity>(); internal int ApiId; internal bool Live=true;
        public void Dispose() { foreach(var h in Held) h.Dispose(); Held.Clear(); }
    }
    sealed class Context {
        internal string Root,Key,Python,Bridge; internal int Port; internal bool ExplicitPort;
        internal Dictionary<string,object> Expected;
        internal Process Pending;
        internal Action<string> PhaseChanged;
        internal string Phase { get; private set; }
        void Mark(string phase) { Phase=phase; if(PhaseChanged!=null) PhaseChanged(phase); }
        internal Context(string root,int port,bool explicitPort) {
            Root=Native.Root(root); Port=port; ExplicitPort=explicitPort;
            Key=Data.Hash(Encoding.UTF8.GetBytes(Root.ToUpperInvariant())); Python=Path.Combine(Root,@".venv\Scripts\python.exe"); Bridge=Path.Combine(Root,@"tools\platform_ops\launcher\bridge.ps1");
        }
        internal void Layout(bool stopping=false) {
            foreach(string path in stopping?new string[0]:new[]{"pyproject.toml","uv.lock",".python-version",@"src\finance_platform\__init__.py",@"src\predictor\__init__.py",@"web\dist\index.html",@".venv\Scripts\python.exe"})
                if(!File.Exists(Path.Combine(Root,path))) throw new Failure(path.StartsWith("web")?"FRONTEND":path.StartsWith(".venv")?"PYTHON":"LAYOUT");
            foreach(var pin in BuildPins.Values) {
                if(stopping && pin.Key!="tools/platform_ops/stop.ps1" && pin.Key!="tools/platform_ops/launcher/bridge.ps1") continue;
                string p=Path.Combine(Root,pin.Key.Replace('/',Path.DirectorySeparatorChar));
                if(!File.Exists(p) || (File.GetAttributes(p)&FileAttributes.ReparsePoint)!=0 || Data.Hash(File.ReadAllBytes(p))!=pin.Value) throw new Failure("PINS");
            }
        }
        internal Dictionary<string,object> Call(string action) {
            if(Pending!=null) throw new Failure("PENDING");
            var elapsed=Stopwatch.StartNew(); int budget=action=="start"?90000:30000;
            Func<int> remaining=()=>Math.Max(0,budget-(int)Math.Min(Int32.MaxValue,elapsed.ElapsedMilliseconds));
            Mark(action+".layout"); Layout(action=="stop");
            string request=Guid.NewGuid().ToString("N");
            string ps=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System),@"WindowsPowerShell\v1.0\powershell.exe");
            var info=new ProcessStartInfo(ps); info.UseShellExecute=false; info.CreateNoWindow=true; info.WorkingDirectory=Root;
            info.Arguments="-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "+Data.Quote(Bridge)+" -Action "+action+" -Root "+Data.Quote(Root)+" -Port "+Port+" -RequestId "+request;
            info.RedirectStandardOutput=true; info.RedirectStandardError=true; info.StandardOutputEncoding=Encoding.UTF8; info.StandardErrorEncoding=Encoding.UTF8;
            info.EnvironmentVariables.Remove("PYTHONPATH"); info.EnvironmentVariables.Remove("PYTHONHOME");
            info.EnvironmentVariables["PYTHONDONTWRITEBYTECODE"]="1"; info.EnvironmentVariables["PYTHONIOENCODING"]="utf-8"; info.EnvironmentVariables["PYTHONUTF8"]="1";
            info.EnvironmentVariables["PATH"]=Path.GetDirectoryName(ps)+";"+Environment.GetFolderPath(Environment.SpecialFolder.System)+";"+info.EnvironmentVariables["PATH"];
            var output=new StringBuilder(); bool overflow=false; int lines=0; var p=new Process(); p.StartInfo=info;
            var frameReady=new ManualResetEvent(false); var outputEnded=new ManualResetEvent(false);
            p.OutputDataReceived+=(sender,e)=>{
                if(e.Data==null) { outputEnded.Set(); return; }
                if(String.IsNullOrWhiteSpace(e.Data)) return;
                lock(output) { lines++; if(output.Length+e.Data.Length>524288) overflow=true; else output.AppendLine(e.Data); }
                frameReady.Set();
            };
            p.ErrorDataReceived+=(sender,e)=>{}; // Drain/discard; no raw captures or token logs.
            Mark(action+".bridge.start"); p.Start(); p.BeginOutputReadLine(); p.BeginErrorReadLine();
            Mark(action+".bridge.exit.wait");
            if(!p.WaitForExit(remaining())) {
                Mark(action+".bridge.exit.pending"); Pending=p; throw new Failure("PENDING");
            }
            Mark(action+".bridge.exit.seen"); int code=p.ExitCode;
            try {
                // A complete final frame plus bridge exit commits the response.
                // Descendants may retain pipe handles; EOF is not a success gate.
                Mark(action+".frame.wait");
                int signal=WaitHandle.WaitAny(new WaitHandle[]{frameReady,outputEnded},remaining());
                if(signal!=0) { Mark(action+".frame.missing"); throw new Failure("FRAME"); }
                string text; lock(output) { if(overflow || lines!=1) throw new Failure("FRAME"); text=output.ToString(); output.Clear(); }
                var frame=Data.Json(text); text=null;
                if(Data.S(frame,"protocol")!="FinanceAnalyzeLauncher/1" || Data.S(frame,"request_id")!=request || Data.S(frame,"action")!=action) throw new Failure("FRAME");
                if(code!=0 || Data.S(frame,"kind")!="ok") throw new Failure(frame.ContainsKey("code")?Data.S(frame,"code"):"BRIDGE_FAILED");
                if(action!="stop") {
                    var binding=Data.Obj(frame,"binding");
                    if(!Data.SamePath(Data.S(binding,"root"),Root)) throw new Failure("IDENTITY");
                    foreach(string field in new[]{"root_hash","source_hash","lock_sha256"}) if(!Regex.IsMatch(Data.S(binding,field),"^[a-f0-9]{64}$")) throw new Failure("FRAME");
                    if(Data.S(binding,"root_hash")!=Data.Hash(Encoding.UTF8.GetBytes(Data.S(binding,"root")))) throw new Failure("IDENTITY");
                    if(action!="inspect") { if(Expected==null) throw new Failure("IDENTITY"); Data.Binding(binding,Expected); }
                }
                if(action=="init") {
                    bool hasToken=frame.ContainsKey("setup_token"), initialized=frame.ContainsKey("initialized") && frame["initialized"] is bool && (bool)frame["initialized"];
                    if(hasToken==initialized || (hasToken && !Regex.IsMatch(Data.S(frame,"setup_token"),"^[A-Za-z0-9_-]{32,128}$"))) throw new Failure("FRAME");
                    if(hasToken && (Data.S(frame,"setup_kind")!="first_admin" && Data.S(frame,"setup_kind")!="upgrade_personal" || Data.I(frame,"expires_in_seconds")!=1800)) throw new Failure("FRAME");
                }
                if(action=="prepare-mfa-key") {
                    string result=Data.S(frame,"mfa_key");
                    if(result!="prepared" && result!="already_configured") throw new Failure("FRAME");
                }
                Mark(action+".frame.accepted"); return frame;
            } finally {
                // Cancellation stops our reads; never terminate a descendant here.
                try { p.CancelOutputRead(); } catch {} try { p.CancelErrorRead(); } catch {}
                p.Dispose(); lock(output) output.Clear();
            }
        }
        internal bool HasState { get { return File.Exists(Path.Combine(Root,@".local\platform\processes\native.json")); } }
        internal Managed Verify(bool allowExited) {
            if(Expected==null || !HasState) throw new Failure("STATE"); var result=new Managed();
            try {
                object parsed=new JavaScriptSerializer().DeserializeObject(Data.FileText(Path.Combine(Root,@".local\platform\processes\native.json")));
                var entries=parsed as object[]; if(entries==null || entries.Length<2 || entries.Length>3) throw new Failure("STATE");
                var roles=new HashSet<string>();
                foreach(object item in entries) {
                    var row=item as Dictionary<string,object>; if(row==null) throw new Failure("STATE");
                    string role=Data.S(row,"component"); if(!roles.Add(role) || (role!="api"&&role!="worker"&&role!="console") || !Data.SamePath(Data.S(row,"root"),Root)) throw new Failure("STATE");
                    foreach(string field in new[]{"source_hash","lock_sha256"}) if(Data.S(row,field)!=Data.S(Expected,field)) throw new Failure("IDENTITY");
                    int id=Data.I(row,"process_id"); var parent=Native.Process(id,allowExited);
                    if(parent!=null) {
                        result.Held.Add(parent);
                        if(parent.Ticks!=Convert.ToInt64(row["started_ticks"]) || !Data.SamePath(parent.Image,Data.S(row,"executable"))) throw new Failure("IDENTITY");
                        if(role!="console" && !Data.SamePath(parent.Image,Python)) throw new Failure("INTERPRETER");
                    }
                    if(role=="console") { if(parent==null||parent.Exited) result.Live=false; continue; }
                    string bp=Path.Combine(Root,@".local\platform\processes",role+".binding.json");
                    var binding=Data.Json(Data.FileText(bp)); Data.Binding(binding,Expected);
                    if(!Data.SamePath(Data.S(binding,"root"),Root) || Data.S(binding,"role")!=role) throw new Failure("IDENTITY");
                    var child=Native.Process(Data.I(binding,"process_id"),allowExited);
                    if(child!=null) result.Held.Add(child);
                    if(parent==null || parent.Exited) { if(child!=null&&!child.Exited) throw new Failure("ORPHAN"); result.Live=false; }
                    else if(child==null || child.Exited) result.Live=false;
                    else {
                        if(child.Id!=parent.Id && child.Parent!=parent.Id) throw new Failure("IDENTITY");
                        double recorded=Convert.ToDouble(binding["recorded_at"]); double born=(new DateTime(child.Ticks,DateTimeKind.Utc)-new DateTime(1970,1,1,0,0,0,DateTimeKind.Utc)).TotalSeconds;
                        if(recorded<born-2 || recorded>(DateTime.UtcNow-new DateTime(1970,1,1,0,0,0,DateTimeKind.Utc)).TotalSeconds+10) throw new Failure("IDENTITY");
                        if(role=="api") result.ApiId=child.Id;
                    }
                }
                if(!roles.Contains("api")||!roles.Contains("worker") || (!allowExited&&!result.Live)) throw new Failure("STATE");
                return result;
            } catch { result.Dispose(); throw; }
        }
        internal void FreshPort() {
            foreach(var ep in IPGlobalProperties.GetIPGlobalProperties().GetActiveTcpListeners()) if(ep.Port==Port) throw new Failure("PORT_BUSY");
        }
        internal void CheckOwner(Managed state) {
            if(!state.Live || state.ApiId==0) throw new Failure("STATE");
            var listeners=Native.Listeners(); var owned=new List<int>();
            foreach(var item in listeners) if(item.Key>0 && item.Value.Contains(state.ApiId)) owned.Add(item.Key);
            if(owned.Count!=1) throw new Failure("PORT_OWNER");
            if(ExplicitPort && Port!=owned[0]) throw new Failure("PORT_ARGUMENT"); Port=owned[0];
            if(listeners[Port].Count!=1 || listeners[Port][0]!=state.ApiId || listeners.ContainsKey(-Port)) throw new Failure("PORT_OWNER");
        }
        internal byte[] Get(string route) {
            var req=(HttpWebRequest)WebRequest.Create("http://127.0.0.1:"+Port+route); req.Proxy=null; req.AllowAutoRedirect=false; req.Timeout=2000; req.ReadWriteTimeout=2000; req.UseDefaultCredentials=false;
            using(var response=(HttpWebResponse)req.GetResponse()) {
                if(response.StatusCode!=HttpStatusCode.OK) throw new Failure("READY");
                using(var input=response.GetResponseStream()) using(var output=new MemoryStream()) {
                    byte[] b=new byte[8192]; int n; while((n=input.Read(b,0,b.Length))!=0) { if(output.Length+n>1048576) throw new Failure("READY"); output.Write(b,0,n); } return output.ToArray();
                }
            }
        }
        internal Dictionary<string,object> Ready(Managed state) {
            CheckOwner(state); Get("/api/v1/health/ready"); Data.Binding(Data.Json(Encoding.UTF8.GetString(Get("/api/v1/health/source"))),Expected);
            if(Data.Hash(Get("/"))!=Data.Hash(File.ReadAllBytes(Path.Combine(Root,@"web\dist\index.html")))) throw new Failure("FRONTEND");
            var accounts=Data.Json(Encoding.UTF8.GetString(Get("/api/v1/auth/status")));
            if(Data.S(accounts,"access_mode")!="accounts" || !accounts.ContainsKey("interactive_login_required") || !(accounts["interactive_login_required"] is bool) || !(bool)accounts["interactive_login_required"]) throw new Failure("ACCOUNT_MODE");
            CheckOwner(state); foreach(var h in state.Held) if(h.Exited) throw new Failure("IDENTITY");
            return accounts;
        }
        internal Dictionary<string,object> Start() {
            Layout(); Expected=Data.Obj(Call("inspect"),"binding"); if(!Data.SamePath(Data.S(Expected,"root"),Root)) throw new Failure("IDENTITY");
            if(!HasState) { FreshPort(); var frame=Call("start"); Data.Binding(Data.Obj(frame,"binding"),Expected); }
            using(var m=Verify(false)) return Ready(m);
        }
        internal static string BrowserUri(int port,bool externalAi) {
            return "http://127.0.0.1:"+port+(externalAi?"/#module-b?research_tab=external":"/");
        }
        internal void Browse(bool externalAi=false) {
            using(var m=Verify(false)) {
                Ready(m);
                Process.Start(new ProcessStartInfo(BrowserUri(Port,externalAi)) { UseShellExecute=true });
            }
        }
        internal Dictionary<string,object> Init() {
            using(var m=Verify(false)) {
                var status=Ready(m);
                if(Data.S(status,"account_state")=="ambiguous") throw new Failure("ACCOUNT_STATE");
                return Call("init");
            }
        }
        internal string PrepareMfa() {
            if(HasState) throw new Failure("MFA_STOP_REQUIRED");
            Expected=Data.Obj(Call("inspect"),"binding");
            return Data.S(Call("prepare-mfa-key"),"mfa_key");
        }
        internal void Stop() {
            using(var m=Verify(true)) {
                Call("stop"); DateTime deadline=DateTime.UtcNow.AddSeconds(5);
                while(true) { bool live=false;foreach(var h in m.Held) if(!h.Exited)live=true;if(!live)break;if(DateTime.UtcNow>=deadline)throw new Failure("STOP_FAILED");Thread.Sleep(50); }
            }
            if(HasState) throw new Failure("STOP_FAILED");
        }
    }
    sealed class MainForm : Form {
        readonly Context context; readonly bool preview; bool busy,ready,canStop; string secret; DateTime expires;
        readonly Label status,details;
        readonly TextBox token=new TextBox();
        readonly Button start=new Button(),open=new Button(),stop=new Button(),copy=new Button(),show=new Button(),init=new Button(),mfa=new Button(),externalAi=new Button(),help=new Button();
        readonly TableLayoutPanel tokenRow;
        System.Windows.Forms.Timer timer=new System.Windows.Forms.Timer();
        internal MainForm(Context value,bool isPreview) {
            context=value; preview=isPreview;
            SuspendLayout();
            AutoScaleDimensions=new SizeF(96,96); AutoScaleMode=AutoScaleMode.Dpi;
            Text="FinanceAnalyze · 启动器"; ClientSize=new Size(680,380); MinimumSize=new Size(560,400);
            Font=new Font("Microsoft YaHei UI",10); StartPosition=FormStartPosition.CenterScreen;
            BackColor=Color.FromArgb(247,249,252);
            var viewport=new Panel { Dock=DockStyle.Fill,AutoScroll=true };
            var content=new TableLayoutPanel { Dock=DockStyle.Top,AutoSize=true,AutoSizeMode=AutoSizeMode.GrowAndShrink,
                ColumnCount=1,Padding=new Padding(28,22,28,22) };
            content.ColumnStyles.Add(new ColumnStyle(SizeType.Percent,100));
            var heading=TextRow("FinanceAnalyze",Color.FromArgb(25,44,70),4);
            heading.Font=new Font("Microsoft YaHei UI",23,FontStyle.Bold);
            var subtitle=TextRow("在这台电脑上做研究和回测",Color.DimGray,22);
            status=TextRow("",ForeColor,8); status.Font=new Font(Font,FontStyle.Bold);
            details=TextRow("",Color.DimGray,14);
            var footer=TextRow("关闭这个窗口后，正在做的任务会继续。",Color.DimGray,0);
            foreach(var label in new[]{heading,subtitle,status,details}) content.Controls.Add(label);
            var root=new TextBox { Text=value==null?"启动器预览":value.Root,
                ReadOnly=true,Dock=DockStyle.Top,BorderStyle=BorderStyle.FixedSingle,Margin=new Padding(0,0,0,12) };
            content.Controls.Add(root);
            start.Text="启动或重试"; open.Text="打开工作台"; stop.Text="停止服务"; init.Text="获取首次设置码";
            mfa.Text="准备双重验证";
            externalAi.Text="连接 AI 助手";
            help.Text="?"; help.AccessibleName="帮助：怎么连接 AI 助手";
            var actions=new FlowLayoutPanel { Dock=DockStyle.Top,AutoSize=true,AutoSizeMode=AutoSizeMode.GrowAndShrink,
                Margin=new Padding(0,0,0,18) };
            foreach(var button in new[]{start,open,stop,init,mfa,externalAi,copy,show}) {
                button.AutoSize=true; button.AutoSizeMode=AutoSizeMode.GrowAndShrink;
                button.Padding=new Padding(12,5,12,5); button.Margin=new Padding(0,0,8,4);
            }
            help.Size=new Size(32,32); help.Margin=new Padding(0,3,10,4);
            actions.Controls.AddRange(new Control[]{start,open,externalAi,help,stop,init,mfa});
            content.Controls.Add(actions);
            tokenRow=new TableLayoutPanel { Dock=DockStyle.Top,ColumnCount=3,AutoSize=true,
                AutoSizeMode=AutoSizeMode.GrowAndShrink,Margin=new Padding(0,0,0,12),Visible=false };
            tokenRow.ColumnStyles.Add(new ColumnStyle(SizeType.Percent,100));
            tokenRow.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
            tokenRow.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
            token.Dock=DockStyle.Fill; token.ReadOnly=true; token.UseSystemPasswordChar=true;
            copy.Text="复制"; show.Text="显示";
            tokenRow.Controls.AddRange(new Control[]{token,copy,show});
            content.Controls.Add(tokenRow);
            content.Controls.Add(footer);
            viewport.Controls.Add(content); Controls.Add(viewport);
            viewport.ClientSizeChanged+=(s,e)=>{
                int width=Math.Max(1,viewport.ClientSize.Width-content.Padding.Horizontal);
                foreach(var label in new[]{heading,subtitle,status,details,footer}) label.MaximumSize=new Size(width,0);
            };
            if(context!=null) context.PhaseChanged=phase=>UI(()=>{ if(busy) details.Text=Progress(phase); });
            start.Click+=(s,e)=>StartWork(); open.Click+=(s,e)=>Work(()=>{ context.Browse(); }); stop.Click+=(s,e)=>Work(()=>{ context.Stop(); UI(()=>{ready=false;canStop=false;ClearSecret();status.Text="已停止工作台。已保存的资料还在。";}); });
            init.Click+=(s,e)=>Work(()=>{ var f=context.Init(); UI(()=>Secret(f)); });
            mfa.Click+=(s,e)=>PrepareMfa();
            externalAi.Click+=(s,e)=>Work(()=>{
                context.Browse(true);
                UI(()=>{ status.Text="已打开 AI 助手连接页面。请在网页继续。"; });
            });
            help.Click+=(s,e)=>MessageBox.Show(this,
                "点“连接 AI 助手”，在网页选你使用的助手。\r\n\r\n"
                +"只想让助手看资料，选“只查看”。想让它帮你下载行情、做回测或整理报告，选“帮我操作”。\r\n\r\n"
                +"点“连接”。如果网页请你验证身份，就在网页输入账号密码。连接页面会告诉你还有哪一步。",
                "怎么连接 AI 助手",MessageBoxButtons.OK,MessageBoxIcon.Information);
            copy.Click+=(s,e)=>{ if(secret!=null&&DateTime.UtcNow<expires) try { Clipboard.SetText(secret); } catch { status.Text="剪贴板暂不可用，请稍后重试"; } };
            show.Click+=(s,e)=>{ token.UseSystemPasswordChar=!token.UseSystemPasswordChar; show.Text=token.UseSystemPasswordChar?"显示":"隐藏"; };
            FormClosing+=(s,e)=>{ if(busy || (context!=null&&context.Pending!=null)) { e.Cancel=true; status.Text="请等当前操作完成后再关闭窗口"; } else ClearSecret(); };
            timer.Interval=1000; timer.Tick+=(s,e)=>{
                if(secret!=null&&DateTime.UtcNow>=expires) ClearSecret();
                if(context!=null&&context.Pending!=null&&context.Pending.HasExited) { context.Pending.Dispose(); context.Pending=null; status.Text="操作已结束。请点“启动或重试”继续"; EnableButtons(); }
            }; timer.Start();
            status.Text=isPreview?"启动器预览":"正在检查软件能否启动…";
            details.Text=isPreview?"第一次使用时，可以在这里领取首次设置码。":"请稍候。准备好后会自动打开工作台。";
            ready=isPreview; EnableButtons(); ResumeLayout(true);
        }
        static Label TextRow(string text,Color color,int spaceBelow) {
            return new Label { Text=text,ForeColor=color,AutoSize=true,Dock=DockStyle.Top,Margin=new Padding(0,0,0,spaceBelow) };
        }
        protected override void OnShown(EventArgs e) { base.OnShown(e); if(!preview) StartWork(); }
        internal void FocusExisting() { if(WindowState==FormWindowState.Minimized) WindowState=FormWindowState.Normal; Show(); Activate(); }
        void UI(Action a) { if(!IsDisposed) BeginInvoke(a); }
        void ClearSecret() { secret=null;token.Clear();token.UseSystemPasswordChar=true;show.Text="显示";copy.Enabled=show.Enabled=false;tokenRow.Visible=false; }
        void Secret(Dictionary<string,object> f) {
            ClearSecret();
            if(f.ContainsKey("setup_token")) {
                string s=Data.S(f,"setup_token");
                if(!Regex.IsMatch(s,"^[A-Za-z0-9_-]{32,128}$")) throw new Failure("FRAME");
                tokenRow.Visible=true;secret=s;token.Text=s;expires=DateTime.UtcNow.AddSeconds(Data.I(f,"expires_in_seconds"));
                status.Text="首次设置码已准备好";
                details.Text=Data.S(f,"setup_kind")=="upgrade_personal"?"在工作台输入设置码，为原有工作区创建账号。新码发出后，旧码就会失效。":"在工作台输入设置码，创建管理员账号。设置码有效 30 分钟，重新获取后旧码失效。";
            } else {
                status.Text="账号已设置好";
                details.Text="请在工作台登录。";
            }
            EnableButtons();
        }
        void EnableButtons() { bool idle=!busy&&(context==null||context.Pending==null); start.Enabled=idle&&!ready; open.Enabled=init.Enabled=externalAi.Enabled=idle&&ready;stop.Enabled=idle&&(ready||canStop);mfa.Enabled=idle;copy.Enabled=show.Enabled=idle&&secret!=null; }
        void PrepareMfa() {
            if(preview) return;
            if(context.HasState) { details.Text="等任务做完后，点“停止服务”，再准备双重验证。"; return; }
            Work(()=>{
                string result=context.PrepareMfa();
                UI(()=>{
                    status.Text=result=="prepared"?"已准备好，可以设置双重验证":"这台电脑已准备过双重验证";
                    details.Text="点“启动或重试”。打开网页的“账号安全”，跟着步骤设置验证器。";
                });
            });
        }
        void StartWork() {
            Work(()=>{
                var f=context.Start();
                bool needsSetup=f.ContainsKey("setup_kind")&&f["setup_kind"] is string;
                string hint=Data.S(f,"account_state")=="ambiguous"?"请先试着用现有管理员账号登录。需要帮助时，请查看维护说明。":
                    needsSetup?"第一次使用，请点“获取首次设置码”，然后在网页创建账号。":"请在网页上登录。";
                UI(()=>{ready=true;canStop=true;ClearSecret();status.Text="工作台已准备好";details.Text=hint;});
                context.Browse();
            });
        }
        void Work(Action action) {
            if(busy||preview) return; busy=true; EnableButtons(); status.Text="正在检查，请稍候…";
            Task.Factory.StartNew(()=>{
                string error=null; bool safeStop=false; try { action(); } catch(Failure f) { error=f.Code; } catch { error="CHECK_FAILED"; }
                if(error!=null && context.Expected!=null && context.HasState) try { using(var held=context.Verify(true)) safeStop=true; } catch {}
                UI(()=>{ busy=false; if(error!=null) {ready=(error=="MFA_STOP_REQUIRED"||error=="ACCOUNT_STATE"||error=="ACCOUNT_DB_CHANGED")&&ready&&safeStop;canStop=safeStop;ClearSecret();status.Text=Message(error);
                    details.Text=error=="MFA_STOP_REQUIRED"?"请先点击“停止服务”，再准备双重验证。":
                        error=="MFA_PREPARE_FAILED"?"请按维护说明检查已有的验证器和恢复方式。":
                        error=="ACCOUNT_STATE"?"如果已有管理员账号，请在工作台登录。也可查看维护说明。":
                        error=="ACCOUNT_DB_CHANGED"?"请核对账号数据位置。等任务完成后停止服务，再重新启动获取设置码。":
                        "请先查看软件目录中 docs 下的 OPERATIONS.md，按步骤排查。";} EnableButtons(); });
            });
        }
        static string Progress(string phase) {
            string operation=phase.StartsWith("start.")?"正在启动工作台":phase.StartsWith("stop.")?"正在停止后台服务":phase.StartsWith("init.")?"正在获取首次设置码":phase.StartsWith("prepare-mfa-key.")?"正在准备双重验证":"正在检查软件";
            if(phase.EndsWith(".layout")) return operation+"。正在检查文件。";
            if(phase.EndsWith(".bridge.start")) return operation+"。请稍候。";
            if(phase.EndsWith(".bridge.exit.wait")) return operation+"。请等这次操作完成。";
            if(phase.EndsWith(".bridge.exit.pending")) return operation+"。正在等待上一次操作结束。";
            if(phase.Contains(".frame.")) return operation+"。正在确认是否完成。";
            return operation+"。正在确认结果。";
        }
        internal static string Message(string code) {
            switch(code) {
                case "ROOT":return "找不到本机软件目录。请从安装目录打开启动器";
                case "FRONTEND":return "工作台网页文件缺失或版本不同。请检查安装是否完整";
                case "PYTHON":return "软件缺少运行组件。请按安装说明补齐。";
                case "LAYOUT":return "软件文件不齐。请在完整安装目录中打开 FinanceAnalyze.exe";
                case "PINS":return "启动器与软件文件的版本不同。请使用这次安装配套的启动器";
                case "PORT_BUSY":return "别的程序正在用这个本机地址。工作台没能打开。";
                case "PORT_ARGUMENT":return "工作台已经在另一个地址打开。请用原来的入口。";
                case "PENDING":case "OPERATION_BUSY":return "请等当前操作完成，再试一次";
                case "DEPENDENCY_CHECK_FAILED":return "启动需要的组件缺失或版本不符。请按维护说明检查安装";
                case "SOURCE_CHECK_FAILED":case "START_FAILED":return "工作台未能启动。请按维护说明检查";
                case "STOP_FAILED":return "尚未确认服务已停止。请按维护说明检查";
                case "INTERPRETER":return "这份工作台由另一套软件启动。请用原来的启动器。";
                case "ACCOUNT_MODE":return "工作台还没有启用账号登录。等任务完成后停止服务，再打开";
                case "ACCOUNT_STATE":return "账号数据需要检查，暂时无法生成首次设置码";
                case "ACCOUNT_DB_CHANGED":return "账号数据位置与运行中的服务不同，暂时无法生成设置码";
                case "MFA_STOP_REQUIRED":return "准备双重验证需要先停止本地服务";
                case "MFA_PREPARE_FAILED":return "无法准备双重验证";
                default:return "无法确认工作台是否可以安全操作。请按维护说明检查";
            }
        }
    }
    static class Program {
        [STAThread] static int Main(string[] args) {
            try {
                if(args.Length==1&&args[0]=="--self-test") { SelfTest(); Console.WriteLine("{\"status\":\"PASS\",\"checks\":\"argv-native-roundtrip,own-process-handle,JSON,hash\",\"services_started\":0}");return 0; }
                if(args.Length==2&&args[0]=="--preview") { Application.EnableVisualStyles(); using(var form=new MainForm(null,true)) using(var bitmap=new Bitmap(form.Width,form.Height)) { form.CreateControl(); form.DrawToBitmap(bitmap,new Rectangle(Point.Empty,bitmap.Size)); using(var stream=new FileStream(args[1],FileMode.CreateNew)) bitmap.Save(stream,System.Drawing.Imaging.ImageFormat.Png); } return 0; }
                string root=AppDomain.CurrentDomain.BaseDirectory;int port=8000;bool explicitPort=false,layout=false;var seen=new HashSet<string>();
                for(int i=0;i<args.Length;i++) {
                    if(!seen.Add(args[i])) throw new Failure("ROOT");
                    if(args[i]=="--root"&&i+1<args.Length) root=args[++i];
                    else if(args[i]=="--port"&&i+1<args.Length) {port=Int32.Parse(args[++i]);explicitPort=true;}
                    else if(args[i]=="--check-layout") layout=true;else throw new Failure("ROOT");
                }
                if(port<1024||port>65535) throw new Failure("PORT");var context=new Context(root,port,explicitPort);
                if(layout) { context.Layout();Console.WriteLine("{\"status\":\"LAYOUT_OK_ONLY\",\"services_started\":0}");return 0; }
                bool created;using(var mutex=new Mutex(true,"Global\\FinanceAnalyze.Window."+context.Key,out created)) {
                    bool held=created;if(!held)try{held=mutex.WaitOne(0);}catch(AbandonedMutexException){held=true;}
                    if(!held) { try {using(var signal=EventWaitHandle.OpenExisting("Global\\FinanceAnalyze.Show."+context.Key))signal.Set();}catch{} return 0; }
                    try { using(var signal=new EventWaitHandle(false,EventResetMode.AutoReset,"Global\\FinanceAnalyze.Show."+context.Key)) {
                        Application.EnableVisualStyles();Application.SetCompatibleTextRenderingDefault(false);
                        using(var form=new MainForm(context,false)) {
                            var wait=ThreadPool.RegisterWaitForSingleObject(signal,(s,t)=>{if(form.IsHandleCreated&&!form.IsDisposed)form.BeginInvoke((Action)form.FocusExisting);},null,-1,false);
                            try {Application.Run(form);} finally {wait.Unregister(null);}
                        }
                    }}finally {mutex.ReleaseMutex();}
                }return 0;
            } catch(Failure f) { if(Diagnostic(args)) Console.WriteLine("{\"status\":\"REFUSED\",\"code\":\""+f.Code+"\",\"services_started\":0}");else MessageBox.Show(MainForm.Message(f.Code),"FinanceAnalyze",MessageBoxButtons.OK,MessageBoxIcon.Information);return 2; }
            catch { if(Diagnostic(args))Console.WriteLine("{\"status\":\"REFUSED\",\"code\":\"CHECK_FAILED\",\"services_started\":0}");else MessageBox.Show("启动器无法完成检查。请按软件目录中的维护说明排查。","FinanceAnalyze",MessageBoxButtons.OK,MessageBoxIcon.Information);return 2; }
        }
        static bool Diagnostic(string[] a) { return Array.IndexOf(a,"--self-test")>=0||Array.IndexOf(a,"--check-layout")>=0||Array.IndexOf(a,"--preview")>=0; }
        static void SelfTest() {
            foreach(string value in new[]{"D:\\测试 空格\\FinanceAnalyze", "a&b;'$(x)", "trailing\\", "quote\"inside", ""}) {
                int count;IntPtr p=Native.CommandLineToArgvW("test.exe "+Data.Quote(value),out count);if(p==IntPtr.Zero)throw new Failure("SELFTEST");
                try {if(count!=2||Marshal.PtrToStringUni(Marshal.ReadIntPtr(p,IntPtr.Size))!=value)throw new Failure("SELFTEST");}finally{Native.LocalFree(p);}
            }
            using(var self=Native.Process(Process.GetCurrentProcess().Id,false)) if(self.Id!=Process.GetCurrentProcess().Id||self.Exited||self.Ticks<=0)throw new Failure("SELFTEST");
            if(Data.S(Data.Json("{\"kind\":\"ok\"}"),"kind")!="ok"||Data.Hash(new byte[0])!="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")throw new Failure("SELFTEST");
        }
    }
}
