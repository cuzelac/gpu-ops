<#
.SYNOPSIS
  Keep the 1Password service account token for gpu-ops in Windows Credential Manager.

.DESCRIPTION
  The token lets gpu-ops read the RunPod key with no 1Password prompt (unattended runs, the reaper).
  It is stored as a Windows "generic credential", encrypted to your Windows login, never in a file.

    set     Prompt for the token (input hidden) and store it.   Run this yourself, interactively.
    test    Exit 0 if a token is stored, 1 if not. Prints nothing secret.
    get     Print the token to stdout, no newline. Used by gpu-ops; don't run it in a shared terminal.
    remove  Delete the stored token.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File C:\Users\cuzel\src\gpu-ops\bin\gpuops-credential.ps1 set
#>
param(
    [Parameter(Mandatory = $true, Position = 0)][ValidateSet('set', 'get', 'test', 'remove')][string]$Action,
    [string]$Target = 'gpu-ops/op-service-account'
)
$ErrorActionPreference = 'Stop'

$src = @"
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;

public static class GpuOpsCred {
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    struct CREDENTIAL {
        public int Flags; public int Type; public string TargetName; public string Comment;
        public System.Runtime.InteropServices.ComTypes.FILETIME LastWritten;
        public int CredentialBlobSize; public IntPtr CredentialBlob; public int Persist;
        public int AttributeCount; public IntPtr Attributes; public string TargetAlias; public string UserName;
    }
    const int CRED_TYPE_GENERIC = 1;
    const int CRED_PERSIST_LOCAL_MACHINE = 2;

    [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern bool CredRead(string target, int type, int flags, out IntPtr cred);
    [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern bool CredWrite(ref CREDENTIAL cred, int flags);
    [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern bool CredDelete(string target, int type, int flags);
    [DllImport("advapi32.dll")]
    static extern void CredFree(IntPtr cred);

    // UTF-8 blob: service account tokens are long, and the blob limit is 2560 bytes.
    public static string Read(string target) {
        IntPtr p;
        if (!CredRead(target, CRED_TYPE_GENERIC, 0, out p)) return null;
        try {
            var c = (CREDENTIAL)Marshal.PtrToStructure(p, typeof(CREDENTIAL));
            var bytes = new byte[c.CredentialBlobSize];
            Marshal.Copy(c.CredentialBlob, bytes, 0, bytes.Length);
            return Encoding.UTF8.GetString(bytes);
        } finally { CredFree(p); }
    }
    public static void Write(string target, string secret) {
        var bytes = Encoding.UTF8.GetBytes(secret);
        if (bytes.Length > 2560) throw new ArgumentException("token longer than the 2560-byte credential limit");
        var blob = Marshal.AllocHGlobal(bytes.Length);
        try {
            Marshal.Copy(bytes, 0, blob, bytes.Length);
            var c = new CREDENTIAL { Type = CRED_TYPE_GENERIC, TargetName = target, UserName = "gpu-ops",
                                     Comment = "1Password service account token for gpu-ops",
                                     CredentialBlob = blob, CredentialBlobSize = bytes.Length,
                                     Persist = CRED_PERSIST_LOCAL_MACHINE };
            if (!CredWrite(ref c, 0)) throw new Win32Exception(Marshal.GetLastWin32Error());
        } finally {
            for (int i = 0; i < bytes.Length; i++) Marshal.WriteByte(blob, i, 0);
            Marshal.FreeHGlobal(blob);
        }
    }
    public static bool Delete(string target) { return CredDelete(target, CRED_TYPE_GENERIC, 0); }
}
"@
if (-not ('GpuOpsCred' -as [type])) { Add-Type -TypeDefinition $src }

switch ($Action) {
    'set' {
        $secure = Read-Host -AsSecureString "Paste the 1Password service account token (input hidden)"
        $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
        try {
            $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr).Trim()
            if (-not $plain.StartsWith('ops_')) { throw "That doesn't look like a service account token (they start with 'ops_'). Nothing stored." }
            [GpuOpsCred]::Write($Target, $plain)
            Write-Host "Stored in Windows Credential Manager as '$Target'."
        } finally {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
            $plain = $null
        }
    }
    'get' {
        $t = [GpuOpsCred]::Read($Target)
        if ($null -eq $t) { exit 1 }
        [Console]::Out.Write($t)
    }
    'test' {
        if ($null -eq [GpuOpsCred]::Read($Target)) { Write-Host "no token stored as '$Target'"; exit 1 }
        Write-Host "token present as '$Target'"
    }
    'remove' {
        if ([GpuOpsCred]::Delete($Target)) { Write-Host "removed '$Target'" } else { Write-Host "nothing stored as '$Target'" }
    }
}
