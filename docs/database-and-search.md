# 数据库与 Web Search

默认使用项目内的 SQLite 示例库，无需安装独立数据库服务。项目也包含 MySQL 驱动，可通过配置连接已有 MySQL 数据库。

## 本机 SQLite

`.env` 默认配置：

```dotenv
DB_ENGINE=sqlite
SQLITE_PATH=data/agent_demo.db
DB_QUERY_TIMEOUT_SECONDS=10
DB_MAX_ROWS=200
```

首次执行 `mcp-agent db-init` 创建新的示例文件，包含虚构商品表 `products(id,name,category,price,stock)` 和订单表 `orders(id,product_id,quantity,ordered_at)`；已有文件会被拒绝覆盖。另建示例库可用 `mcp-agent db-init --path data/another_demo.db`，随后修改 `SQLITE_PATH`。

无需 LLM Key 即可通过真实 MCP 检查数据：

```powershell
uv run mcp-agent call database_database_info
uv run mcp-agent call database_list_tables
uv run mcp-agent call database_query --args-file examples/database-query.json
```

`call` 支持 `--args` JSON 字符串和 `--args-file` UTF-8 文件，失败时返回非零退出码。文件方式可以避免不同 PowerShell 版本的引号转义问题。表结构工具参数为 `{"table_name":"products"}`。

参数化查询示例：

```json
{
  "sql": "SELECT id, name, price FROM products WHERE price >= :min_price ORDER BY price",
  "parameters": {"min_price": 10},
  "max_rows": 20
}
```

用户值使用 `:name` 绑定，表名和字段名不能作为绑定值。结果返回 `columns`、对应的 `rows` 数组，以及 `row_count`、`truncated` 等字段。最多返回 200 行，可通过 `DB_MAX_ROWS` 缩小上限。

数据库工具只读：查询前解析 SQL，拒绝多语句、写操作及不支持的语句/函数；SQLite 以 `mode=ro` 打开真实文件，MySQL 使用只读事务和查询超时。模型先查看方言、表和字段，再生成查询。

## 在另一台电脑使用 MySQL

1. 复制项目源码、`pyproject.toml`、`uv.lock`、`config` 和 `examples` 到目标电脑。不要复制虚拟环境 `.venv`、`.cache` 或本机真实凭据。
2. 安装 [uv](https://docs.astral.sh/uv/getting-started/installation/)，在项目目录运行 `uv sync --locked`，建立该电脑自己的环境。PyMySQL 与认证依赖会自动安装。
3. 从 `.env.example` 创建 `.env`，设置 LLM 配置，并修改以下数据库字段。

```dotenv
DB_ENGINE=mysql
MYSQL_HOST=127.0.0.1
MYSQL_PORT=3306
MYSQL_DATABASE=你的数据库名
MYSQL_USER=agent_reader
MYSQL_PASSWORD='你的数据库密码'
DB_QUERY_TIMEOUT_SECONDS=10
DB_MAX_ROWS=200
# 远程 MySQL 使用 TLS 时可指定 CA 文件：
# MYSQL_SSL_CA=C:/certs/mysql-ca.pem
```

数据库与智能体在同一电脑时用 `127.0.0.1`，否则填写实际主机地址。密码通过独立字段构造连接，不需要手动 URL 编码；`.env` 中含空格、`#` 等字符的值请加引号。不要提交含真实密码的 `.env`。

建议 MySQL 8.x；查询超时需要 MySQL 5.7.8+，当前不以 MariaDB 为兼容目标。目标库必须已经存在，使用仅有目标库 `SELECT` / `SHOW VIEW` 权限的账号。管理员可参考 `config/mysql-readonly.sql`，替换其中的数据库名、密码和客户端主机后自行执行。

目标电脑验证命令：

```powershell
uv run mcp-agent call database_database_info
uv run mcp-agent call database_list_tables
uv run mcp-agent ask "查看数据库表结构，说明有哪些可查询的数据。" --trace
```

无需复制 SQLite 文件或修改 LangGraph，也不会自动把 SQLite 数据迁入 MySQL。`db-init` 只创建 SQLite 示例文件，不会在 MySQL 建库写表。

自动测试验证驱动、连接配置和事务逻辑；部署时仍须实际验证目标电脑的连接地址、账号、权限和服务器兼容性。

## Web Search

```dotenv
WEB_SEARCH_PROVIDER=auto
TAVILY_API_KEY=
WEB_SEARCH_TIMEOUT_SECONDS=15
WEB_SEARCH_MAX_RESULTS=5
WEB_SEARCH_REGION=wt-wt
WEB_SEARCH_BACKEND=auto
```

`auto` 在配置 Tavily Key 时使用 Tavily，否则使用无需 API Key 的 DDGS。也可明确指定 `ddgs` 或 `tavily`。DDGS 使用公共搜索引擎，结果受网络、地区和限流影响；`WEB_SEARCH_BACKEND` 可指定后端，例如 `bing` 或 `duckduckgo`。项目限定 DDGS 9.16+，该版本不使用旧版 DHT/P2P 缓存路径。

```powershell
uv run mcp-agent call web_search --args-file examples/web-search.json
uv run mcp-agent ask "搜索 LangGraph 的 MCP 文档，并给出来源链接。" --trace
```

工具返回 `{query, provider, results:[{title,url,snippet}]}`，最多 10 条，默认上限 5 条。联网失败会返回错误，不生成假结果。当前实现搜索摘要与链接，尚未抓取网页全文；系统提示要求在回答中引用工具返回的 URL。数据库内容不会由代码自动提交给搜索服务。

DDGS 已通过真实 MCP 联网验证，TLS 校验保持开启。Tavily 自动测试使用模拟 API 响应；启用时需配置有效 Key，并验证实际网络和账号额度。

## 配置和扩展位置

三个 Server 注册在 `config/mcp.servers.json`。`inherit_env` 显式声明要从环境或 `.env` 传入子进程的配置，系统环境优先于 `.env`，显式 `env` 优先于继承值。数据库密码的环境继承仅发给数据库 Server；LLM Key 不自动透传。内置模块仍会独立读取当前工作目录 `.env`，所以本地 Server 属于同一信任边界。

- `src/mcp_agent/database.py`：数据库配置、查询和初始化。
- `src/mcp_agent/web_search.py`：搜索供应商和结果归一化。
- `src/mcp_agent/servers/`：MCP 工具注册。
- `src/mcp_agent/graph.py`：工具选择循环和来源引用提示。

所有相对路径都以当前工作目录为基准。单次数据库查询由 `DB_QUERY_TIMEOUT_SECONDS` 控制，搜索请求由 `WEB_SEARCH_TIMEOUT_SECONDS` 控制；一轮智能体任务或直接工具调用仍受 `AGENT_TIMEOUT_SECONDS` 总超时限制。

官方参考：[SQLAlchemy MySQL](https://docs.sqlalchemy.org/en/20/dialects/mysql.html)、[DDGS](https://github.com/deedy5/ddgs)、[Tavily Search](https://docs.tavily.com/documentation/api-reference/endpoint/search)。
