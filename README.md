# 公众号小号爆款发现

把一次真实的 **FDE 公众号选题研究**变成可复用的 Agent Skill：按主题找文章，用同一公众号的近期文章作对照，再读正文判断哪些选题值得参考。

历史试采得到 102 篇搜索候选、7 篇高表现候选。仓库包含完整脚本、方法、配置、样稿、精简历史证据和免费回放；使用新数据时，结果数量会变化。

## 给智能体的一句话

> 请获取 https://github.com/guasi18587278913/wechat-breakout-finder 的完整仓库，读取 SKILL.md，先运行 doctor 和免费 replay，打开报告。正式查询前告诉我主题范围、次数和费用。

需要能够执行本地 Python、读写文件的智能体客户端。仅有网页对话、不能运行脚本的客户端不能直接运行。分析使用当前智能体，不要求额外模型 API Key。

## 安装与免费体验

```bash
git clone https://github.com/guasi18587278913/wechat-breakout-finder.git
cd wechat-breakout-finder
python3 scripts/run.py doctor
python3 scripts/run.py replay --out runs/demo
```

打开 `runs/demo/report.html`。Windows 将 `python3` 换为 `python`。也可把完整仓库目录复制到客户端支持的 skills 目录，目录名使用 `wechat-breakout-finder`；不能只复制 `SKILL.md`。

环境：Python 3.9+、系统 curl；Python 包依赖为零。回放无需 Key、不联网、不收费。导出包括 `已核验候选.csv`、`对照文章明细.csv`、`全部搜索候选.csv`、`核验数据.json` 和 HTML。

## 正式使用

跟智能体说：**“找 FDE 相关的公众号小号爆款”**。它会提出关键词和研究计划。你需要自己的 [TikHub Key](https://user.tikhub.io/dashboard/api)，只在本机输入。

默认最多 50 次请求、USD 0.50 本地预留上限。先查询当前报价，得到本轮同意后才能取数。失败保守计入预留；预留不等于实际账单。Just One API 默认关闭，可选探针须单独核价和授权人民币预算。

这套方法识别的是“**低日常阅读账号里的高表现文章**”，并未证实真实粉丝数。对照初始只有三篇，原流程用列表创建时间作为时间代理，不能称为精确的成熟发布样本。适合发现选题线索，不是全行业榜单或增长因果证明。

## 仓库包含什么

| 材料 | 作用 |
|---|---|
| [SKILL.md](SKILL.md) | 智能体从启动到交付的执行指引 |
| [config/fde.json](config/fde.json) | FDE 关键词、范围、比较口径和预算默认值 |
| [研究协议](references/method.md) | 定义每个数字怎样产生，哪些结论不能下 |
| [筛选与报告要求](references/editorial.md) | 固化人工筛选、正文阅读和选题建议标准 |
| [环境与状态](references/environment-state.md) | 依赖、凭证、缓存、账本与迁移规则 |
| [历史样本](examples/fde-20261012/) | 白名单精简证据及七篇结果的回归期望 |
| [来源与差异](references/provenance.md) | 目标会话的定位依据，以及打包时显式修正的细节 |
| [命令流程](references/commands.md) | 可执行步骤和恢复方式 |

新研究需要重新搜索和判断。固定样本的七个账号只用于回放，不能拿来冒充新的发现。全文、私人会话、密钥、账户余额和个人历史缓存没有公开；保留下来的字段足以复算原筛选数字。

## 开发验证

```bash
python3 scripts/check.py
```

这是离线验证，不发付费请求。代码和原创说明采用 [MIT](LICENSE)；历史文章标题、链接及统计属于研究引用，权利归各自权利人，未提供文章全文转载许可。
