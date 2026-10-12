# 可选 Just One API 质量探针

默认研究不调用它，不要求它的 Key。原试采用的账号搜索 V1 每页一个结果，历史价格为 CNY 0.40；当时十二次请求五次账号不匹配，粉丝数来源和更新时间也不能证实。它按成功业务响应计费，结果无用不必然免费。当前规则和价格须以 [使用说明](https://docs.justoneapi.com/zh/usage) 与使用者控制台为准，不能沿用示例单价或假定前两次免费。

接口：[账号搜索 V1](https://docs.justoneapi.com/zh/api/wechat-official-accounts/account-search-v1)，POST `https://api.justoneapi.com/api/weixin/search-account/v1`，参数 token、keyword、page。响应 `code=0`，账号数组位于 `data.data`。

仅当用户明确要测试本供应商时：

1. 在其控制台核实当前单价，说明最多两次、人民币预算，不与 TikHub 美元相加。
2. 获取单独的明确授权；使用者设置自己的 `JUSTONE_API_TOKEN`，不在聊天或参数中传密钥。
3. 建立独立计划，例如在当前报价确为 0.40 元且用户同意 0.80 元时：

```bash
python3 scripts/justone_probe.py init --run runs/research-01 --unit-cny 0.40 --budget-cny 0.80 --price-source https://dashboard.justoneapi.com/ --consent "用户明确同意的本轮探针范围与人民币预算"
python3 scripts/justone_probe.py call --run runs/research-01 --keyword "已知目标账号" --expected-biz "从已核验文章长链读取的 biz" --allow-paid
```

这只是语法例子，不是付费授权。程序在发送前预留全价，最多两次，不假设免费额度。账号必须通过 biz 精确匹配，不能只比名称。

`lastest_publish` 是最近发文时间，不是粉丝指标的更新时间。当前响应没有充分的粉丝统计来源与时效证明，因此即使 biz 匹配，探针也保留不可用标记并停止，不自动把粉丝数用于小号判断。若供应商以后新增可靠字段，应先另行验证再改适配器。

`justone-ledger.json` 保留独立费用与停止原因；已成功处理的同一探针可读缓存，失败不重试。本包没有执行新的 Just One API 实测，只有原快照与离线模拟验证。
