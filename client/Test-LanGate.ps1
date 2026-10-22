<#
  内网 B 站"知识性内容"门禁 —— Windows 客户端自检脚本
  用法：把本文件保存为 Test-LanGate.ps1，右键"使用 PowerShell 运行"（或 powershell -ExecutionPolicy Bypass -File Test-LanGate.ps1）
  预期：5 项全部 PASS
#>
$ErrorActionPreference = "Continue"
$ProxyHost= "192.168.200.174"   # <<< 改成你的门禁服务器 IP
$Proxy    = "http://${ProxyHost}:8080"
$PacUrl   = "$Proxy/proxy.pac"
$NonKnow  = "BV1bwnF6eEYV"   # 今天吃红糖糍粑（应被拦截 403）
$Know     = "BV1Xdnc6jEbL"   # 中文已死：为什么我们越来越不敢好好说话？（应放行 200）
$script:pass = 0; $script:fail = 0

function Ok($m) { Write-Host ("  [PASS] " + $m) -ForegroundColor Green; $script:pass++ }
function No($m, $hint) {
    Write-Host ("  [FAIL] " + $m) -ForegroundColor Red
    if ($hint) { Write-Host ("         -> " + $hint) -ForegroundColor Yellow }
    $script:fail++
}
function Get-Status($bv) {
    try {
        $r = Invoke-WebRequest -Uri "https://www.bilibili.com/video/$bv/" -Proxy $Proxy -UseBasicParsing -TimeoutSec 25
        return [int]$r.StatusCode
    } catch {
        if ($_.Exception.Response) { return [int]$_.Exception.Response.StatusCode }
        return 0
    }
}

Write-Host ""
Write-Host "=== 内网 B 站知识门禁 自检 ===" -ForegroundColor Cyan
Write-Host "代理服务器: $Proxy" -ForegroundColor DarkGray
Write-Host ""

# 1) 代理端口可达
try {
    $t = Test-NetConnection -ComputerName $ProxyHost -Port 8080 -WarningAction SilentlyContinue
    if ($t.TcpTestSucceeded) { Ok "代理端口 $ProxyHost`:8080 可达" }
    else { No "代理端口 $ProxyHost`:8080 不可达" "确认在同一内网 / VPN 已连上 / 174 上的 mitmproxy 在跑" }
} catch { No "代理端口检测异常" $_.Exception.Message }

# 2) PAC 可下载
try {
    $r = Invoke-WebRequest -Uri $PacUrl -UseBasicParsing -TimeoutSec 15
    if ($r.Content -match [regex]::Escape("PROXY $ProxyHost`:8080")) { Ok "PAC 可下载且指向正确" }
    else { No "PAC 内容异常" $r.Content.Substring(0, [Math]::Min(120, $r.Content.Length)) }
} catch { No "PAC 下载失败" $_.Exception.Message }

# 3) 根证书已被系统信任
try {
    $ca = Get-ChildItem Cert:\LocalMachine\Root -ErrorAction Stop | Where-Object { $_.Subject -like "*mitmproxy*" }
    if ($ca) { Ok "MITM 根证书已在受信任根证书库（指纹 $($ca[0].Fingerprint.Substring(0,29))...）" }
    else { No "系统里找不到 mitmproxy 根证书" "先安装 mitmproxy-ca.cer：双击安装 -> 存储位置选『受信任的根证书颁发机构』" }
} catch { No "读取证书库失败（需管理员权限运行）" "请以管理员身份运行 PowerShell" }

# 4) 经代理访问 HTTPS
try {
    $r = Invoke-WebRequest -Uri "https://www.bilibili.com/" -Proxy $Proxy -UseBasicParsing -TimeoutSec 25
    if ([int]$r.StatusCode -eq 200) { Ok "经代理访问 B 站正常 (200)" }
    else { No "经代理访问异常 ($($r.StatusCode))" }
} catch {
    $m = $_.Exception.Message
    if ($m -match "信任|证书|SSL|secure channel|远程证书|TRUST") {
        No "HTTPS 握手被拒：证书不受信任" "根证书没装好/没装到当前用户；Chrome 需完全退出重开"
    } else {
        No "经代理访问失败" $m
    }
}

# 5) 门禁是否真的生效
$s1 = Get-Status $NonKnow
if ($s1 -eq 403) { Ok "非知识视频已被拦截 (403)" }
else { No "非知识视频未拦截（状态 $s1）" "浏览器代理没配好，或走的是『使用设置脚本』但 PAC 未生效" }

$s2 = Get-Status $Know
if ($s2 -eq 200) { Ok "知识视频正常放行 (200)" }
else { No "知识视频被误拦（状态 $s2）" "规则白名单过严，可反馈给管理员调整词表" }

Write-Host ""
Write-Host ("结果： PASS=" + $script:pass + "  FAIL=" + $script:fail) -ForegroundColor $(if ($script:fail) { "Red" } else { "Green" })
if ($script:fail -gt 0) { exit 1 } else { exit 0 }
