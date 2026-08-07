"""远端运维(ops):展示端自动更新 / 自愈脚本。

remote_update:环境检查+自愈 → git 拉取 → 有变更装依赖 → 重启 web+ingest →
健康检查失败自动回滚上一个 commit 并告警。纯逻辑(runner/health 可注入)便于单测;
真实 apply 在远端由 systemd timer / cron 周期触发(见 docs/参考/远端自动更新与自愈.md)。
职责:只做部署运维,不 import 分析/采集/展示业务代码。
"""
