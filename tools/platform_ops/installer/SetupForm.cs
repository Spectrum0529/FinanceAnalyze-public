using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;

namespace FinanceAnalyzeSetup {
    internal sealed class SetupForm : Form {
        readonly TextBox folder = new TextBox();
        readonly Label status = new Label();
        readonly ProgressBar progress = new ProgressBar();
        readonly Button browse = new Button();
        readonly Button setup = new Button();
        readonly Button repair = new Button();
        readonly Button details = new Button();
        readonly Button cancel = new Button();
        CancellationTokenSource active;
        string installed;
        string diagnostic;

        internal SetupForm() {
            Text = "FinanceAnalyze 安装";
            StartPosition = FormStartPosition.CenterScreen;
            ClientSize = new Size(648, 290);
            MinimumSize = new Size(630, 300);
            AutoScaleDimensions = new SizeF(96F, 96F);
            AutoScaleMode = AutoScaleMode.Dpi;
            Font = new Font("Microsoft YaHei UI", 10);
            var hint = new Label { Text = "选择安装目录。已有同版安装可检查并修复。", AutoSize = true, Dock = DockStyle.Fill };
            folder.Dock = DockStyle.Fill;
            folder.Text = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                "FinanceAnalyze");
            browse.Text = "浏览…";
            browse.AutoSize = true;
            browse.Click += delegate {
                using (var dialog = new FolderBrowserDialog()) {
                    dialog.Description = "选择新安装目录的上级文件夹";
                    if (dialog.ShowDialog(this) == DialogResult.OK)
                        folder.Text = Path.Combine(dialog.SelectedPath, "FinanceAnalyze");
                }
            };
            progress.Dock = DockStyle.Fill;
            progress.MinimumSize = new Size(0, 18);
            status.Dock = DockStyle.Fill;
            status.Text = "准备安装";
            setup.Text = "安装或继续";
            setup.Click += async delegate { await StartInstall(false); };
            repair.Text = "检查并修复";
            repair.Click += async delegate { await StartInstall(true); };
            details.Text = "查看诊断";
            details.Enabled = false;
            details.Click += delegate {
                MessageBox.Show(this, diagnostic, "安装诊断（Ctrl+C 可复制）", MessageBoxButtons.OK);
            };
            cancel.Text = "关闭";
            cancel.Click += delegate { if (active == null) Close(); else active.Cancel(); };
            var buttons = new FlowLayoutPanel { Dock = DockStyle.Fill, AutoSize = true, WrapContents = true };
            foreach (Button button in new[]{setup, repair, details, cancel}) {
                button.AutoSize = true;
                button.MinimumSize = new Size(96, 34);
                buttons.Controls.Add(button);
            }
            var layout = new TableLayoutPanel { ColumnCount = 2, RowCount = 5, Dock = DockStyle.Fill,
                Padding = new Padding(20), AutoScroll = true };
            layout.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
            layout.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
            for (int row = 0; row < 5; row++)
                layout.RowStyles.Add(new RowStyle(row == 3 ? SizeType.Percent : SizeType.AutoSize, 100));
            layout.Controls.Add(hint, 0, 0); layout.SetColumnSpan(hint, 2);
            layout.Controls.Add(folder, 0, 1); layout.Controls.Add(browse, 1, 1);
            layout.Controls.Add(progress, 0, 2); layout.SetColumnSpan(progress, 2);
            layout.Controls.Add(status, 0, 3); layout.SetColumnSpan(status, 2);
            layout.Controls.Add(buttons, 0, 4); layout.SetColumnSpan(buttons, 2);
            Controls.Add(layout);
            AcceptButton = setup;
        }

        protected override void OnFormClosing(FormClosingEventArgs args) {
            if (active != null) { active.Cancel(); args.Cancel = true; }
            base.OnFormClosing(args);
        }

