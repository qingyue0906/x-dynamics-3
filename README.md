# x-dynamics-3

用 `XClientTransaction` 和 X 网页版 GraphQL 接口，增量抓取指定 X/Twitter 用户的推文与媒体，下载原图/视频，并通过本地 WebUI 浏览和管理。

整体结构参考 `bili-dynamics-3`：SQLite 主库、后台任务队列、WebUI 为唯一日常入口、旧数据迁移、失败重试、本地头像/缩略图、深色模式和灯箱画廊。

## 功能

- 用户列表存在 SQLite 中，可启用/停用、批量选择、增删用户
- 三种同步模式，按用户选择：
  - `media`：仅媒体（`UserMedia`，默认，最快）
  - `timeline`：全部推文（`UserTweets`，含文字/转推/引用，可筛选转推）
  - `replies`：全部推文 + 回复（`UserTweetsAndReplies`）
- 增量/全量同步，支持限制页数、限制媒体数、只抓元数据
- 图片、视频、GIF/动图；原图优先，视频选最高码率 MP4
- 转推、引用、回复、长文 `note_tweet`、无媒体推文
- 失败/缺失/待下载媒体重试；404/410 标记失效，401/403 标记受限
- 增量同步带连续空页保护和 cursor 循环检测，不会在 X 的空尾页上无限翻页
- 迁移媒体使用 URL 级 canonical key，和后续同步共用同一身份，不会重复下载
- `dedupe-media` 可以合并历史遗留的重复媒体记录，并可选删除多余文件
- 指定推文 URL/ID：预览后下载；可选加入用户列表
- 本地头像：160×160 WebP；可选横幅；X 外链不参与页面渲染
- Cookie、代理、同步参数、头像、媒体、任务日志全部在 `data/` 下
- 后台任务队列：进度、日志、取消、历史；同一时间只跑一个重任务
- SQLite 主库 + WAL + 每页事务；数据库升级前自动备份
- 仿 X/动态页的画廊：侧栏、卡片、媒体网格、灯箱、视频、深色模式、搜索
- 动态/媒体两种视图；最新/最早排序；无限滚动或分页
- 按需生成 WebP 缩略图，列表页不直接加载大原图
- 归档语义：只增不删；无媒体推文和失效媒体仍可查看
- 旧 `twitter_download` 数据迁移：主用户媒体/文本、标签、评论区、profile 头像/横幅
- GraphQL queryId 运行时从 X 前端脚本自动刷新；XClientTransaction 失效时自动重建

## 安装

需要 Python 3.11+（当前环境 Python 3.14 可运行）。

```bat
cd /d <项目目录>
python -m venv .venv
.venv\Scripts\python -m pip install -U pip
.venv\Scripts\pip install -r requirements.txt
```

如果直连 PyPI 失败：

```bat
.venv\Scripts\pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

可选：如果 `httpx` 被 Cloudflare/403 拦截，在设置里把 API 传输切到 `curl_cffi`，并安装：

```bat
.venv\Scripts\pip install curl-cffi
```

## 启动

```bat
cd /d <项目目录>
.venv\Scripts\python -m xdyn
```

不带子命令时默认启动 WebUI，并自动打开浏览器。默认地址：

```text
http://127.0.0.1:5001/
```

显式启动：

```bat
.venv\Scripts\python -m xdyn serve --port 5001
```

不想显示控制台时：

```bat
.venv\Scripts\pythonw.exe -m xdyn
```

服务默认只监听 `127.0.0.1`，使用 waitress，不会暴露到局域网。

## 首次使用

1. 启动 WebUI，进入 **设置**。
2. 粘贴 X Cookie，至少包含 `auth_token` 和 `ct0`；可以直接粘贴浏览器 Cookie 字符串或 JSON。
3. 点“保存并验证”，或先点“测试连接”。
4. 如果直连 x.com 失败，在“同步参数 → 代理”里填写 `http://127.0.0.1:7890` 或 `socks5://...`，保存后重启/重新测试。
5. 进入 **用户**，添加 `@handle`、`x.com/handle` 或数字 ID。
6. 选择每个用户的同步模式，点“同步全部启用用户”或单个“同步”。
7. 在 **任务** 页查看进度、日志和取消。
8. 在 **指定推文** 页可以单独预览/下载一条推文。

