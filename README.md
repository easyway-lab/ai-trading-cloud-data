# GitHub 免费云端采集器

这是“全市场AI辅助交易系统”的零成本云端数据方案。GitHub Actions 在交易日定时运行 Python，按 AKShare 东方财富、AKShare 新浪、BaoStock 股票池加腾讯行情的顺序自动切换全市场数据源，并把最新结果写入 `public/data`。行业映射优先使用 AKShare 东方财富行业板块，失败时自动切换 BaoStock 行业分类。Finnhub 外围市场数据为可选项。

1.2 版同时自动生成：

- `snapshot.json`：全市场行情、技术结构、行业和公告风险标记；
- `market_overview.json`：上证、深证、创业板、沪深300、科创50、中证1000、北证50与市场情绪；
- `announcements.json`：重点候选和持仓观察代码的巨潮资讯公告索引、原文链接、交易所复核入口与标题风险词；
- `sector_map.json`：自动行业映射；
- `wencai_sector_overrides.json`：可选的同花顺问财行业校正层。

采集器会用最少 3000 条有效行情作为成功门槛。全部行情源失效时，旧的有效 `snapshot.json` 不会被空数据覆盖，`health.json` 会标记 `stale=true`，同时 Actions 任务会显示失败，避免“绿色任务、空白数据”的假成功。

## 一次性安装

1. 登录 GitHub，点击右上角 `+` → `New repository`。
2. 仓库名建议填写 `ai-trading-cloud-data`，选择 `Public`，不要勾选初始化 README。
3. 解压本安装包，把解压后的全部内容上传到仓库根目录。必须包含 `.github`、`public`、`collector_cloud.py` 等文件。
4. 进入仓库 `Settings` → `Actions` → `General` → `Workflow permissions`，选择 `Read and write permissions` 并保存。
5. 进入仓库 `Actions`，启用工作流；打开 `Collect A-share market data`，点击 `Run workflow`，首次保持 `refresh_history=true`。
6. 等待任务变为绿色。第一次需要补候选股历史结构，通常比日常任务慢。

## 在网页中连接

假设 GitHub 用户名是 `zhangsan`，仓库名是 `ai-trading-cloud-data`，则云端数据地址为：

`https://raw.githubusercontent.com/zhangsan/ai-trading-cloud-data/main/public/data`

打开“全市场AI辅助交易系统”→“数据与扫描”，选择 GitHub 云端模式，粘贴这个地址并连接。

## Finnhub（可选）

仓库 `Settings` → `Secrets and variables` → `Actions` → `New repository secret`：

- Name：`FINNHUB_API_KEY`
- Secret：你申请的 Finnhub 密钥

密钥不会写入公开仓库。没有配置时，A股采集仍然运行。

## 用问财补齐行业

系统会自动维护行业映射。若你希望用同花顺问财结果校正个别股票，可编辑 `public/data/wencai_sector_overrides.json`，只填写代码与行业，例如：

```json
{"schemaVersion":"1.0","source":"同花顺问财用户校正","items":{"000063":"通信设备"}}
```

网页中的问财导入也会把行业校正保存到网站私有数据中并跨设备同步。公开仓库内不要填写持仓、成本、账户金额或复盘内容。

## 公告核验边界

采集器自动从巨潮资讯法定披露平台获取重点代码的公告标题和原文链接，并提供上交所、深交所或北交所复核入口。标题风险词只负责预警，不能替代阅读公告原文；发生接口故障时保留上次有效公告快照并在 `health.json` 中报错。

## 隐私与限制

- 仓库公开的是市场行情快照，不要把持仓、成本、券商账号、复盘或个人信息写进 `config.json` 或任何点文件。点文件并不等于私密文件。
- `config.json` 中的腾讯代码只建议填写普通观察标的；持仓和成本继续保留在浏览器本地。
- GitHub 定时任务可能延迟，因此适合盘前、盘中阶段性扫描和盘后复盘，不作为秒级实盘行情源。
- Actions失败时旧快照仍然保留，网页不会清空已有数据。
- BaoStock 历史技术指标覆盖沪市主板、深市主板、创业板和科创板；北交所仍参与实时全市场扫描，但暂不使用 BaoStock 计算历史均线。
