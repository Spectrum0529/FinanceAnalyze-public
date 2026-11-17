using System;
using System.Collections.Generic;
using System.IO;
using System.IO.Compression;
using System.Linq;
using System.Security.AccessControl;
using System.Security.Cryptography;
using System.Security.Principal;
using System.Text;
using System.Threading;
using Microsoft.Win32;

namespace FinanceAnalyzeSetup {
    internal static class PackageInstaller {
        const string Launcher = "FinanceAnalyze.exe";
        const string Pending = ".financeanalyze-installing";
        const string Complete = ".financeanalyze-installed";
        const string Binding = "src/finance_platform/data/provider_sdk/runtime_binding.json";
        const string BindingReceipt = ".financeanalyze-sdk-binding";
        static readonly string[] ProgramRoots = { "src/", "web/dist/", "tools/platform_ops/",
            "configs/", "vendor/wheels/", "workers/tradingagents/vendor/wheels/", "runtime/python/",
            "runtime/uv-cache/", "licenses/", "docs/" };
        static readonly string[] ProgramFiles = { Launcher, "uv.lock", "pyproject.toml",
            ".python-version", "README.md", "README.zh-CN.md", "LICENSE", "THIRD_PARTY_NOTICES.md", "runtime/uv.exe",
            "resources/prediction_protocol.json", "resources/data_requirements.json",
            "samples/public/synthetic_market/events.json" };
        // NT SERVICE\TrustedInstaller owns some stock Windows ancestors, including the OS volume.
        static readonly SecurityIdentifier WindowsServicing = new SecurityIdentifier(
            "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464");

        static SecurityIdentifier[] PrivateAccounts() {
            return new[] { WindowsIdentity.GetCurrent().User, new SecurityIdentifier("S-1-5-18"),
                new SecurityIdentifier("S-1-5-32-544") };
        }

        static DirectorySecurity PrivateDirectory() {
            var security = new DirectorySecurity();
            security.SetAccessRuleProtection(true, false);
            foreach (var account in PrivateAccounts())
                security.AddAccessRule(new FileSystemAccessRule(account, FileSystemRights.FullControl,
                    InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit,
                    PropagationFlags.None, AccessControlType.Allow));
            return security;
        }

        static void CheckParentProtection(string root) {
            var trusted = PrivateAccounts();
            // DELETE on the ancestor itself can allow it to be renamed as a whole.
            var danger = FileSystemRights.Delete | FileSystemRights.DeleteSubdirectoriesAndFiles
                | FileSystemRights.ChangePermissions | FileSystemRights.TakeOwnership;
            for (var parent = Directory.GetParent(root); parent != null; parent = parent.Parent) {
                if (!parent.Exists) continue;
                var acl = parent.GetAccessControl(AccessControlSections.Access | AccessControlSections.Owner);
                var owner = (SecurityIdentifier)acl.GetOwner(typeof(SecurityIdentifier));
                if (!trusted.Contains(owner) && !WindowsServicing.Equals(owner))
                    throw new InvalidOperationException("INSTALL_PARENT_SHARED_WRITE");
                foreach (FileSystemAccessRule rule in acl.GetAccessRules(true, true, typeof(SecurityIdentifier))) {
                    var account = (SecurityIdentifier)rule.IdentityReference;
                    if (rule.AccessControlType == AccessControlType.Allow
                        && (rule.PropagationFlags & PropagationFlags.InheritOnly) == 0
                        && (rule.FileSystemRights & danger) != 0
                        && !trusted.Contains(account) && !WindowsServicing.Equals(account))
                        throw new InvalidOperationException("INSTALL_PARENT_SHARED_WRITE");
                }
            }
        }

        static void CheckPrivateRoot(string root) {
            var acl = Directory.GetAccessControl(root);
            var trusted = PrivateAccounts();
            if (!acl.AreAccessRulesProtected || !acl.GetOwner(typeof(SecurityIdentifier)).Equals(trusted[0]))
                throw new InvalidOperationException("INSTALL_ACL_UNSAFE");
            foreach (FileSystemAccessRule rule in acl.GetAccessRules(true, true, typeof(SecurityIdentifier)))
                if (rule.AccessControlType == AccessControlType.Allow
                    && !trusted.Contains((SecurityIdentifier)rule.IdentityReference))
                    throw new InvalidOperationException("INSTALL_ACL_UNSAFE");
        }

