# 展览网页的限域离线采集包

这个 Python 项目保存采集侧的来源读取工具和合成资料。代码位于 app，示例输入位于 fixtures，现有读取工具的回归位于 tests。示例不代表任何真实单位的业务记录。

## 数据与接口

inspect_links 仅查看普通 href/src，保留 URL 片段，不执行网络请求或抓取 CSS。scope.json 分开定义页面路径范围和静态资源 origin；fixtures 中包含 CSS 循环、srcset、多种图片查询参数和跨范围跳转响应。图片是 1×1 自造 PNG。

## 本地使用

使用 Python 3.12 或更新版本，当前工具仅依赖标准库。从项目根目录执行：

```sh
python3 -m app.inspect
python3 -m unittest discover -s tests -v
python3 -m compileall -q app tests
```

读取命令输出当前素材的结构或单次读取结果。运行过程中产生的文件放在 runtime 中，与原始素材分开。公开示例仅使用 .invalid 域名，网络适配时应替换为已授权的来源。

本地查看页面可运行 `python3 -m http.server 8766 --bind 127.0.0.1 --directory fixtures`，然后访问 `http://127.0.0.1:8766/index.html`。URL 解析工具展示的 .invalid 地址表示原始来源身份，不是需要访问的真实站点。
