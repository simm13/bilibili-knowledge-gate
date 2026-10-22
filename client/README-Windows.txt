内网 B 站"知识性内容"门禁 —— Windows 机器接入说明
=================================================

下载本页文件：
  1) mitmproxy-ca.cer      —— MITM 根证书（双击安装）
  2) Test-LanGate.ps1      —— 一键自检脚本（5 项全 PASS 即成功）
  3) Fix-CaTrust.ps1       —— 一键修证书（报"连接不是专用连接"时用）

把下面所有 <你的服务器IP> 替换成门禁服务器的 IP。

------------------------------------------------------
第 1 步：安装根证书（必须，否则所有 HTTPS 会报"不安全"）
------------------------------------------------------
1) 双击 mitmproxy-ca.cer -> "安装证书"
2) 存储位置必须选：**本地计算机** -> **受信任的根证书颁发机构**
3) 装完后 Chrome/Edge 会自动信任；Firefox 建议再在
   设置 -> 隐私与安全 -> 证书 -> 查看证书 -> 导入 -> 选 mitmproxy-ca.cer
   （勾选"为所有用户"）

核对指纹（应一致）：
  SHA1   = 9D:C6:15:60:55:EA:2B:8C:EE:27:D4:7E:EE:43:E9:0C:32:1C:E8:A1
  主体   = CN=mitmproxy, O=mitmproxy
  （各人自建 CA 指纹不同，用 openssl x509 -in mitmproxy-ca.cer -noout -fingerprint 自己看）

------------------------------------------------------
第 2 步：设置代理（二选一）
------------------------------------------------------
A) 自动脚本（推荐，改动最小）
   设置 -> 网络和 Internet -> 代理 -> "使用设置脚本"
   地址：http://<你的服务器IP>:8080/proxy.pac

B) 手动
   设置 -> 网络和 Internet -> 代理 -> "使用代理服务器"
   地址：<你的服务器IP>     端口：8080
   并勾选"对所有协议都使用此代理服务器"

注：内网网段不走代理（默认 192.168.0.0/16，可在 gate.env 的 KB_LAN_NET 改），其余外网流量全部经门禁过滤。

------------------------------------------------------
第 3 步：自检
------------------------------------------------------
右键 Test-LanGate.ps1 -> 使用 PowerShell 运行
（或：powershell -ExecutionPolicy Bypass -File Test-LanGate.ps1）
预期 5 项全部 [PASS]：
  代理端口可达 / PAC 可下载 / 根证书已信任 / 经代理访问正常 / 门禁生效

------------------------------------------------------
常见问题
------------------------------------------------------
* 页面提示"您的连接不是私密连接" -> 根证书没装或装错位置；装完要完全退出浏览器再打开
  快速修复（管理员 PowerShell）：
     iwr http://<你的服务器IP>:8081/Fix-CaTrust.ps1 -OutFile $env:TEMP\Fix-CaTrust.ps1
     powershell -NoProfile -ExecutionPolicy Bypass -File $env:TEMP\Fix-CaTrust.ps1
* 首页正常但视频没被拦 -> 代理没走通（PAC 地址写错/未保存），跑自检脚本看哪一项 FAIL
* 装了证书后所有 HTTPS 都报错 -> 证书装到了"个人"存储却想给所有用户用，重新安装并勾"为所有用户"
* 某些老旧软件报错 -> 代理只建议给浏览器用；老软件（IE10 以下/curl 7.x）不支持 TLS1.2
* 管理员可用 GPO 批量下发：证书走"受信任的根证书颁发机构"计算机策略；
  Firefox 把 policies.json 放到 C:\Program Files\Mozilla Firefox\distribution\

=========================================================
出现「您的连接不是专用连接」时怎么修（服务端可查）
---------------------------------------------------------
原因：Windows 没信任门禁服务器的 mitmproxy CA。
服务端自查：
  journalctl -u mitmfilter.service | grep "Client TLS handshake failed"
  会打出是哪台机器：sslv3 alert certificate unknown = 不信任 CA
方法 A（推荐，一键）：管理员 PowerShell 执行
      iwr http://<你的服务器IP>:8081/Fix-CaTrust.ps1 -OutFile $env:TEMP\Fix-CaTrust.ps1
      powershell -NoProfile -ExecutionPolicy Bypass -File $env:TEMP\Fix-CaTrust.ps1
      （脚本会自动提权、装证书、查时钟、关浏览器、实测连通性）
方法 B（手工）：打开 http://<你的服务器IP>:8081/mitmproxy-ca.cer
      ->「安装证书」-> 存储位置选「本地计算机」
      ->「受信任的根证书颁发机构」-> 完成
      -> 完全退出浏览器再打开（只关标签页不生效）
注意：系统时间不对也会出现同样提示，脚本里已自动检查/同步
