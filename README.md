# Career Assistant · 智能求职助手

单用户、本机浏览器运行：搜索或粘贴岗位 JD → 分析适配 → 按需准备材料 → 编辑导出 → 自行投递并记录申请。Flask、SQLite、单后台 Worker；不自动填写、上传或提交真实招聘申请。

## 安装

Python 3.10+。下载本仓库并进入项目目录：

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
python -m playwright install chromium
```

Windows 激活使用 `.venv\Scripts\activate`。搬动目录后请重新创建虚拟环境；旧环境的激活脚本可能包含绝对路径。

## 配置与启动

可复制 `config.example.yaml` 为 `config.yaml` 配置来源、浏览器和定时搜索。网页偏好独立保存在数据库。AI、SMTP 凭据按 `.env.example` 在启动终端设置；示例文件不会自动加载。未配置 AI 时分析给出错误，搜索及申请管理仍可用；未配置 SMTP 时仅生成未发送预览。

```sh
export CAREER_HOME="$PWD"
career-assistant migrate
career-assistant web --port 5000
```

Windows PowerShell 设置目录使用 `$env:CAREER_HOME = (Get-Location).Path`。

打开 [本机网页](http://127.0.0.1:5000)。源码检出也可使用 `python main.py migrate`、`python main.py web`；均调用同一个入口。`python -m career_assistant` 等价。`web --schedule` 启用按配置定时创建搜索任务；每日邮件由网页设置控制。`worker` 供单独运行后台使用，不要与 web 对同一个库同时启动。`login jobsdb`（或 indeed、linkedin）显式打开已有浏览器档案供用户手动登录，不自动绕过限制。`doctor` 是显式维护诊断，会检查外部网络但不调用 AI；不能将其作为离线测试。

## 数据与恢复

`CAREER_HOME` 决定配置和数据根目录；源码运行默认项目目录，普通安装默认当前目录，建议总是显式设置。默认数据库 `data/radar.db`、简历 `data/course_uploads/`、浏览器档案 `data/browser/`、会话密钥 `data/course.secret`。这些名字及 SQL 的 `course_*` 表保留兼容，不是旧程序分层。已有 `RADAR_DATA_DIR`、`RADAR_DB_PATH`、`RADAR_CONFIG_PATH`、`RADAR_AI_*` 仍有效，分别覆盖数据目录、数据库、配置文件及 AI 环境设置。配置加载统一在 config.py。

升级前停止使用该库的 web/worker，备份整个 data/ 及自定义外部上传目录；配置与浏览器档案包含隐私，应只保存在个人受限位置，不能放入代码归档或 Git。停止后完整复制目录可保留 SQLite WAL；运行中备份数据库必须使用 SQLite backup API，不能只复制主 db 文件。`migrate` 在需要升级时自动生成同目录 `.bak` 并使用进程锁防止并行升级。当前版本重复迁移是幂等操作。

恢复时先停止进程，将当前 data/ 改名保留，再完整恢复原 data/；也可将所需数据库 `.bak` 复制为新的恢复库并通过 `RADAR_DB_PATH` 指向它验证，避免覆盖原库和混用旧 WAL。恢复配置和上传文件后启动对应代码版本。不要只恢复空库。旧填写任务保留历史但永不恢复执行。

历史简历绝对路径可兼容原 hk-job-radar 项目目录名；自定义外部文件路径仍需保持可访问。

## 功能与来源

- [产品需求](REQUIREMENTS.md)
- [上游来源与版权](docs/UPSTREAM.md) · [MIT 许可证](LICENSE)

本仓库发布当前运行程序。测试、开发依赖及历史验收资料保留在本地，不是安装运行前提。AI 和招聘来源依赖实际服务配置及访问条件，不保证跨越登录、验证码或访问限制。