        static string Hex(byte[] value) {
            return BitConverter.ToString(value).Replace("-", "").ToLowerInvariant();
        }

        static string Hash(Stream input, CancellationToken cancel) {
            using (var sha = SHA256.Create()) {
                var buffer = new byte[1024 * 1024];
                int count;
                while ((count = input.Read(buffer, 0, buffer.Length)) != 0) {
                    cancel.ThrowIfCancellationRequested();
                    sha.TransformBlock(buffer, 0, count, null, 0);
                }
                sha.TransformFinalBlock(buffer, 0, 0);
                return Hex(sha.Hash);
            }
        }

        internal static void CheckPath(string path) {
            for (string current = Path.GetFullPath(path); current != null;) {
                try {
                    if ((File.GetAttributes(current) & FileAttributes.ReparsePoint) != 0)
                        throw new InvalidOperationException("REPARSE_PATH");
                } catch (FileNotFoundException) {} catch (DirectoryNotFoundException) {}
                var parent = Directory.GetParent(current);
                current = parent == null ? null : parent.FullName;
            }
        }

        static void ValidateEntry(string name) {
            if (String.Equals(name, Binding, StringComparison.OrdinalIgnoreCase))
                throw new InvalidOperationException("PACKAGED_MACHINE_BINDING");
            var parts = name.Split('/');
            foreach (string part in parts) {
                if (part.Length == 0 || part.EndsWith(".") || part.EndsWith(" ")
                    || part.IndexOfAny(Path.GetInvalidFileNameChars()) >= 0
                    || System.Text.RegularExpressions.Regex.IsMatch(part.Split('.')[0],
                        @"^(CON|PRN|AUX|NUL|COM[0-9¹²³]|LPT[0-9¹²³]|CONIN\$|CONOUT\$)$",
                        System.Text.RegularExpressions.RegexOptions.IgnoreCase))
                    throw new InvalidOperationException("UNSAFE_ARCHIVE_PATH");
            }
            if (!ProgramFiles.Contains(name, StringComparer.Ordinal)
                && !ProgramRoots.Any(prefix => name.StartsWith(prefix, StringComparison.Ordinal)))
                throw new InvalidOperationException("UNEXPECTED_PAYLOAD_PATH");
        }

        static string LocalIdentity(string root, string packageHash) {
            using (var key = Registry.LocalMachine.OpenSubKey(@"SOFTWARE\Microsoft\Cryptography")) {
                string machine = key == null ? null : key.GetValue("MachineGuid") as string;
                if (String.IsNullOrWhiteSpace(machine)) throw new InvalidOperationException("MACHINE_ID_UNAVAILABLE");
                using (var sha = SHA256.Create()) {
                    string input = root.ToUpperInvariant() + "\n" + packageHash + "\n" + machine
                        + "\n" + WindowsIdentity.GetCurrent().User.Value;
                    return Hex(sha.ComputeHash(Encoding.UTF8.GetBytes(input)));
                }
            }
        }

        static void Preserve(string file, string backup) {
            CheckPath(file);
            CheckPath(backup);
            Directory.CreateDirectory(Path.GetDirectoryName(backup));
            File.Move(file, backup);
        }

        static string BindingPath(string root) {
            return Path.Combine(root, Binding.Replace('/', Path.DirectorySeparatorChar));
        }

        static void CheckBindingOwnership(string root, string identity, CancellationToken cancel) {
            string binding = BindingPath(root);
            string receipt = Path.Combine(root, BindingReceipt);
            CheckPath(binding);
            CheckPath(receipt);
            if (Directory.Exists(binding) || Directory.Exists(receipt))
                throw new InvalidOperationException("SDK_BINDING_UNRECOGNIZED");
            if (!File.Exists(receipt)) {
                if (File.Exists(binding)) throw new InvalidOperationException("SDK_BINDING_UNRECOGNIZED");
                return;
            }
            var record = File.ReadAllLines(receipt);
            if (record.Length != 2 || record[0] != identity
                || !System.Text.RegularExpressions.Regex.IsMatch(record[1], "^[a-f0-9]{64}$"))
                throw new InvalidOperationException("SDK_BINDING_UNRECOGNIZED");
            if (File.Exists(binding)) {
                using (var input = File.OpenRead(binding))
                    if (Hash(input, cancel) != record[1]) throw new InvalidOperationException("SDK_BINDING_UNRECOGNIZED");
            }
        }

