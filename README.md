# 展览网页的限域离线采集包（site-capsule）

给已获授权的线上展览在闭展后留一份**可离线阅读、范围可控、来源可追溯**的档案。
入口是一组页面 URL；工具只在授权范围内跟随页面与静态资源，不处理音视频，
也不会因为某个获准资源的重定向而把整个站点卷进来。

所有示例仅使用 `.invalid` 域名与自造素材（自写 HTML/CSS、自生成的 1×1 PNG），
授权配置只覆盖本地样本站点，不含任何真人作品。

## 解决的问题

* 单 HTML “另存”后，图片和样式仍指向即将关闭的站点；
* 通用下载器追着日历、分享链接无限扩张，把整站拖下水；
* 闭展成品宣称可离线，却仍残留偷偷联网的引用。

本工具把**授权范围、逐跳重定向检查、内容寻址存储、来源清单、离线核查**
做成一条可恢复、可审计的流水线。

## 目录结构

```
app/
  model.py        数据模型与状态/原因常量（可 JSON 序列化）
  scope.py        范围判定、URL 规范化、安全落盘路径、相对路径计算
  wire.py         可重放传输（Replay），含可模拟中断的流式响应
  transport.py    传输抽象：ReplayTransport / HttpTransport，均不自动跟随重定向
  cssrewrite.py   CSS 的 url()/@import 提取与本地改写（注释保护）
  htmlrewrite.py  HTML 的 base/相对链接/srcset/内联样式/meta refresh 解析与改写
  archiver.py     采集编排：BFS 队列、逐跳授权、预算、续采、内容寻址、发布
  manifest.py     断点状态 state.json 与归档清单 manifest.json 的原子落盘
  offline.py      离线核查入口与 offline-report.html 生成（全程不联网）
  cli.py          命令行：archive / check / inspect
  references.py   最初的只读链接检查工具（保留兼容）
fixtures/         自造站点素材、授权配置、回放路由与路由构建脚本
tests/            单元与端到端测试（隔离模式 python3 -I 可直接运行）
run_capsule.py    隔离模式启动器（自动注入项目根到 sys.path）
```

输出包布局：

```
output/
  index.html              离线入口（链接到各页面镜像与核查报告）
  manifest.json           归档清单：页面/资源/依赖边/原URL/媒体类型/摘要/失败
  state.json              断点续采状态（运行中原子更新）
  offline-report.html     离线核查报告，逐条解释刻意保留的外部链接
  pages/...               改写后的可读页面镜像
  assets/css/<sha>.css    改写后的样式表
  store/<前2位>/<sha>.<ext>  内容寻址的原始响应字节（留证）
  partials/*.part         下载中断的分片（续采时清理重抓）
```

## 范围模型

授权在配置里分开声明，互不混淆：

* `page_prefixes`：可**跟随为页面**的 URL 前缀（路径级，按路径边界匹配，
  `/show/` 不会授权 `/showcase/`）；
* `resource_origins`：允许抓取**静态资源**的 origin（scheme://host:port），
  资源路径不限，但 origin 之外一律拒绝。

非 HTTP 协议（`mailto:`、`data:` 等）与音视频（`<audio>`/`<video>`/`<track>`
及其中的 `<source>`）刻意不采集；视频 `poster` 是封面图片，仍正常采集。

## 关键机制

### 逐跳重定向授权

传输层**不自动跟随**重定向。采集器对每一跳的 `Location` 重新做范围判定：
任一 hop 越界，该身份立即标记 `denied`，逐跳过程写入清单，越界目标不会建记录、
不会被抓取——一个获准资源无法借 302 把整站链接带进来。

### 引用解析与本地改写

覆盖 HTML 的 `<base>`、相对链接、`srcset`（保留宽度/密度描述符）、
内联 `style`、`<style>` 块、`meta refresh`，以及 CSS 的 `url()` 与 `@import`
（注释中的引用不采集）。片段标识（`#id`）原样保留，查询参数不丢弃；
未采集的引用保留原绝对 URL，并在清单与报告里写明原因，绝不静默改写或删除。

### 循环引用

循环 CSS（A `@import` B，B `@import` A）在抓取层按 URL 身份去重，天然不成环；
发布采用两阶段（先登记全部发布路径再逐个改写），循环 `@import` 闭合到包内文件。

### 同内容多地址

