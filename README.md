# 饮食与体重记录

一个可以在电脑和手机上使用的轻量网页应用，用来记录每日饮食、估算卡路里、保存体重，并自动绘制体重变化曲线。现在同时支持：

- 本地模式：本机 `SQLite`
- Render 云端模式：`Render Web Service + Render Postgres + 登录保护`

## 现在有哪些功能

- 记录每天吃了什么、吃了多少
- 内置常见食物热量库，可搜索后自动计算卡路里
- 支持手动输入热量并保存为自定义常用食物
- 记录每日体重，自动覆盖同一天的旧记录
- 自动生成近 `14 / 30 / 90` 天体重曲线
- 展示当日总热量、分餐热量、近 7 天平均热量和体重变化
- 支持登录/退出，适合公开部署到 Render
- 自动识别 `DATABASE_URL`，线上切换为 Postgres 持久化

## 本地启动

直接运行：

```bash
python3 server.py --host 0.0.0.0 --port 8766 --open-browser
```

如果你只想在当前电脑上打开，也可以：

```bash
python3 server.py
```

如果你想提前生成密码哈希：

```bash
python3 server.py --print-password-hash '你的密码'
```

生成结果可以放到 `APP_PASSWORD_HASH` 环境变量里。

## 本地使用手机访问

1. 让手机和电脑连接同一个 Wi-Fi。
2. 启动服务时使用 `--host 0.0.0.0`。
3. 终端会显示一个类似 `http://192.168.x.x:8766` 的地址。
4. 在手机浏览器里打开这个地址，就能使用同一份数据。

## 部署到 Render

### 1. 推送到 Git 仓库

把整个 `diet-weight-tracker` 目录推到 GitHub、GitLab 或 Bitbucket。

### 2. 在 Render 创建 Blueprint

这个项目已经包含根目录蓝图文件：

- `render.yaml`

在 Render 里选择：

1. `New +`
2. `Blueprint`
3. 连接你的代码仓库
4. 选择这个项目目录

Render 会自动读取：

- 一个 Python Web Service
- 一个 Postgres 数据库

### 3. 首次部署时填写密码

`render.yaml` 里把 `APP_PASSWORD` 标成了 `sync: false`，首次创建 Blueprint 时 Render 会提示你填写。

默认账号是：

```text
admin
```

你也可以在 Render 控制台里改成：

- `APP_PASSWORD`
- 或者更安全的 `APP_PASSWORD_HASH`

如果使用 `APP_PASSWORD_HASH`，记得删除 `APP_PASSWORD`，避免保留明文密码。

### 4. 访问线上地址

部署成功后，Render 会给你一个 `onrender.com` 地址。打开后先登录，再开始记录饮食和体重。

## Render 环境变量说明

- `DATABASE_URL`：由 Render Postgres 自动注入
- `APP_USERNAME`：登录账号，默认 `admin`
- `APP_PASSWORD`：登录密码，适合最省事的配置
- `APP_PASSWORD_HASH`：密码哈希，适合更安全的配置
- `SESSION_SECRET`：会话签名密钥，蓝图里自动生成
- `AUTH_REQUIRED`：是否强制登录，Render 默认为 `true`
- `FORCE_SECURE_COOKIE`：线上使用安全 Cookie，默认为 `true`

## 关于 Render 免费版

`render.yaml` 目前默认用了 `free` 计划，方便你先试跑。

如果你想长期稳定使用，建议改成付费计划：

- Web Service：改成 `starter`，避免空闲休眠
- Postgres：改成 `basic-256mb` 或更高，避免免费库到期

## 数据文件

- 本地数据库：`/Users/linsenli/Documents/diet-weight-tracker/data/app.db`
- 食物种子数据：`/Users/linsenli/Documents/diet-weight-tracker/foods_seed.json`

## 目录结构

- `server.py`：后端 API、登录会话、数据库切换
- `static/index.html`：页面结构
- `static/style.css`：界面样式
- `static/app.js`：前端逻辑与登录交互
- `foods_seed.json`：内置热量库
- `requirements.txt`：Render 部署依赖
- `render.yaml`：Render Blueprint 配置
- `.env.example`：环境变量示例
- `.python-version`：固定 Python 版本