        static string FailureMessage(string code) {
            switch (code) {
                case "WINDOWS_X64_REQUIRED": return "这份安装包适用于 Windows x64。请在兼容的 Windows 电脑上打开。";
                case "PACKAGE_HASH_MISMATCH":
                case "UNSAFE_ARCHIVE_PATH":
                case "UNEXPECTED_PAYLOAD_PATH":
                case "DUPLICATE_PAYLOAD_PATH":
                case "PACKAGED_MACHINE_BINDING":
                case "LAUNCHER_MISSING": return "发行包未通过完整性验证。请重新下载并解压同一发行包。";
                case "INSTALL_ALREADY_COMPLETE": return "此目录已完成安装。请点“检查并修复”，或直接打开已安装的软件。";
                case "INSTALL_ROOT_NOT_EMPTY": return "此目录已有文件。首次安装请选择新的本机目录。";
                case "INVALID_INSTALL_ROOT": return "请选择本机固定磁盘中的专用目录，且安装包放在该目录之外。";
                case "INSTALL_PARENT_SHARED_WRITE": return "所选位置允许其他用户改动安装目录。请选择你的个人目录或受保护的本机目录。";
                case "INSTALL_ACL_UNSAFE": return "此目录无法验证为你的私有安装目录。请在个人目录中选择一个新位置。";
                case "REPARSE_PATH": return "文件路径包含链接或重定向。请移到普通本机目录后重试。";
                case "REPAIR_WORKBENCH_OPEN":
                case "REPAIR_NATIVE_OPERATION_BUSY":
                case "REPAIR_SERVICE_PRESENT": return "软件仍在运行或有未结束的运维操作。请在原启动器停止服务并关闭窗口，再检查并修复。";
                case "INSTALL_IDENTITY_MISMATCH":
                case "REPAIR_PACKAGE_OR_ROOT_MISMATCH": return "此目录与发行包或此电脑上的安装登记不匹配。请选择原来安装时使用的发行包和目录。";
                case "PROGRAM_FILE_CHANGED": return "中断安装中的程序文件已变化。请选择新目录安装，原文件会保留。";
                case "SDK_BINDING_UNRECOGNIZED": return "现有数据连接绑定与安装记录不符。请保留文件并检查原安装记录，或选择新目录安装。";
                case "SDK_BINDING_GENERATION_FAILED": return "本机数据连接绑定未通过验证。请查看诊断并用同一发行包重试。";
                case "PREPARE_TIMEOUT": return "准备依赖超时。请检查可用磁盘空间后，用同一发行包继续。";
                case "SOURCE_BINDING_MISMATCH":
                case "PYTHON_VERSION_MISMATCH":
                case "UV_VERSION_MISMATCH":
                case "ENVIRONMENT_VERSION_MISMATCH": return "程序或运行环境与发行包版本不一致。请重新下载该发行包。";
                default: return "准备运行环境失败。请检查剩余磁盘空间，打开诊断后再用同一发行包重试。";
            }
        }

