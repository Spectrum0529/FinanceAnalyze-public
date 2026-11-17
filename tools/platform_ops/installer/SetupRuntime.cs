using System;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Threading;

namespace FinanceAnalyzeSetup {
    internal static class SetupRuntime {
        static string Quote(string value) {
            // Only absolute paths to files/directories and fixed arguments are used.
            if (value.IndexOf('"') >= 0) throw new InvalidOperationException("INVALID_ARGUMENT");
            return "\"" + value + "\"";
        }

        static void Run(string exe, string args, string root, string cache, CancellationToken cancel) {
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

        internal static void Prepare(string root, string pendingBinding, CancellationToken cancel, Action<string> phase) {
            string embedded = Path.Combine(root, "runtime", "python", "python.exe");
            string uv = Path.Combine(root, "runtime", "uv.exe");
            string environment = Path.Combine(root, ".venv");
            string python = Path.Combine(environment, "Scripts", "python.exe");
            PackageInstaller.CheckPath(environment);
            phase("正在准备锁定依赖");
            string cache = Path.Combine(root, ".fauv-" + Guid.NewGuid().ToString("N").Substring(0, 12));
            CopyCache(Path.Combine(root, "runtime", "uv-cache"), cache, cancel);
            // The locked jsonpath dependency is source-only. Its verified seed and pinned
            // pure-Python build backend are bundled, so preparation stays offline.
            Run(uv, "sync --locked --offline --no-config --no-install-project --no-python-downloads --python "
                + Quote(embedded) + " --project " + Quote(root), root, cache, cancel);
            phase("正在生成本机数据连接绑定");
            string tool = Path.Combine(root, @"src\finance_platform\data\provider_sdk\runtime_binding_tool.py");
            PackageInstaller.CheckPath(tool);
            try {
                Run(python, "-I -B -X utf8 " + Quote(tool) + " --python " + Quote(python)
                    + " --output " + Quote(pendingBinding), root, null, cancel);
            } catch (InvalidOperationException) {
                throw new InvalidOperationException("SDK_BINDING_GENERATION_FAILED");
            }
        }
    }
}