### 新账号/新环境自检

```bat
.venv\Scripts\python -m xdyn doctor
```

`doctor` 会检查 Cookie、XClientTransaction 初始化、queryId 刷新、一次 GraphQL 用户请求。它不会输出 Cookie 内容。

## 数据目录

```text
data/
  x.db                        SQLite 主库
  config/
    settings.json             同步/Web 参数（无敏感信息）
    cookies.json              X Cookie（不进 Git、不被 Web 暴露）
    gql_ops.json              运行时刷新的 GraphQL queryId 缓存
  images/                     用户媒体目录（新下载 + 迁移复制）
  avatars/                    本地头像 WebP
  banners/                    本地横幅 WebP
  thumbnails/                 WebP 缩略图缓存
  exports/                    每用户 JSON 备份
  imports/                    旧数据迁移报告
  logs/
    jobs/                     每个后台任务日志
    parse/                    解析/调试日志
  backups/                    数据库升级前备份
  legacy/                     旧配置迁移备份
```

`data/` 已加入 `.gitignore`。Cookie 文件默认是本地明文，仅依靠目录权限和“不被 Web 静态目录暴露”保护。

## WebUI 页面

- **动态**：动态/媒体视图切换、最新/最早排序、无限滚动/分页、作者筛选、灯箱、视频播放；无媒体推文和失效媒体占位可见
- **用户**：添加用户、启用/停用、模式选择、批量同步、重试、刷新头像；导入集合单独分组
- **任务**：任务队列、进度条、状态、日志尾部、取消
- **设置**：Cookie、代理、同步参数、媒体参数、Web 显示、校验/补下/头像补全/导出
- **指定推文**：URL/ID → 预览 → 下载；可选加入用户列表

“移出列表”只是停用，不会删除该用户的推文和媒体。

## 头像与横幅

- 添加/同步用户时从资料接口更新头像和横幅
- 头像保存为 `data/avatars/<user_id>_<hash>.webp`，160×160
- 横幅保存为 `data/banners/<user_id>_<hash>.webp`，最大宽 1500
- WebUI 统一请求 `/avatar/<user_id>`、`/banner/<user_id>`；没有本地头像时返回按 ID 着色的 SVG 初始头像

## 命令（高级/迁移/灾备）

```bat
:: 同步全部启用用户
.venv\Scripts\python -m xdyn sync --all

:: 只同步一个用户
.venv\Scripts\python -m xdyn sync --user @XDevelopers

:: 覆盖本次模式，全量历史，只抓元数据
.venv\Scripts\python -m xdyn sync --user @XDevelopers --mode timeline --full --no-download --max-pages 10

:: 重试媒体
.venv\Scripts\python -m xdyn retry --all

:: 校验本地媒体；--deep 会读取文件头检查 HTML 错误页
.venv\Scripts\python -m xdyn verify --repair
.venv\Scripts\python -m xdyn verify --deep

:: 合并迁移/同步产生的重复媒体（先预演）
.venv\Scripts\python -m xdyn dedupe-media --dry-run

:: 确认后写库并删除多余且大小相同的文件
.venv\Scripts\python -m xdyn dedupe-media --apply --delete-files

:: 刷新 queryId / transaction
.venv\Scripts\python -m xdyn refresh-ids

:: 统计
.venv\Scripts\python -m xdyn stats

:: 导出/重建 JSON 备份
.venv\Scripts\python -m xdyn export
.venv\Scripts\python -m xdyn export --user-id 1
.venv\Scripts\python -m xdyn import-json --dry-run
```

这些命令保留给迁移、灾备和调试；日常操作不需要使用。

## 从 twitter_download 迁移

旧脚本的 `downloads` 目录通常长这样：

```text
downloads/
  <screen_name>/                主用户媒体 + *.csv + *.md
  <#Tag>/                       标签媒体 + *-media.csv / *-text.csv
  <user_or_tweet_id>/           *-Reply.csv + *_reply.png/mp4
  profile/                      *_avatar.jpg / *_banner.jpg / *_description.txt
```

先预演，不写库、不复制：

```bat
.venv\Scripts\python -m xdyn migrate --src "<旧 downloads 目录>" --dry-run
```

