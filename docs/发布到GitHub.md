# 发布到 GitHub：现在的状态 + 你剩下的 3 步

> 仓库名沿用 `leigod-duration-guard`，账号 `astronrain0113`。
> **如果你想起了别的仓库名，先告诉我**，我把仓库里的 3 处链接改掉再推（不然后台链接会 404）。

---

## 已经替你做完的

| 项目 | 状态 |
| --- | --- |
| Git 身份（全局 + 本仓库） | `astronrain0113` / `332066610+astronrain0113@users.noreply.github.com`（匿名邮箱，真实邮箱不会出现在公开提交里） |
| 本地仓库 | 已 `init` + 首次提交，**99 个文件 / 1.34 MB**（磁盘上 10.4GB 的构建产物全部被 `.gitignore` 排除） |
| 远程地址 | 已 `remote add origin https://github.com/astronrain0113/leigod-duration-guard.git`（只是记下地址，**还没推**） |
| 个人痕迹 | 已清零：源码/文档里没有任何 `C:\Users\<你>\...` 硬编码路径 |
| 仓库文件 | `README.md`（公开版）/ `LICENSE` / `requirements.txt` / `.gitignore` / `.gitattributes` / Issue 模板 / CI 工作流 |
| Issue 模板里的链接 | 已填成你的用户名 + 仓库名 |
| Release 附件 | 已打包好：项目根目录 `LeigodGuard-v1.0.0.zip`（103 MB，含两个 exe + OCR 模型，已排除 logs/config） |
| 发布说明正文 | 已写好：`docs/发布说明-v1.0.0.md`，发 Release 时直接复制 |

**唯一没做的**：凡是要用到你 GitHub 账号的操作（建仓库、推送、发 Release），都需要你的凭据，所以留给你。

---

## 第 1 步：建一个空仓库（30 秒）

打开 <https://github.com/new>：

| 字段 | 填什么 |
| --- | --- |
| Repository name | **`leigod-duration-guard`** ← 必须与上面一致 |
| Description | `关掉加速器之前，先替你把「计时」停下来（Windows）` |
| 可见性 | 先选 **Private**，确认没问题再切 Public（少一次"推错了再删") |
| Add a README / .gitignore / license | **全部不要勾** —— 本地已经有了 |

---

## 第 2 步：推送（1 分钟）

用我放在 `桌面\workbuddy\` 的 **`打开Git命令行.bat`** 双击打开一个已经配好 Git 的窗口，
然后敲这一条就够了（远程和身份都已经配好）：

```bash
git push -u origin main
```

会弹一次凭据输入：

- **用户名**：`astronrain0113`
- **密码**：**不是**你的 GitHub 登录密码，而是 **Personal Access Token**
  （GitHub → Settings → Developer settings → Personal access tokens → 生成一个勾 `repo` 的 token，复制粘贴到这里）

> 推送成功后刷新仓库页面，README 里的界面截图应该能直接显示出来。
>
> ⚠️ **如果报 `Failed to connect to github.com:443`** —— 那是网络问题，不是你的操作错。
> 先重试一次（这个域名的通断是间歇性的），仍不行就看
> **[网络问题-GitHub推不上去.md](网络问题-GitHub推不上去.md)**（内含走 SSH over 443 绕开 github.com 的做法）。

---

## 第 3 步：发 Release（2 分钟）

**这一步决定有没有人真的用得上** —— 绝大多数人不会为了一个"小工具"去装 Python 自己打包。

网页操作：仓库页右侧 **Releases → Draft a new release**

| 字段 | 填什么 |
| --- | --- |
| Choose a tag | 输入 `v1.0.0`（新 tag）→ 选 `Create new tag` |
| Release title | `v1.0.0` |
| Describe this release | 复制 `docs/发布说明-v1.0.0.md` 的正文（去掉第一行标题） |
| Attach binaries | 拖入项目根目录的 **`LeigodGuard-v1.0.0.zip`** |

或者用命令行（装了 GitHub CLI 的话）：

```bash
gh release create v1.0.0 LeigodGuard-v1.0.0.zip --title v1.0.0 --notes-file docs/发布说明-v1.0.0.md
```

> 以后每次发版：改 `main.py` 里的 `__build__` → `packaging\build.bat` → 重新打 zip → 推新 tag。
> 打了 `v*` tag 之后，仓库里的 CI（`.github/workflows/build.yml`）也会自动打包并附带发布。

---

## 切到 Public 之前，再确认一次

```bash
git grep -n -i "astronrain"        # 应只在 LICENSE / 发布说明等"你就是作者"的地方出现
git ls-files | grep -c "\.log$"    # 应为 0
```

仓库里**不该有**的东西（都已排除，但值得知道为什么）：

- `_pkg/`（历史打包输出，9.5GB）与 `dist/`（922MB）—— GitHub 单文件硬上限 100MB，
  里面 `cv2.pyd` 一个就 82MB，提交会被直接拒收
- `logs/`、`real_state/`、`real_inspector/`、`tests/out/` —— 这些是**本机**跑出来的
  日志与截图，含窗口标题、句柄、甚至整屏画面，等于把桌面公开
- `config/config.json` —— 使用者自己的配置

---

## 之后想改代码

```bash
git add -A
git commit -m "说明这次改了什么"
git push
```

改了 `__build__` 记得同步更新 `使用教程.md` 里提到的版本号，并重新打包发 Release。