        static void PreserveBinding(string root, string backup) {
            string binding = BindingPath(root);
            if (!File.Exists(binding)) return;
            Preserve(binding, Path.Combine(backup, "runtime_binding.json"));
            File.Copy(Path.Combine(root, BindingReceipt), Path.Combine(backup, "sdk-binding.receipt"));
        }

        static void PrepareRuntime(string root, string identity,
                                   Action<string, string, CancellationToken> prepare, CancellationToken cancel) {
            string binding = BindingPath(root);
            string receipt = Path.Combine(root, BindingReceipt);
            string suffix = Guid.NewGuid().ToString("N");
            string pendingBinding = Path.Combine(root, ".fa-sdk-" + suffix);
            string pendingReceipt = Path.Combine(root, ".fa-sdk-record-" + suffix);
            try {
                prepare(root, pendingBinding, cancel);
                cancel.ThrowIfCancellationRequested();
                string hash;
                CheckPath(pendingBinding);
                using (var input = File.OpenRead(pendingBinding)) hash = Hash(input, cancel);
                using (var stream = new FileStream(pendingReceipt, FileMode.CreateNew, FileAccess.Write,
                                                  FileShare.None)) {
                    byte[] record = Encoding.UTF8.GetBytes(identity + "\n" + hash + "\n");
                    stream.Write(record, 0, record.Length);
                    stream.Flush(true);
                }
                // A crash here leaves no live binding. The same package can
                // validate the receipt and resume with another fresh binding.
                if (File.Exists(receipt)) File.Replace(pendingReceipt, receipt, null);
                else File.Move(pendingReceipt, receipt);
                File.Move(pendingBinding, binding);
                CheckBindingOwnership(root, identity, cancel);
            } finally {
                foreach (var file in new[]{pendingBinding, pendingReceipt})
                    if (File.Exists(file)) File.Delete(file);
            }
        }

        sealed class RepairState {
            readonly string directory;
            readonly string root;
            readonly List<Tuple<string, string>> originals = new List<Tuple<string, string>>();

            internal RepairState(string root) {
                this.root = root;
                directory = Path.Combine(root, ".fa-repair-" + Guid.NewGuid().ToString("N").Substring(0, 12));
            }

            static void MovePreserved(string source, string destination) {
                CheckPath(source);
                CheckPath(destination);
                Directory.CreateDirectory(Path.GetDirectoryName(destination));
                if (Directory.Exists(source)) Directory.Move(source, destination);
                else File.Move(source, destination);
            }

            internal void Save(string path, string name = null) {
                CheckPath(path);
                string previous = null;
                if (File.Exists(path) || Directory.Exists(path)) {
                    previous = Path.Combine(directory, name ?? Guid.NewGuid().ToString("N"));
                    MovePreserved(path, previous);
                }
                originals.Add(Tuple.Create(path, previous));
                if (name == null && previous != null)
                    File.WriteAllText(previous + ".path", path.Substring(root.Length + 1), Encoding.UTF8);
            }

            internal void Rollback() {
                for (int index = originals.Count - 1; index >= 0; index--) {
                    var item = originals[index];
                    if (File.Exists(item.Item1) || Directory.Exists(item.Item1))
                        MovePreserved(item.Item1, Path.Combine(directory, "failed-" + index.ToString()));
                    if (item.Item2 != null) MovePreserved(item.Item2, item.Item1);
                }
            }
        }

