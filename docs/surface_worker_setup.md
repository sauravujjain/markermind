# Surface Laptop 2 — Remote Nesting Worker

## Connection Details
- **IP**: 192.168.50.2 (direct ethernet to gaming PC at 192.168.50.1)
- **Username**: nestworker
- **Password**: Password123!
- **Connection**: Direct ethernet cable, Private network profile

## Quick Connect (from gaming PC PowerShell)
```powershell
$cred = Get-Credential -UserName nestworker -Message "Surface password"
Enter-PSSession -ComputerName 192.168.50.2 -Credential $cred
```

## What's Installed
- Windows 10/11
- Python 3.11+ (winget install)
- spyrrow 0.8.1 (pip install)

## Network Config
- Gaming PC ethernet (ifIndex 22): 192.168.50.1/24, Private profile
- Surface ethernet (ifIndex 39): 192.168.50.2/24, Private profile
- Both have WinRM enabled, TrustedHosts configured
- LocalAccountTokenFilterPolicy = 1 on Surface

## If Connection Fails
1. Check cable is connected
2. Verify IPs: `ping 192.168.50.2` from gaming PC
3. If ping fails, check network profile is Private:
   - Gaming PC: `Set-NetConnectionProfile -InterfaceIndex 22 -NetworkCategory Private`
   - Surface: `Set-NetConnectionProfile -InterfaceIndex 39 -NetworkCategory Private`
4. Ensure WinRM is running on both: `winrm quickconfig -force`
5. Ensure TrustedHosts on gaming PC: `Set-Item WSMan:\localhost\Client\TrustedHosts -Value "192.168.50.2" -Force`

## Services Set to Auto-Start
- sshd (OpenSSH Server) on Surface
- WinRM on both PCs

## Important Notes
- `saura` is a Microsoft account — cannot use for remoting
- `nestworker` is a local admin account — use this for all remote access
- Surface uses Windows Hello (face) for local login, but remoting needs password auth
