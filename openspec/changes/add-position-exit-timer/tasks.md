## 1. Implementation
- [x] 1.1 配置项 max_hold_s、stop_loss_bps、exit_reduce_offset_bps，默认关闭，并加校验
- [x] 1.2 持仓计时：从零变非零或方向翻转时开始，归零时清除，重启时从启动时刻开始
- [x] 1.3 超时或止损时撤单、核零，再用 IOC 平到零；复用硬上限减仓的安全流程；IOC 未成交时退避重试，不熔断
- [x] 1.4 持仓期间减仓侧使用 exit_reduce_offset_bps
- [x] 1.5 健康统计和摘要记录离场次数与原因
- [x] 1.6 测试
## 2. Deploy
- [x] 2.1 生产配置 max_hold_s=600、stop_loss_bps=30、exit_reduce_offset_bps=10（IBM 和 RDDT）