        static void CopyEntry(string root, ZipArchiveEntry entry, CancellationToken cancel, RepairState repair = null) {
            string target = Path.Combine(root, entry.FullName.Replace('/', Path.DirectorySeparatorChar));
            CheckPath(target);
            Directory.CreateDirectory(Path.GetDirectoryName(target));
            if (Directory.Exists(target)) throw new InvalidOperationException("PROGRAM_FILE_CHANGED");
            if (File.Exists(target)) {
                using (var current = File.OpenRead(target))
                using (var expected = entry.Open())
                    if (Hash(current, cancel) == Hash(expected, cancel)) return;
                if (repair == null) throw new InvalidOperationException("PROGRAM_FILE_CHANGED");
            }
            if (repair != null) repair.Save(target);
            string temporary = Path.Combine(root, ".fa-" + Guid.NewGuid().ToString("N"));
            try {
                using (var input = entry.Open())
                using (var output = new FileStream(temporary, FileMode.CreateNew, FileAccess.Write, FileShare.None)) {
                    var buffer = new byte[1024 * 1024];
                    int count;
                    while ((count = input.Read(buffer, 0, buffer.Length)) != 0) {
                        cancel.ThrowIfCancellationRequested();
                        output.Write(buffer, 0, count);
                    }
                    output.Flush(true);
                }
                File.Move(temporary, target);
            } finally {
                if (File.Exists(temporary)) File.Delete(temporary);
            }
        }

        internal static void Install(string root, string package, string packageHash,
                                     Action<string, string, CancellationToken> prepare, CancellationToken cancel,
                                     Action<int, int> progress, bool repair = false) {
            root = Path.GetFullPath(root).TrimEnd(Path.DirectorySeparatorChar);
            package = Path.GetFullPath(package);
            if (root.TrimEnd('\\') == Path.GetPathRoot(root).TrimEnd('\\')
                || new DriveInfo(Path.GetPathRoot(root)).DriveType != DriveType.Fixed
                || package.StartsWith(root.TrimEnd('\\') + "\\", StringComparison.OrdinalIgnoreCase))
                throw new InvalidOperationException("INVALID_INSTALL_ROOT");
            CheckPath(root);
            CheckPath(package);
            using (var stream = new FileStream(package, FileMode.Open, FileAccess.Read, FileShare.Read)) {
                if (Hash(stream, cancel) != packageHash) throw new InvalidOperationException("PACKAGE_HASH_MISMATCH");
                stream.Position = 0;
                using (var archive = new ZipArchive(stream, ZipArchiveMode.Read)) {
                    var names = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
                    foreach (var entry in archive.Entries) {
                        ValidateEntry(entry.FullName);
                        if (!names.Add(entry.FullName)) throw new InvalidOperationException("DUPLICATE_PAYLOAD_PATH");
                    }
                    var launcher = archive.GetEntry(Launcher);
                    if (launcher == null) throw new InvalidOperationException("LAUNCHER_MISSING");
                    cancel.ThrowIfCancellationRequested();
                    if (repair && !Directory.Exists(root)) throw new InvalidOperationException("REPAIR_PACKAGE_OR_ROOT_MISMATCH");
                    CheckParentProtection(root);
                    if (!Directory.Exists(root)) Directory.CreateDirectory(root, PrivateDirectory());
                    string pending = Path.Combine(root, Pending);
                    string complete = Path.Combine(root, Complete);
                    CheckPath(pending);
                    CheckPath(complete);
                    string identity = LocalIdentity(root, packageHash);
                    if (repair) {
                        CheckPrivateRoot(root);
                        Repair(root, complete, identity, archive, launcher, prepare, cancel, progress);
                        return;
                    }
                    if (File.Exists(complete)) throw new InvalidOperationException("INSTALL_ALREADY_COMPLETE");
                    if (!File.Exists(pending)) {
                        if (Directory.EnumerateFileSystemEntries(root).Any())
                            throw new InvalidOperationException("INSTALL_ROOT_NOT_EMPTY");
                        if (!Directory.GetAccessControl(root).GetOwner(typeof(SecurityIdentifier))
                            .Equals(WindowsIdentity.GetCurrent().User))
                            throw new InvalidOperationException("INSTALL_ACL_UNSAFE");
                        Directory.SetAccessControl(root, PrivateDirectory());
                        CheckPrivateRoot(root);
                        using (var state = new FileStream(pending, FileMode.CreateNew, FileAccess.Write, FileShare.None))
                        using (var writer = new StreamWriter(state))
                            writer.Write(identity);
                    }
                    using (var state = new FileStream(pending, FileMode.Open, FileAccess.Read, FileShare.Delete))
                    using (var reader = new StreamReader(state)) {
                        CheckPrivateRoot(root);
                        if (reader.ReadToEnd() != identity) throw new InvalidOperationException("INSTALL_IDENTITY_MISMATCH");
                        if (File.Exists(complete)) throw new InvalidOperationException("INSTALL_ALREADY_COMPLETE");
                        CheckBindingOwnership(root, identity, cancel);
                        PreserveBinding(root, Path.Combine(root, ".fa-resume-" + Guid.NewGuid().ToString("N").Substring(0, 12)));
                        int copied = 0;
                        foreach (var entry in archive.Entries.Where(entry => entry != launcher)) {
                            cancel.ThrowIfCancellationRequested();
                            CopyEntry(root, entry, cancel);
                            progress(++copied, archive.Entries.Count);
                        }
                        PrepareRuntime(root, identity, prepare, cancel);
                        cancel.ThrowIfCancellationRequested();
                        CopyEntry(root, launcher, cancel);
                        File.Move(pending, complete);
                        progress(++copied, archive.Entries.Count);
                    }
                }
            }
        }

