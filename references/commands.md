# 命令流程与恢复

下面均在仓库根目录执行。路径可换为用户工作目录。每轮使用新目录，续跑同一轮必须保留原目录和账本。

## 免费检查

```bash
python3 scripts/run.py doctor
python3 scripts/run.py replay --out runs/demo-01
python3 scripts/check.py
```

回放完全离线；重复回放换新目录，不覆盖之前的报告。

## 新研究

```bash
python3 scripts/run.py init --out runs/research-01
python3 scripts/run.py quote --run runs/research-01
```

在这里检查计划、报价，获得用户对范围/次数/预算的明确同意。仅当同意已经存在：

```bash
python3 scripts/run.py authorize --run runs/research-01 --consent "用户确认的主题、请求上限与美元预算"
python3 scripts/run.py search --run runs/research-01 --keyword FDE --allow-paid
python3 scripts/run.py search --run runs/research-01 --keyword "Forward Deployed Engineer" --allow-paid
python3 scripts/run.py search --run runs/research-01 --keyword "前线部署工程师" --allow-paid
python3 scripts/run.py search --run runs/research-01 --keyword "前沿部署工程师" --allow-paid
python3 scripts/run.py review --run runs/research-01
```

收费命令从 `TIKHUB_API_KEY` 读取用户自己的凭证。可在用户私密终端为单次命令加 `--prompt-key`，隐藏输入、不保存。`.env.example` 仅说明变量名，脚本不会扫描或自动加载其他目录的凭证。

宿主依据 [筛选标准](editorial.md) 编辑 `candidates.json` 中的 `selected` 与 `reason`，初轮每账号一篇，最多七篇，然后：

```bash
python3 scripts/run.py verify --run runs/research-01 --allow-paid
python3 scripts/run.py draft-analysis --run runs/research-01
```

宿主读取成功缓存的正文，编辑 `analysis.json`，再导出：

```bash
python3 scripts/run.py report --run runs/research-01
python3 scripts/run.py status --run runs/research-01
```

报告未写分析也能导出阶段结果，会明确“正文分析待补充”。不要把阶段报告称为完整内容拆解。

## 失败与显式重试

同一运行目录中成功请求直接读缓存。默认不自动重试失败；仍保留本地费用预留。初始核验失败停止批次，补采失败停止补采；先看 `interruptions.json`，按具体失败说明缺口。

只有用户明确要求重试时，创建不含密钥的参数文件，例如：

```json
{"url":"https://mp.weixin.qq.com/s?__biz=目标biz&mid=目标mid&idx=1&sn=目标sn"}
```

再调用对应操作：

```bash
python3 scripts/run.py request --run runs/research-01 --operation stats --params retry-params.json --allow-paid --retry-failed
```

只有一次额外尝试，仍计入原预算。不得换目录或改账本逃过上限；真实额度不足时停止并交付已得到的数据。
