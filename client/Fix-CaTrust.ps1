<#
  Fix-CaTrust.ps1  ——  修复 Windows 11 "您的连接不是专用连接"（mitmproxy CA 未受信任）
  用法（管理员 PowerShell）：
      iwr http://<你的服务器IP>:8081/Fix-CaTrust.ps1 -OutFile $env:TEMP\Fix-CaTrust.ps1
      powershell -NoProfile -ExecutionPolicy Bypass -File $env:TEMP\Fix-CaTrust.ps1
  或在浏览器里打开 http://<你的服务器IP>:8081/Fix-CaTrust.ps1 另存后运行
#>

$ErrorActionPreference = 'Continue'
$ProgressPreference    = 'SilentlyContinue'
$GateHost = '192.168.200.174'                        # <<< 改成你的门禁服务器 IP
$Server   = "http://${GateHost}:8081"
$Proxy    = "${GateHost}:8080" 
$CAUrl    = "$Server/mitmproxy-ca.cer"
$ExpectFP = '9DC6156055EA2B8CEE27D47EEE439E0C321CE8A1'

function W($m) { Write-Host $m }
function Step($m) { Write-Host "`n=== $m ===" -ForegroundColor Cyan }

# ---------- 0. 提权 ----------
$id = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not ([Security.Principal.WindowsPrincipal]$id).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)) {
    W "当前不是管理员，将尝试自动提权重启本脚本..."
    Start-Process powershell -Verb RunAs -ArgumentList @(
        '-NoProfile','-ExecutionPolicy','Bypass','-File',"`"$PSCommandPath`"")
    exit
}

# ---------- 1. 时钟（证书有效期）----------
Step '1/6 检查系统时间（时间不对也会报"连接不是专用连接"）'
W ("本机时间: " + (Get-Date))
try { W ("时区    : " + (Get-TimeZone).Id) } catch {}
try {
    $st = w32tm /query /status 2>&1
    W ($st | Out-String).Trim()
    if ($st -match '源\s*:\s*Local CMOS Clock|源\s*:\s*本地 CMOS 时钟') {
        W '[!] 时钟未同步，先执行: w32tm /resync'
        w32tm /resync 2>&1 | Out-String | ForEach-Object { W $_ }
    }
} catch { W "[!] 无法查询时间服务状态: $_" }

# ---------- 2. 下载 CA ----------
Step '2/6 下载并安装 CA 证书（受信任的根证书颁发机构）'
$tmpCer = Join-Path $env:TEMP 'mitmproxy-ca.cer'
try {
    Invoke-WebRequest -Uri $CAUrl -OutFile $tmpCer -UseBasicParsing
    W "已下载: $CAUrl -> $tmpCer"
} catch {
    W "[!] 下载失败: $($_.Exception.Message)"
    W "    若本机无 174 直连，请手动把 mitmproxy-ca.cer 放到脚本同目录后重试"
    if (Test-Path (Join-Path $PSScriptRoot 'mitmproxy-ca.cer')) {
        Copy-Item (Join-Path $PSScriptRoot 'mitmproxy-ca.cer') $tmpCer -Force
        W "    已改用同目录文件: $tmpCer"
    } else { exit 1 }
}

# 校验指纹
$cert = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2 $tmpCer
W ("证书主题: " + $cert.Subject)
W ("有效期  : " + $cert.NotBefore.ToString('yyyy-MM-dd HH:mm:ss') + " ~ " + $cert.NotAfter.ToString('yyyy-MM-dd HH:mm:ss'))
$fp = ($cert.GetCertHashString('SHA1') -replace ' ', '')
W ("SHA1    : " + $fp)
if ($fp -ne $ExpectFP) {
    W "[!] 指纹与服务器 CA 不一致！请确认下载的是 174 上的那份（期望 $ExpectFP）"
}

foreach ($store in @('Root','CA')) {
    foreach ($scope in @('LocalMachine','CurrentUser')) {
        $before = @(Get-ChildItem "Cert:\$scope\$store" -ErrorAction SilentlyContinue |
                    Where-Object { $_.Thumbprint -eq $cert.Thumbprint }).Count
        & certutil.exe -f -addstore $store $tmpCer | Out-Null
        $after = @(Get-ChildItem "Cert:\$scope\$store" -ErrorAction SilentlyContinue |
                   Where-Object { $_.Thumbprint -eq $cert.Thumbprint }).Count
        $ok = if ($after -gt 0) { 'OK' } else { 'FAIL' }
        W ("[{0}] {1}\{2}  已存在={3} 安装后={4}" -f $ok, $scope, $store, $before, $after)
    }
}

# ---------- 3. 代理设置 ----------
Step '3/6 检查系统代理'
$ik = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings'
$p  = Get-ItemProperty $ik -ErrorAction SilentlyContinue
W ("ProxyEnable   : " + $p.ProxyEnable)
W ("ProxyServer   : " + $p.ProxyServer)
W ("AutoConfigURL : " + $p.AutoConfigURL)
if ($p.ProxyEnable -eq 1 -and $p.ProxyServer -notmatch [regex]::Escape($Proxy)) {
    W "[!] 当前系统代理不是 $Proxy —— 如果你就是要走 174 门禁，请手工改或运行:"
    W "    Set-ItemProperty '$ik' ProxyServer '$Proxy'"
}

# ---------- 4. 杀浏览器 ----------
Step '4/6 关闭浏览器（让浏览器重新读取信任库）'
foreach ($b in @('msedge','chrome','iexplore','firefox')) {
    $ps = Get-Process $b -ErrorAction SilentlyContinue
    if ($ps) { W "关闭 $b ($($ps.Count) 个)"; $ps | Stop-Process -Force -ErrorAction SilentlyContinue }
}
Start-Sleep -Seconds 2

# ---------- 5. 连通性测试 ----------
Step '5/6 测试（直连 + 走代理）'
foreach ($u in @('https://cn.bing.com','https://www.bilibili.com','https://www.gov.cn')) {
    try {
        $r = Invoke-WebRequest -Uri $u -UseBasicParsing -TimeoutSec 20 `
              -Proxy "http://$Proxy" -MaximumRedirection 3
        W ("[OK ] {0,-28} HTTP {1}" -f $u, $r.StatusCode)
    } catch {
        $m = $_.Exception.Message
        if ($m -match 'certificate|证书|SSL|TLS') {
            W ("[ERR] {0,-28} 证书错误: {1}" -f $u, $m)
        } else {
            W ("[ERR] {0,-28} {1}" -f $u, $m)
        }
    }
}

# ---------- 6. 总结 ----------
Step '6/6 完成'
W "如果浏览器仍报'连接不是专用连接'："
W "  1) 浏览器必须完全退出再打开（上面已杀进程，请重新打开浏览器）"
W "  2) 检查 证书颁发机构 -> 受信任的根证书颁发机构 里是否有 CN=mitmproxy, O=mitmproxy"
W "  3) 检查系统时间是否正确（时间偏差也会导致同样提示）"
W "  4) Edge 策略: edge://policy  看 CertificateRevocation / SSL 策略是否拦截"
Write-Host ''
