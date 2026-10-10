# Checkpoint 与请求幂等

## 背景

澄清、分配、审核都会把图停住。刷新页面要能看到清单，续跑要能回到同一格。重复点击不能再开一趟。

## 决策

两套存储：SQLite 快照给人读；LangGraph SQLite checkpointer 存图游标，键是稳定的 `graph_ns`。`claim_request` 对同一个 `request_id` 回放第一次的快照。线程处于 planning 或 awaiting_review 时，新的 `request_id` 返回 409。

## 取舍

快照和 checkpoint 可能短暂不一致（节点已写游标、快照还没落盘）。恢复时以 checkpoint 续跑，快照在每次 SSE 结束时重写。

## 后果

澄清续跑不把正在等待的线程传给 `claim_request`，否则会被自己判成忙碌。分配和审核的幂等只按 `request_id`。
