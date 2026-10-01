# 推不上去？`Failed to connect to github.com:443`

> 症状：
> ```
> fatal: unable to access 'https://github.com/<user>/<repo>.git/':
> Failed to connect to github.com:443 after 21111 ms: Could not connect to server
> ```

**先给结论：这通常不是你的操作错误，也不是仓库配置问题 —— 是国内网络到 `github.com` 的连接不稳定。**

（这份文档来自一次真实排查：同一台机器上，同一命令几分钟前全部超时、几分钟后 4/4 成功。
所以**第一步永远是先重试**。）

---

## 第一步：判断现在通不通（10 秒）

```bash
git ls-remote origin
```

| 看到什么 | 含义 | 下一步 |
| --- | --- | --- |
| `fatal: could not read Username for 'https://github.com'` | **通了**（只是要凭据） | 直接 `git push -u origin main` |
| `Failed to connect ... Could not connect to server` | 当前不通 | 看下面的方案 |

> 凭据弹窗：用户名是 GitHub 用户名；密码**不是**登录密码，而是
> **Personal Access Token**（Settings → Developer settings → Personal access tokens，勾 `repo`）。

---

## 判断是哪一层不通

```bash
# ① 域名解析到哪个 IP
nslookup github.com

# ② 换个域名试试（这些通常比 github.com 稳）
curl -sS -m 10 -o /dev/null -w "api    %{http_code}\n" https://api.github.com
curl -sS -m 10 -o /dev/null -w "ssh443 %{http_code}\n" https://ssh.github.com
```

如果 `api.github.com` 通、只有 `github.com` 不通，那就是**这个域名被单独干扰**——
下面三个方案任意一个都能解决。

---

## 方案 1（最推荐）：SSH over 443 —— 绕开 `github.com`

`ssh.github.com:443` 走的是 SSH 协议、443 端口，通常比 `github.com:443` 稳定得多。
它**完全不经过** `github.com` 这个域名。

```bash
# 1) 生成密钥（已有就跳过；一路上回车即可，口令留空方便命令行推送）
ssh-keygen -t ed25519 -C "你的GitHub邮箱或用户名"

# 2) 打印公钥，复制整行
cat ~/.ssh/id_ed25519.pub

# 3) 贴到 GitHub：Settings → SSH and GPG keys → New SSH key
#    （https://github.com/settings/ssh/new）

# 4) 验证通道（看到 "Hi <用户名>!" 就成功）
ssh -T -p 443 git@ssh.github.com

# 5) 换成 443 的 SSH 远程并推送
git remote add gh443 ssh://git@ssh.github.com:443/<用户名>/<仓库名>.git
git push -u gh443 main
```

`-u` 会记住上游，之后直接 `git push` 即可。想切回 HTTPS：`git push -u origin main`。

> ⚠️ 私钥（`~/.ssh/id_ed25519`）**不要**发给任何人；公钥可以随便公开。

---

## 方案 2：改 hosts，把 `github.com` 指向一个可达 IP（需管理员）

先用 `curl --resolve` 挑一个**当前可用**的 IP：

```bash
# 逐个试，看哪个返回 200
curl -sS -m 10 --resolve github.com:443:20.27.177.113 -o /dev/null -w "20.27.177.113  %{http_code}\n" https://github.com
curl -sS -m 10 --resolve github.com:443:140.82.113.4  -o /dev/null -w "140.82.113.4   %{http_code}\n" https://github.com
```

把返回 200 的那个写进 hosts（`C:\Windows\System32\drivers\etc\hosts`，用**管理员**记事本打开）：

```
20.27.177.113 github.com
```

- 命令行版（管理员 PowerShell）：
  ```powershell
  Add-Content "$env:windir\System32\drivers\etc\hosts" -Value "`n20.27.177.113 github.com" -Encoding ASCII
  ```
- **GitHub 的边缘 IP 会变**。哪天又连不上，**先删掉这一行**再试 —— 留着过期 IP 只会让问题更难查。
  这也是它不如方案 1 稳的原因。

---

## 方案 3：用你自己的代理

把代理软件的「系统代理 / 全局 / TUN」打开，然后让 git 也走它：

```bash
git config --global http.proxy  http://127.0.0.1:端口
git config --global https.proxy http://127.0.0.1:端口
```

常见端口：Clash `7890`、Clash Verge `7897`、v2rayN `10809`。
取消：`git config --global --unset http.proxy`。

> 注意：**git 不会读 Windows 的"Internet 选项"里的代理**（它用自带 libcurl），
> 所以只在浏览器里设代理是不够的，必须像上面这样显式告诉 git，或者把代理开成 TUN/全局。

---

## 相关：大文件别用命令行传

Release 附件（几十 MB 以上）建议**在浏览器里拖进 Release 页面**：浏览器自带重试，
而这类时通时断的线路传大文件很容易中途断掉。
