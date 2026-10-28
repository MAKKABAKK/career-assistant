# 来源与复用

本项目由 [HK Job Radar](https://github.com/prui0474/hk-job-radar) 已有检出整合，基线 commit `e2bcb2ba917644b8a88d9981d4e3eb514be98c65`。原 Git 历史保留在本地；本公开仓库从当前整合版本开始记录提交。保留 [MIT 许可证](../LICENSE)，版权 Rui Zhou；不是完全原创项目。

复用 models、db、config、scrapers、scoring.keyword_filter、notify 和调度触发逻辑。网页、PDF、三角色分析、版本化材料、申请记录、持久 Worker、缓存及每日汇总来自此前课程扩展，现整合到同一个 career_assistant 包。

本项目独立维护和发布，不代表原作者发布或背书。保留 MIT 许可证中的 Copyright (c) 2026 Rui Zhou；复用代码不声明为完全原创。旧 CLI、配置向导和本地填表不属于当前程序入口。
