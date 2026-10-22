# QQ 邮箱 HTML 批量发送脚本

用 **Python 标准库** 通过 QQ 邮箱 SMTP，把指定 HTML 文件作为邮件正文，**逐个收件人单独发送**（每封邮件的 `To` 里只有一个人，互不可见）。带发送间隔、失败重试、日志与 dry-run。

> 无需 `pip install` 任何第三方依赖。

## 功能

- 读取 `config.ini` 中的发件邮箱与授权码
- 读取 HTML 文件作为邮件正文（自动生成纯文本兜底）
- 从 `recipients.txt` 读取收件人，一行一个，支持 `#` 注释
- **逐封独立连接发送**，单封失败不影响后续
- 发送间隔 + 失败重试，降低被频率限制的概率
- 认证失败时立即中止并给出明确中文提示
- `--dry-run` / `--check` / `--debug` 便于先校验再发信
- 发送结果写入 `send_log.txt`，失败名单写入 `failed_recipients.txt`

## 环境要求

- Python 3.9+（推荐 3.10+）
- 已开启 SMTP 的 QQ 邮箱，以及 **16 位授权码**（不是登录密码）

### 如何获取授权码

1. 登录 [QQ 邮箱网页版](https://mail.qq.com/)
2. **设置 → 账户**
3. 开启 **POP3/SMTP 服务**
4. 生成授权码，填入本地 `config.ini` 的 `auth_code`

## 快速开始

```bash
# 1. 克隆仓库
git clone https://github.com/taaaakoooo/qq-email-sender.git
cd qq-email-sender

# 2. 准备配置与收件人（不要提交这两个文件）
copy config.example.ini config.ini          # Windows
copy recipients.example.txt recipients.txt
# macOS / Linux:
# cp config.example.ini config.ini
# cp recipients.example.txt recipients.txt

# 3. 编辑 config.ini：填写 email、auth_code；按需改 html_file / subject
# 4. 编辑 recipients.txt：一行一个收件人邮箱
# 5. 编辑或替换 content.html：作为邮件正文

# 6. 先检查配置（不发送）
python send_mail.py --dry-run

# 7. 只验证能否登录 SMTP（不发送）
python send_mail.py --check

# 8. 正式发送（会再次确认）
python send_mail.py
```

## 常用命令

```bash
python send_mail.py                                   # 使用默认 config.ini + recipients.txt
python send_mail.py --dry-run                         # 只校验配置与名单
python send_mail.py --check                           # 只测试 SMTP 登录
python send_mail.py --debug                           # 尝试多种端口/认证方式并打印服务器原文
python send_mail.py --html notice.html --subject "通知"
python send_mail.py --recipients other.txt --yes      # 指定名单并跳过确认
python send_mail.py --recipients failed_recipients.txt  # 只重发失败的
```

## 配置说明（`config.ini`）

| 段落 | 字段 | 说明 |
|------|------|------|
| `[smtp]` | `host` / `port` / `use_ssl` | QQ 邮箱一般为 `smtp.qq.com:465` + SSL |
| `[sender]` | `email` | 发件 QQ 邮箱，须与授权码同一账号 |
| `[sender]` | `auth_code` | 16 位授权码，**不是** QQ 登录密码 |
| `[sender]` | `display_name` | 收件人看到的发件人名称，可留空 |
| `[mail]` | `html_file` | HTML 正文路径 |
| `[mail]` | `subject` | 主题；留空则取 HTML 的 `<title>`，再退回文件名 |
| `[mail]` | `delay_seconds` | 每封间隔（秒），建议 3～10 |
| `[mail]` | `retry_times` | 单封失败重试次数 |
| `[mail]` | `retry_wait_seconds` | 重试等待基数（第 n 次等待 n 倍） |

## 主题优先级

`--subject` 参数 > `config.ini` 的 `subject` > HTML 的 `<title>` > HTML 文件名

## 退出码

| 码 | 含义 |
|----|------|
| 0 | 全部成功（或 dry-run / check 通过） |
| 1 | 有部分收件人发送失败 |
| 2 | 配置或文件错误 |
| 3 | 认证失败 / 登录诊断失败 |

## 仓库里有什么 / 不会上传什么

**会纳入仓库：**

- `send_mail.py` — 主程序
- `content.html` — 示例邮件模板
- `config.example.ini` / `recipients.example.txt` — 配置与名单模板
- `README.md` / `.gitignore`

**本地私密文件（已写入 `.gitignore`，请勿提交）：**

- `config.ini`（含授权码）
- `recipients.txt`（真实收件人）
- `send_log.txt` / `failed_recipients.txt`
- 任何含学号、姓名、真实邮箱的名单导出
- 含真实 QQ / 招新内容的本地 HTML

## 注意事项

1. **授权码是敏感信息**，不要发到公开仓库、群聊或截图里。
2. QQ 邮箱对日发送量有限制，大批量发送请适当加大 `delay_seconds`。
3. HTML 里的外链图片在部分客户端需手动「显示图片」才会加载。
4. `From` 地址必须与登录账号一致，否则 QQ SMTP 可能返回 501/502。
5. 本工具仅供合法、授权的邮件通知使用；请遵守相关服务条款与反垃圾邮件规定。

## License

MIT
