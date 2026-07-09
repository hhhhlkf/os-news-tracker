"""一次性脚本：将 osnews_empty_test 中超过50字的 why_it_matters 归纳为 ≤50 字。

运行：cd backend && DATABASE_URL=postgresql+psycopg://osnews_app:OsNewsTracker2026DbA7K9M4@localhost:15432/osnews_empty_test .venv/bin/python -m scripts.condense_reasons_v2
"""

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Item

CONDENSED: dict[int, str] = {
    7: "CHERI-D低开销实现UAF缓解，为内核内存安全设计提供新思路。",
    9: "共享GPU内存侧信道防御方案无需硬件改动，为驱动设计提供参考。",
    13: "修复Raptor Lake崩溃并增SIMD加速，Rust实现内存安全可替代C zlib。",
    14: "KUnit支持JUnit输出可对接CI系统，值得集成以自动化内核测试。",
    16: "bpftool批量操作struct_ops后未重置BTF状态，需关注此修复避免异常。",
    20: "借鉴前瞻补丁与调度器调优，需关注驱动移除及x86_64-v3兼容性风险。",
    22: "v261引入无TPM安全启动和内核热升级，需关注云环境适配。",
    23: "AUR供应链攻击暴露孤儿包接管风险，需审视信任模型与防范注入。",
    25: "单跳块复制结合RDMA多播降开销，将影响内核块层，维护者应审查。",
    29: "NVK解锁Tensor核心AI超分，需关注异构算力调度与驱动集成影响。",
    34: "多分支修复内存竞态与越界等高危缺陷，提供防数据损坏修复范本。",
    35: "eprobe读取字符串勿强转char*，须用完整指针宽度避免显示错误。",
    37: "防止交换紧缺时徒劳大页分割，降低CPU开销，需关注memcg计费。",
    38: "更新熵服务规避符号链接攻击，借鉴systemd网络隔离与权限最小化。",
    39: "修复信号量预种植漏洞并示范网络隔离，可借鉴systemd加固方案。",
    41: "kselftest覆盖allocinfo ioctl过滤，修复构建避免编译断裂，需关注。",
    42: "bpftool批量操作后缓存指针未置空致UAF崩溃，需关注状态清洗。",
    52: "RHEL 9.6 EUS内核修复多个重要安全漏洞，需审查CVE并移植修复。",
    57: "拆分零计数与冻结状态缓解锁竞争，但ABA竞争暴露引用计数隐患。",
    64: "zlib-rs 0.6.4修复Raptor Lake和adler32错误，须尽快合并防损坏。",
    65: "KVM VFIO中断释放UAF可致提权或崩溃，修复加置空，需审查同类风险。",
    67: "systemd服务隔离与信号量防护加固随机数服务，可借鉴至其他服务。",
    68: "zlib-rs aarch64 adler32错误致校验和失准，须跟进修复防数据损坏。",
    69: "SMT感知并行热插拔提升CPU上下电效率，需关注调度器依赖与配置风险。",
    70: "zlib-rs 0.6.4修复adler32和Raptor Lake缺陷，引入方须尽快更新。",
    71: "zlib-rs修复Raptor Lake和adler32错误，须跟踪跨架构正确性修复。",
    75: "ARM64性能计数语义偏差致perf分支缺失率失真，须排查PMU映射。",
    78: "nf_tables钩子释放RCU竞态致崩溃，复用回调结构适配是回传范本。",
    80: "ext4 extent分裂缓存陈旧致数据损坏，需合入nocache补丁规避CVE。",
    90: "HFS+ bmap缺少偏移验证致越界崩溃，修复提供通用加固思路。",
    103: "CVE警示跟踪事件访问已释放内存风险，预捕获字段修复模式可借鉴。",
    119: "双内核设计兼顾RHEL兼容与云原生，ANCK的XDP/io_uring优化可借鉴。",
    121: "关注OpenAnolis云内核与机密计算，借鉴云原生全栈实践应对调优。",
    160: "CFS扩展至能量域实现功耗比例分配，可借鉴优化电池续航与调度。",
    163: "融合频次与新鲜度的自适应淘汰策略可弥补LRU缺陷，优化I/O路径。",
    164: "调度器整合预测与伸缩降尾延迟61.5%，值得借鉴优化低延迟SLO。",
    165: "BatchGen事件驱动协程模型可优化内核调度与异构内存管理。",
}


def main() -> None:
    db = SessionLocal()
    try:
        items = db.scalars(
            select(Item).where(Item.id.in_(CONDENSED.keys()))
        ).all()
        print(f"待更新条目：{len(items)}")
        updated = 0
        for item in items:
            new_text = CONDENSED.get(item.id)
            if new_text is None:
                continue
            old_len = len(item.why_it_matters or "")
            new_len = len(new_text)
            item.why_it_matters = new_text
            db.commit()
            updated += 1
            print(f"id={item.id} {old_len}→{new_len}字 ok")
        print(f"\n完成：{updated}/{len(CONDENSED)} 条已更新")

        from sqlalchemy import func

        remaining = db.scalar(
            select(func.count())
            .select_from(Item)
            .where(func.length(Item.why_it_matters) > 50)
        )
        print(f"数据库中仍超过50字的条目数：{remaining}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