        static void Repair(string root, string complete, string identity, ZipArchive archive, ZipArchiveEntry launcher,
                           Action<string, string, CancellationToken> prepare, CancellationToken cancel, Action<int, int> progress) {
            if (!File.Exists(complete) || File.ReadAllText(complete) != identity)
                throw new InvalidOperationException("REPAIR_PACKAGE_OR_ROOT_MISMATCH");
            string launcherKey;
            using (var sha = SHA256.Create())
                launcherKey = Hex(sha.ComputeHash(Encoding.UTF8.GetBytes(root.ToUpperInvariant())));
            using (var window = new Mutex(false, @"Global\FinanceAnalyze.Window." + launcherKey)) {
                try {
                    if (!window.WaitOne(0)) throw new InvalidOperationException("REPAIR_WORKBENCH_OPEN");
                } catch (AbandonedMutexException) {}
                try {
                    using (var bridge = new Mutex(false, @"Global\FinanceAnalyze.Operation." + launcherKey)) {
                        try {
                            if (!bridge.WaitOne(0)) throw new InvalidOperationException("REPAIR_NATIVE_OPERATION_BUSY");
                        } catch (AbandonedMutexException) {}
                        try {
                            RepairInstalledProgram(root, identity, archive, launcher, prepare, cancel, progress);
                        } finally {
                            bridge.ReleaseMutex();
                        }
                    }
                } finally {
                    window.ReleaseMutex();
                }
            }
        }

        static void RepairInstalledProgram(string root, string identity, ZipArchive archive, ZipArchiveEntry launcher,
                                          Action<string, string, CancellationToken> prepare, CancellationToken cancel,
                                          Action<int, int> progress) {
            string processDirectory = Path.Combine(root, @".local\platform\processes");
            string operation = Path.Combine(processDirectory, "native-operation.lock");
            string active = Path.Combine(processDirectory, "native.json");
            CheckPath(operation);
            CheckPath(active);
            if (File.Exists(active)) throw new InvalidOperationException("REPAIR_SERVICE_PRESENT");
            Directory.CreateDirectory(processDirectory);
            using (var guard = new FileStream(operation, FileMode.OpenOrCreate, FileAccess.ReadWrite, FileShare.ReadWrite)) {
                try { guard.Lock(0, 1); }
                catch (IOException) { throw new InvalidOperationException("REPAIR_NATIVE_OPERATION_BUSY"); }
                if (File.Exists(active)) throw new InvalidOperationException("REPAIR_SERVICE_PRESENT");
                cancel.ThrowIfCancellationRequested();
                CheckBindingOwnership(root, identity, cancel);
                var previous = new RepairState(root);
                try {
                    previous.Save(Path.Combine(root, Launcher), Launcher);
                    previous.Save(Path.Combine(root, ".venv"), ".venv");
                    previous.Save(BindingPath(root), "runtime_binding.json");
                    previous.Save(Path.Combine(root, BindingReceipt), "sdk-binding.receipt");
                    int copied = 0;
                    foreach (var entry in archive.Entries.Where(entry => entry != launcher)) {
                        cancel.ThrowIfCancellationRequested();
                        CopyEntry(root, entry, cancel, previous);
                        progress(++copied, archive.Entries.Count);
                    }
                    PrepareRuntime(root, identity, prepare, cancel);
                    cancel.ThrowIfCancellationRequested();
                    CopyEntry(root, launcher, cancel);
                    progress(++copied, archive.Entries.Count);
                } catch {
                    previous.Rollback();
                    throw;
                }
            }
        }
    }
}