正式迁移（默认复制到 `data/images`）：

```bat
.venv\Scripts\python -m xdyn migrate --src "<旧 downloads 目录>"
```

只迁移部分类别：

```bat
.venv\Scripts\python -m xdyn migrate --src "..." --include main,text,tags,replies,profile
```

文件处理模式：

- `--mode copy`（默认）：复制到 `data/images/<slug>/`
- `--mode reference`：引用旧目录，不复制（数据库保存绝对路径）
- `--mode move`：移动到 `data/images/<slug>/`

迁移行为：

- 只读取 CSV/Markdown、文件名和文件大小；不解码 feed 中的图片/视频内容
- `profile/` 头像和横幅会解码为 WebP，仅用于本地用户资料
- 主用户媒体按 `Tweet URL` 的 status ID 归组，多图推文合并为一条
- 媒体身份使用 canonical URL：图片用原图 URL，视频/GIF 用视频 variant URL；和后续同步一致，避免重复下载
- 文本 CSV 按 `(URL, 内容 hash)` 去重，避免旧脚本用 conversation id 导致误合并
- 标签/评论区导入为只读“导入集合”，不参与自动同步
- 评论区媒体按 `日期_@用户_hash_reply` 文件名尽力匹配
- 缺失文件会标记为 `pending`（有远程 URL）或 `missing`（无 URL），以后可重试
- 生成 `data/imports/<时间>/report.json`，包含未匹配 CSV 行、未匹配文件、复制字节数和错误
- 可重复运行，已存在的推文/媒体不会重复创建

**隐私说明**：如果旧 `downloads` 含敏感内容，迁移器不会解码 feed 中的图片/视频，只按文件大小判断是否存在；`verify --deep` 才会读取文件头。`profile/` 头像和横幅会被解码并转成 WebP，仅用于本地用户资料。浏览 WebUI 时媒体才会实际显示。

## 安全与风控

- 默认 `sleep = 4.0` 秒，带 ±20% 随机抖动；用户之间也暂停
- 读取 `x-rate-limit-*` 响应头；429 按 reset 等待，超过阈值停止当前任务
- 404 空响应会刷新 queryId + transaction 后重试一次
- 401/403 停止任务，不反复重试
- Cookie 不回显完整值，不写日志，不进 JSON 导出
- 所有修改类 API 要求 `X-Xdyn: 1` 请求头，降低本地 CSRF 风险
- WebUI 默认只监听 `127.0.0.1`

## 测试

```bat
.venv\Scripts\pip install -r requirements-dev.txt
.venv\Scripts\pytest
```

测试使用合成数据与临时目录，不访问真实 X，也不会加载旧 `downloads` 中的媒体。覆盖：

- Cookie 解析、设置存储、输入解析
- 旧/新 X schema、转推、引用、长短文本、媒体
- SQLite upsert、搜索、统计、媒体状态
- 同步水位/全量截断
- queryId 脚本解析
- Web 页面、媒体/缩略图、视频占位、CSRF、路径穿越
- 迁移：主媒体、文本、标签、评论区、profile、dry-run、幂等、reference 模式

## 已知限制

- X 的内部 GraphQL 接口没有公开 SLA，queryId、feature 和响应结构会变化；工具内置运行时刷新和自愈，但不能保证永久有效
- X 有时会在时间线末尾返回只含 cursor 的空页；同步默认连续 3 页无推文就停止，可在设置里调整“连续空页上限”
- transaction 签名材料优先从公共 `https://x.com/i/embed` 获取（该页面仍提供 `ondemand.s`）；x-web 的 `home/tesla` 页面通常不直接暴露签名脚本，只作为回退
- 如果 `doctor` 显示 `i/embed(public)` 401，先检查代理是否要求认证，或代理是否拦截了 x.com；如果显示 `UserByScreenName` 401/403，则是 Cookie 失效或账号受限
- 视频缩略图优先使用 X 自带封面；没有封面的旧视频显示视频占位图
- 旧回复 CSV 没有媒体文件列，迁移时只能按文件名匹配，极端情况下可能漏挂
- 引用推文的媒体默认不下载，避免重复占用空间
- 不支持登录自动化；Cookie 失效后需要在设置页重新粘贴