原始字节以 SHA-256 内容寻址存入 `store/`。不同 URL 返回相同内容时共用一个物理文件、
字节不重复计数，但每个 URL 仍是独立记录，各自保留来源、媒体类型、依赖边与
`shared_with` 共享关系——链接身份与来源不丢。

### 安全落盘

本地文件名与远端文件名脱钩。远端路径里的 `..` 段被降级为普通目录名，
不可能逃出输出目录；查询串以短哈希参与消歧。

### 断点续采与取消

每个身份处理完即原子落盘 `state.json`。下载中断保留分片并把身份重新排队，
自动重试（默认 3 次）；`KeyboardInterrupt` 取消后再次运行同一命令即可从边界继续。
续采会把 `in_progress`/`deferred` 身份重新激活，并清理陈旧分片。

### 预算耗尽的可恢复边界

深度（`max_depth`）、页面数（`max_pages`）、资源数（`max_resources`）、
体积（`max_bytes`）四类预算。耗尽时：

* 当前已采内容全部保留；
* 超出的身份标记 `deferred` 并**留在队列**，记录具体 `budget_*` 原因；
* 本次运行停止扩张但状态完整；放宽预算后重跑，从该边界继续推进。

### 清单与离线核查

`manifest.json` 关联页面、依赖边、原 URL、响应媒体类型、SHA-256 摘要、字节数、
重定向跳数与失败关系。存在失败/延期/越界拒绝时 `complete=false`，
并在 `incomplete_reasons` 列明——不会发布一个声称可离线却仍偷偷联网的成品。

`check` 子命令不发起任何网络请求，静态核查：

1. 每个已发布 HTML/CSS 的本地相对引用都能在包内找到，且无法借 `..` 逃出目录；
2. 残留的 http(s) 绝对 URL 必须能在清单边/失败关系中找到“刻意保留”的理由；
3. 清单登记的 store 文件全部存在；
4. 生成 `offline-report.html`，逐条解释外部链接为何保留外部访问。

## 命令用法

隔离模式启动器（推荐，`-I` 下也能正确导入）：

```sh
# 用回放路由采集（离线、确定性，适合演示与测试）
python3 -I run_capsule.py archive \
  --config fixtures/demo-scope.json \
  --output runtime/demo \
  --replay fixtures/demo-routes.json

# 离线核查（断网可跑，退出码 0 通过 / 1 有完整性问题 / 3 采集后核查未过）
python3 -I run_capsule.py check --output runtime/demo

# 只读检查单个 HTML 的引用结构
python3 -I run_capsule.py inspect \
  --html fixtures/index.html \
  --base-url https://exhibition.invalid/show/index.html
```

对真实授权站点使用 HTTP 传输（去掉 `--replay` 即走 `HttpTransport`）：

```sh
python3 -I run_capsule.py archive --config scope.json --output runtime/capsule
```

## 验证场景

测试与 `fixtures/build_routes.py` 显式覆盖需求点名的四类情形：

| 场景 | 构造方式 | 对应测试 |
| --- | --- | --- |
| 循环 CSS | `a.css`/`b.css` 互相 `@import` | `test_circular_css_resolved_without_loop` |
| 重定向越界 | 域内 302 一跳后 302 到未授权 origin | `test_redirect_leaving_scope_is_denied_hop_by_hop` |
| 同内容多地址 | 两个 URL 返回字节完全相同的 PNG | `test_identical_content_shares_store_but_keeps_identities` |
| 下载中断 | 首响应吐 12 字节即断、二次成功 | `test_interrupted_download_recovers_on_retry` |

另含：深度/页面数/体积预算耗尽与恢复、取消后续采、远端 `..` 路径不可逃逸、
片段保留、查询不丢弃、音视频排除、外部 base、404 失败关系、干净包 `complete=true`。

```sh
python3 -I fixtures/build_routes.py     # 由素材生成回放路由
python3 -I -m unittest discover -s tests -v
python3 -I -m compileall -q app tests run_capsule.py
```

## 范围与非目标

* 只采集 HTML/CSS 与静态资源（图片、脚本、字体、图标等）；不处理音视频；
* 不执行 JavaScript，因此不发现 JS 运行时才生成的链接；
* 回放传输用于离线确定性测试，真实抓取请用内置 HTTP 传输并确认授权范围。
