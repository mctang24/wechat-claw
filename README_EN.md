# WeChat Claw

> Keep building, even when your laptop is out of reach.

Continue active Codex CLI sessions on your Mac directly from WeChat.

[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
![Codex CLI](https://img.shields.io/badge/Codex-CLI-111111)

[中文](README.md)

<p align="center">
  <img src="assets/wechat.png" alt="Viewing active Codex CLI sessions from WeChat" width="300">
  &nbsp;&nbsp;
  <img src="assets/wechat-result.png" alt="Sending a message to Codex CLI and receiving its reply from WeChat" width="300">
</p>

## Usage

Requires macOS, Python 3.11+, and an authenticated Codex CLI.

```bash
curl -fsSL https://raw.githubusercontent.com/mctang24/wechat-claw/refs/heads/main/install.sh | bash
```

## Commands

| Command | Behavior |
| --- | --- |
| `/sessions` | List active sessions and their numbers |
| `/focus <number>` | Select where subsequent messages go |
| `/unfocus` | Clear the current focus |
| `/send <number> <message>` | Send once to a specific session |
| `/approve [code]` | Approve a pending request |
| `/help` | Show help and the current focus |

## License

[MIT](LICENSE)
