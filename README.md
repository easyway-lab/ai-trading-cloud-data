# GitHub 免费云端采集器

这是“全市场AI辅助交易系统”的零成本云端数据方案。GitHub Actions 在交易日定时运行 Python，采集 AKShare、BaoStock、腾讯行情与可选 Finnhub 数据，并把最新结果写入 `public/data`。

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

## 隐私与限制

- 仓库公开的是市场行情快照，不要把持仓、成本、券商账号或个人信息写进 `config.json`。
- `config.json` 中的腾讯代码只建议填写普通观察标的；持仓和成本继续保留在浏览器本地。
- GitHub 定时任务可能延迟，因此适合盘前、盘中阶段性扫描和盘后复盘，不作为秒级实盘行情源。
- Actions失败时旧快照仍然保留，网页不会清空已有数据。
