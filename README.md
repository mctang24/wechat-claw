# WeChat Claw

> AI 时代，手机也是生产力。

WeChat Claw 把微信变成 Codex CLI 的移动入口。离开电脑时，也能从手机继续推进本机正在运行的任务。

[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
![Codex CLI](https://img.shields.io/badge/Codex-CLI-111111)

[English](README_EN.md)

<p align="center">
  <img src="assets/wechat.png" alt="通过微信查看活跃 Codex CLI 会话" width="300">
  &nbsp;&nbsp;
  <img src="assets/wechat-result.png" alt="通过微信向 Codex CLI 发送消息并接收回复" width="300">
</p>

## 使用

需要 macOS、Python 3.11＋和已登录的 Codex CLI。

```bash
curl -fsSL https://raw.githubusercontent.com/mctang24/wechat-claw/refs/heads/main/install.sh | bash
```

## 命令

| 命令 | 作用 |
| --- | --- |
| `/sessions` | 查看活跃会话和对应编号 |
| `/focus <编号>` | 选择后续消息发送到哪个会话 |
| `/unfocus` | 解除当前选择 |
| `/send <编号> <消息>` | 向指定会话发送一次消息 |
| `/approve [审批码]` | 批准待审批请求 |
| `/help` | 查看帮助和当前 focus |

## License

[MIT](LICENSE)
