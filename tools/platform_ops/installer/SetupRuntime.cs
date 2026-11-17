using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Threading;
using System.Web.Script.Serialization;

namespace FinanceAnalyzeSetup {
    internal static class SetupRuntime {
        static string Quote(string value) {
            // Only absolute paths to files/directories and fixed arguments are used.
            if (value.IndexOf('"') >= 0) throw new InvalidOperationException("INVALID_ARGUMENT");
            return "\"" + value + "\"";
        }

        static string Run(string exe, string args, string root, string cache, bool source, CancellationToken cancel) {
            PackageInstaller.CheckPath(exe);
            var start = new ProcessStartInfo(exe, args) {
                WorkingDirectory = root, CreateNoWindow = true, UseShellExecute = false,
                RedirectStandardOutput = true, RedirectStandardError = true
            };
            var inherited = start.EnvironmentVariables;
            foreach (string key in inherited.Keys.Cast<string>().ToArray())
                if (key.StartsWith("UV_", StringComparison.OrdinalIgnoreCase)
                    || key.StartsWith("PYTHON", StringComparison.OrdinalIgnoreCase)
                    || key.Equals("VIRTUAL_ENV", StringComparison.OrdinalIgnoreCase)
                    || key.Equals("CONDA_PREFIX", StringComparison.OrdinalIgnoreCase))
                    inherited.Remove(key);
            inherited["UV_PROJECT_ENVIRONMENT"] = Path.Combine(root, ".venv");
            if (cache != null) inherited["UV_CACHE_DIR"] = cache;
            inherited["UV_LINK_MODE"] = "copy";
            inherited["PYTHONDONTWRITEBYTECODE"] = "1";
            if (source) inherited["PYTHONPATH"] = Path.Combine(root, "src");
            cancel.ThrowIfCancellationRequested();
            using (var process = Process.Start(start)) {
                var stdout = process.StandardOutput.ReadToEndAsync();
                var stderr = process.StandardError.ReadToEndAsync();
                var watch = Stopwatch.StartNew();
                while (!process.WaitForExit(150)) {
                    if (!cancel.IsCancellationRequested && watch.Elapsed < TimeSpan.FromMinutes(15)) continue;
                    try { if (!process.HasExited) process.Kill(); } catch (InvalidOperationException) {}
                    process.WaitForExit(5000);
                    cancel.ThrowIfCancellationRequested();
                    throw new InvalidOperationException("PREPARE_TIMEOUT");
                }
                if (process.ExitCode != 0) throw new InvalidOperationException(
                    Path.GetFileName(exe) + "_EXIT_" + process.ExitCode.ToString());
                if (!stdout.Wait(5000) || !stderr.Wait(5000))
                    throw new InvalidOperationException("PREPARE_OUTPUT_TIMEOUT");
                return stdout.Result.Trim();
            }
        }

        static void CopyCache(string seed, string working, CancellationToken cancel) {
            PackageInstaller.CheckPath(seed);
            PackageInstaller.CheckPath(working);
            Directory.CreateDirectory(working);
            foreach (string path in Directory.EnumerateFiles(seed)) {
                cancel.ThrowIfCancellationRequested();
                PackageInstaller.CheckPath(path);
                File.Copy(path, Path.Combine(working, Path.GetFileName(path)));
            }
            foreach (string folder in Directory.EnumerateDirectories(seed))
                CopyCache(folder, Path.Combine(working, Path.GetFileName(folder)), cancel);
        }

        internal static void Prepare(string root, string pendingBinding, string sourceHash, string lockHash,
                                     CancellationToken cancel, Action<string> phase) {
            string embedded = Path.Combine(root, "runtime", "python", "python.exe");
            string uv = Path.Combine(root, "runtime", "uv.exe");
            string environment = Path.Combine(root, ".venv");
            string python = Path.Combine(environment, "Scripts", "python.exe");
            PackageInstaller.CheckPath(environment);
            if (Run(embedded, "-I -B --version", root, null, false, cancel) != "Python 3.12.14")
                throw new InvalidOperationException("PYTHON_VERSION_MISMATCH");
            if (!Run(uv, "--version", root, null, false, cancel).StartsWith("uv 0.12.5 ", StringComparison.Ordinal))
                throw new InvalidOperationException("UV_VERSION_MISMATCH");
            phase("正在准备锁定依赖");
            string cache = Path.Combine(root, ".fauv-" + Guid.NewGuid().ToString("N").Substring(0, 12));
            CopyCache(Path.Combine(root, "runtime", "uv-cache"), cache, cancel);
            // The locked jsonpath dependency is source-only. Its verified seed and pinned
            // pure-Python build backend are bundled, so preparation stays offline.
            Run(uv, "sync --locked --offline --no-config --no-install-project --no-python-downloads --python "
                + Quote(embedded) + " --project " + Quote(root), root, cache, false, cancel);
            if (Run(python, "-I -B --version", root, null, false, cancel) != "Python 3.12.14")
                throw new InvalidOperationException("ENVIRONMENT_VERSION_MISMATCH");
            phase("正在校验程序、锁文件和环境");
            var info = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(
                Run(python, "-P -B -m finance_platform.operations.source_binding --root " + Quote(root),
                    root, null, true, cancel));
            if (!Equals(info["source_hash"], sourceHash) || !Equals(info["lock_sha256"], lockHash)
                || !String.Equals((string)info["root"], root, StringComparison.OrdinalIgnoreCase))
                throw new InvalidOperationException("SOURCE_BINDING_MISMATCH");
            Run(python, "-P -B -m finance_platform.operations --root " + Quote(root) + " doctor",
                root, null, true, cancel);
            phase("正在生成本机数据连接绑定");
            string tool = Path.Combine(root, @"src\finance_platform\data\provider_sdk\runtime_binding_tool.py");
            PackageInstaller.CheckPath(tool);
            try {
                Run(python, "-I -B -X utf8 " + Quote(tool) + " --python " + Quote(python)
                    + " --output " + Quote(pendingBinding), root, null, false, cancel);
                const string validate = "import json,sys; from pathlib import Path; "
                    + "from finance_platform.data.provider_sdk.runner import SDKRunner; "
                    + "binding=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8')); "
                    + "runner=SDKRunner(None,python=sys.executable,pins={},source_root=Path(sys.argv[2])/'src',runtime_binding=binding); "
                    + "sys.exit(0 if runner.base_python.resolve()==Path(sys.argv[3]).resolve() else 2)";
                Run(python, "-P -B -X utf8 -c " + Quote(validate) + " " + Quote(pendingBinding)
                    + " " + Quote(root) + " " + Quote(embedded), root, null, true, cancel);
            } catch (InvalidOperationException) {
                throw new InvalidOperationException("SDK_BINDING_GENERATION_FAILED");
            }
        }
    }
}