        async Task StartInstall(bool repairing) {
            if (installed != null) {
                try { Process.Start(Path.Combine(installed, "FinanceAnalyze.exe")); }
                catch (System.ComponentModel.Win32Exception) { status.Text = "无法打开 FinanceAnalyze，请从安装目录启动。"; }
                return;
            }
            var operation = new CancellationTokenSource();
            active = operation;
            try {
                if (!Environment.Is64BitOperatingSystem) throw new InvalidOperationException("WINDOWS_X64_REQUIRED");
                string root = Path.GetFullPath(folder.Text.Trim());
                string package = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "FinanceAnalyze.payload.zip");
                setup.Enabled = repair.Enabled = browse.Enabled = folder.Enabled = false;
                details.Enabled = false;
                cancel.Text = "取消";
                progress.Style = ProgressBarStyle.Marquee;
                status.Text = "正在验证发行包";
                Action<string> phase = message => BeginInvoke((Action)(() => {
                    progress.Style = ProgressBarStyle.Marquee;
                    status.Text = message;
                }));
                await Task.Run(() => PackageInstaller.Install(root, package, SetupPins.Package,
                    (target, binding, token) => SetupRuntime.Prepare(target, binding, SetupPins.Source, SetupPins.Lock, token, phase),
                    operation.Token, (done, total) => {
                        if (done != total && done % 25 != 0) return;
                        BeginInvoke((Action)(() => {
                            progress.Style = ProgressBarStyle.Continuous;
                            progress.Value = done * 100 / total;
                            status.Text = "正在安装文件";
                        }));
                    }, repairing));
                installed = root;
                progress.Style = ProgressBarStyle.Continuous;
                progress.Value = 100;
                bool shortcut = UserShortcut.Ensure(root);
                status.Text = (repairing ? "修复完成。" : "安装完成。")
                    + (shortcut ? "也可以从开始菜单打开软件。" : "以后也可双击安装目录里的 FinanceAnalyze.exe。");
                setup.Text = "打开软件";
            } catch (OperationCanceledException) {
                status.Text = repairing ? "修复已取消。请使用同一发行包再次点“检查并修复”。"
                    : "安装已取消。重新打开同一发行包可继续。";
            } catch (InvalidOperationException error) {
                diagnostic = error.Message;
                status.Text = FailureMessage(diagnostic);
                details.Enabled = true;
            } catch (FileNotFoundException error) {
                diagnostic = error.GetType().Name;
                status.Text = "找不到安装文件。请重新解压完整 ZIP，并将两个安装文件放在同一目录。";
                details.Enabled = true;
            } catch (UnauthorizedAccessException error) {
                diagnostic = error.GetType().Name;
                status.Text = "无法访问所选目录。请选择你可以写入的本机目录后重试。";
                details.Enabled = true;
            } catch (PathTooLongException error) {
                diagnostic = error.GetType().Name;
                status.Text = "所选目录层级过深。请使用更短的本机路径；已存在的文件会保留。";
                details.Enabled = true;
            } catch (Exception error) {
                diagnostic = error.GetType().Name;
                status.Text = "无法完成文件操作。请检查目录访问权限和可用磁盘空间，再用同一发行包重试。";
                details.Enabled = true;
            } finally {
                progress.Style = ProgressBarStyle.Continuous;
                active = null;
                operation.Dispose();
                setup.Enabled = true;
                repair.Enabled = browse.Enabled = folder.Enabled = installed == null;
                cancel.Text = "关闭";
            }
        }
    }

    static class UserShortcut {
        internal static bool Ensure(string root) {
            string temporary = null;
            object shell = null, shortcut = null;
            try {
                string programs = Environment.GetFolderPath(Environment.SpecialFolder.Programs);
                if (String.IsNullOrEmpty(programs) || !Directory.Exists(programs)) return false;
                string target = Path.Combine(programs, "FinanceAnalyze.lnk");
                if (File.Exists(target)) return false;
                temporary = Path.Combine(programs, "FinanceAnalyze-" + Guid.NewGuid().ToString("N") + ".lnk");
                shell = Activator.CreateInstance(Type.GetTypeFromProgID("WScript.Shell", true));
                shortcut = shell.GetType().InvokeMember("CreateShortcut", BindingFlags.InvokeMethod, null, shell,
                    new object[]{temporary});
                shortcut.GetType().InvokeMember("TargetPath", BindingFlags.SetProperty, null, shortcut,
                    new object[]{Path.Combine(root, "FinanceAnalyze.exe")});
                shortcut.GetType().InvokeMember("WorkingDirectory", BindingFlags.SetProperty, null, shortcut,
                    new object[]{root});
                shortcut.GetType().InvokeMember("Save", BindingFlags.InvokeMethod, null, shortcut, null);
                File.Move(temporary, target);
                return true;
            } catch (Exception) {
                return false;
            } finally {
                if (shortcut != null) Marshal.FinalReleaseComObject(shortcut);
                if (shell != null) Marshal.FinalReleaseComObject(shell);
                if (temporary != null) try { File.Delete(temporary); } catch (IOException) {} catch (UnauthorizedAccessException) {}
            }
        }
    }

    static class SetupProgram {
        [STAThread]
        public static void Main() {
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            Application.Run(new SetupForm());
        }
    }
}
