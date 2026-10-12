from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.research_graph.evidence_corpus import validate_run_directory, write_jsonl

DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "data" / "research_runs"
TOPICS = ("HBM", "CPO/硅光/光互连", "800V HVDC/数据中心电力")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _manifest() -> list[dict[str, Any]]:
    retrieved_at = _now()
    return [
        {
            "source_id": "src_hbm_skhynix_hbm_leadership",
            "topic": "HBM",
            "source_type": "company_website",
            "title": "One-Team Spirit: SK hynix's Journey to HBM Leader",
            "url": "https://news.skhynix.com/one-team-spirit-REPLACE_WITH_LOCAL_API_KEY/",
            "publisher": "SK hynix Newsroom",
            "publish_date": "2025",
            "retrieved_at": retrieved_at,
            "status": "success",
            "failure_reason": "",
            "relevance_hint": "官方回顾 HBM 发展，说明 TSV、WLP 与 HBM 产业化关系。",
        },
        {
            "source_id": "src_hbm_nvidia_blackwell",
            "topic": "HBM",
            "source_type": "technical_doc",
            "title": "NVIDIA Blackwell Architecture",
            "url": "https://www.nvidia.com/en-us/data-center/technologies/blackwell-architecture/",
            "publisher": "NVIDIA",
            "publish_date": "2024",
            "retrieved_at": retrieved_at,
            "status": "success",
            "failure_reason": "",
            "relevance_hint": "官方 Blackwell 架构页用于验证 AI 加速器与 HBM3E 高带宽内存的系统级关系。",
        },
        {
            "source_id": "src_hbm_micron_hbm3e",
            "topic": "HBM",
            "source_type": "company_website",
            "title": "HBM3E | Micron Technology Inc.",
            "url": "https://www.micron.com/products/memory/hbm/hbm3e",
            "publisher": "Micron",
            "publish_date": "",
            "retrieved_at": retrieved_at,
            "status": "success",
            "failure_reason": "",
            "relevance_hint": "官方产品页说明 HBM3E 容量、带宽和 AI 训练/推理用途。",
        },
        {
            "source_id": "src_hbm_qualcomm_ai200_ai250",
            "topic": "HBM",
            "source_type": "company_website",
            "title": "Qualcomm Unveils AI200 and AI250",
            "url": "https://www.qualcomm.com/news/releases/2025/10/qualcomm-unveils-ai200-and-ai250-redefining-rack-scale-data-cent",
            "publisher": "Qualcomm",
            "publish_date": "2025-10-28",
            "retrieved_at": retrieved_at,
            "status": "success",
            "failure_reason": "",
            "relevance_hint": "官方发布 AI200/AI250 与 HBC 近存计算路线，可作为 HBM 替代路线反证候选。",
        },
        {
            "source_id": "src_cpo_broadcom_cpo",
            "topic": "CPO/硅光/光互连",
            "source_type": "company_website",
            "title": "Highest Power Efficiency and Bandwidth Density - Broadcom CPO",
            "url": "https://www.broadcom.com/info/optics/cpo",
            "publisher": "Broadcom",
            "publish_date": "",
            "retrieved_at": retrieved_at,
            "status": "success",
            "failure_reason": "",
            "relevance_hint": "官方 CPO 页面描述光学与硅在单封装基板上的集成。",
        },
        {
            "source_id": "src_cpo_openlight_silicon_photonics",
            "topic": "CPO/硅光/光互连",
            "source_type": "company_website",
            "title": "OpenLight Technology",
            "url": "https://openlightphotonics.com/technology",
            "publisher": "OpenLight",
            "publish_date": "",
            "retrieved_at": retrieved_at,
            "status": "success",
            "failure_reason": "",
            "relevance_hint": "官方技术页说明硅光平台、集成激光器和放大器等光 I/O 关键节点。",
        },
        {
            "source_id": "src_cpo_coherent_1_6t",
            "topic": "CPO/硅光/光互连",
            "source_type": "company_website",
            "title": "Silicon Photonics Based 1.6T Transceiver Modules",
            "url": "https://www.coherent.com/news/press-releases/silicon-photonics-based-transceiver-modules",
            "publisher": "Coherent",
            "publish_date": "2025-03-31",
            "retrieved_at": retrieved_at,
            "status": "success",
            "failure_reason": "",
            "relevance_hint": "官方新闻稿说明硅光 1.6T-DR8 光模块、DSP、电/光接口。",
        },
        {
            "source_id": "src_cpo_arista_xpo_whitepaper",
            "topic": "CPO/硅光/光互连",
            "source_type": "technical_doc",
            "title": "XPO: Redefining Pluggable Optics for AI Networking",
            "url": "https://www.arista.com/assets/data/pdf/Whitepapers/XPO-Whitepaper.pdf",
            "publisher": "Arista",
            "publish_date": "2026",
            "retrieved_at": retrieved_at,
            "status": "success",
            "failure_reason": "",
            "relevance_hint": "技术白皮书指出 CPO 在可维护性、可扩展性、可制造性上的部署挑战。",
        },
        {
            "source_id": "src_800v_nvidia_architecture",
            "topic": "800V HVDC/数据中心电力",
            "source_type": "technical_doc",
            "title": "800 VDC Architecture for AI Data Centers",
            "url": "https://www.nvidia.com/en-us/data-center/technologies/800-vdc-architecture/",
            "publisher": "NVIDIA",
            "publish_date": "2025",
            "retrieved_at": retrieved_at,
            "status": "success",
            "failure_reason": "",
            "relevance_hint": "官方技术页描述从 AC 配电向 800 VDC 的数据中心架构演进。",
        },
        {
            "source_id": "src_800v_nvidia_ecosystem",
            "topic": "800V HVDC/数据中心电力",
            "source_type": "technical_doc",
            "title": "Building the 800 VDC Ecosystem for Efficient, Scalable AI Factories",
            "url": "https://developer.nvidia.com/blog/building-the-800-vdc-ecosystem-for-efficient-scalable-ai-factories/",
            "publisher": "NVIDIA Developer Blog",
            "publish_date": "2025",
            "retrieved_at": retrieved_at,
            "status": "success",
            "failure_reason": "",
            "relevance_hint": "官方技术博客说明 800 VDC 生态、Kyber 机架与 64:1 LLC 转换器。",
        },
        {
            "source_id": "src_800v_flex_power_shelf",
            "topic": "800V HVDC/数据中心电力",
            "source_type": "company_website",
            "title": "Advancing the transition to 800 VDC data centers with NVIDIA",
            "url": "https://flex.com/resources/advancing-the-transition-to-800-vdc-data-centers-with-nvidia",
            "publisher": "Flex",
            "publish_date": "2025",
            "retrieved_at": retrieved_at,
            "status": "success",
            "failure_reason": "",
            "relevance_hint": "供应链公司说明与 NVIDIA 协作的高密度 DC power shelf 与效率指标。",
        },
        {
            "source_id": "src_800v_ti_datacenter_power",
            "topic": "800V HVDC/数据中心电力",
            "source_type": "technical_doc",
            "title": "Powering data centers for the AI era",
            "url": "https://www.ti.com/document-viewer/lit/html/SSZT083",
            "publisher": "Texas Instruments",
            "publish_date": "2025",
            "retrieved_at": retrieved_at,
            "status": "success",
            "failure_reason": "",
            "relevance_hint": "TI 技术文档讨论 AI 数据中心电源转换、48V/高压架构、效率和功率密度约束。",
        },
        {
            "source_id": "fail_hbm_tsmc_cowos_challenge",
            "topic": "HBM",
            "source_type": "company_website",
            "title": "CoWoS - Taiwan Semiconductor Manufacturing Company Limited",
            "url": "https://3dfabric.tsmc.com/english/dedicatedFoundry/technology/cowos.htm",
            "publisher": "TSMC",
            "publish_date": "",
            "retrieved_at": retrieved_at,
            "status": "failed",
            "failure_reason": "curl 探测返回 Cloudflare challenge/403，未抓到正文；未把该页面写入 raw_evidence。",
            "relevance_hint": "原计划验证 CoWoS-S、硅中介层与 HBM cubes 集成关系。",
        },
        {
            "source_id": "fail_hbm_ieee_hbm4_paywall",
            "topic": "HBM",
            "source_type": "paper",
            "title": "HBM4 package design paper search",
            "url": "https://ieeexplore.ieee.org/",
            "publisher": "IEEE Xplore",
            "publish_date": "",
            "retrieved_at": retrieved_at,
            "status": "failed",
            "failure_reason": "搜索入口可见，但正文通常需要订阅；本轮未抓取全文。",
            "relevance_hint": "原计划用于验证 HBM4 封装、TSV、热管理论文证据。",
        },
        {
            "source_id": "fail_cpo_ieee_cpo_paywall",
            "topic": "CPO/硅光/光互连",
            "source_type": "paper",
            "title": "CPO status challenges paper search",
            "url": "https://ieeexplore.ieee.org/document/9771009/",
            "publisher": "IEEE Xplore",
            "publish_date": "",
            "retrieved_at": retrieved_at,
            "status": "failed",
            "failure_reason": "页面可检索到摘要，但全文访问受限；未将摘要当作强证据。",
            "relevance_hint": "原计划用于补充硅光相干收发模块技术趋势。",
        },
        {
            "source_id": "fail_cpo_intel_forbidden",
            "topic": "CPO/硅光/光互连",
            "source_type": "company_website",
            "title": "Intel Silicon Photonics",
            "url": "https://www.intel.com/content/www/us/en/products/details/network-io/silicon-photonics.html",
            "publisher": "Intel",
            "publish_date": "",
            "retrieved_at": retrieved_at,
            "status": "failed",
            "failure_reason": "urllib 返回 403，curl GET 未能取得正文；未写入 raw_evidence。",
            "relevance_hint": "原计划用于验证 PIC、片上激光器、EIC 和光 I/O 子系统。",
        },
        {
            "source_id": "fail_800v_old_nvidia_url",
            "topic": "800V HVDC/数据中心电力",
            "source_type": "technical_doc",
            "title": "NVIDIA 800 VDC guessed URL",
            "url": "https://www.nvidia.com/en-us/data-center/technologies/800vdc-architecture/",
            "publisher": "NVIDIA",
            "publish_date": "",
            "retrieved_at": retrieved_at,
            "status": "failed",
            "failure_reason": "该猜测 URL 返回 404；已改用官方 800-vdc-architecture 页面。",
            "relevance_hint": "记录真实失败尝试，避免把错误 URL 当来源。",
        },
        {
            "source_id": "fail_800v_eaton_timeout",
            "topic": "800V HVDC/数据中心电力",
            "source_type": "company_website",
            "title": "Eaton 800 VDC AI factories architecture page",
            "url": "https://www.eaton.com/us/en-us/company/news-insights/news-releases/2025/eaton-unveils-next-generation-architecture.html",
            "publisher": "Eaton",
            "publish_date": "2025-10-13",
            "retrieved_at": retrieved_at,
            "status": "failed",
            "failure_reason": "本地 curl 探测 30 秒超时，未抓到正文；本轮仅记录为失败来源。",
            "relevance_hint": "原计划用于验证 800VDC 保护和断路架构。",
        },
    ]


def _evidence() -> list[dict[str, Any]]:
    return [
        _ev(
            "ev_hbm_skhynix_tsv_wlp",
            "HBM",
            "src_hbm_skhynix_hbm_leadership",
            "company_website",
            "One-Team Spirit: SK hynix's Journey to HBM Leader",
            "https://news.skhynix.com/one-team-spirit-REPLACE_WITH_LOCAL_API_KEY/",
            "2025",
            "SK hynix 官方回顾将 TSV 与 WLP 作为 HBM 早期量产和封装路线的关键支撑。",
            "HBM 不是单一存储颗粒概念，至少需要 TSV、晶圆级封装等工艺节点验证。",
            ["SK hynix", "HBM", "TSV", "WLP"],
            ["HBM", "TSV", "晶圆级封装", "DRAM堆叠"],
            ["PART_OF", "REQUIRES"],
            "LIKELY",
            "官方公司资料，能支持技术链条关系，但不直接证明 A 股公司受益。",
        ),
        _ev(
            "ev_hbm_nvidia_blackwell_hbm3e",
            "HBM",
            "src_hbm_nvidia_blackwell",
            "technical_doc",
            "NVIDIA Blackwell Architecture",
            "https://www.nvidia.com/en-us/data-center/technologies/blackwell-architecture/",
            "2024",
            "NVIDIA 官方 Blackwell 架构页将 HBM3E 作为 AI 加速器内存子系统的关键组成。",
            "HBM3E 与 AI 加速器封装/系统设计强相关，但不直接证明任何 A 股公司受益。",
            ["NVIDIA", "Blackwell", "HBM3E", "AI accelerator"],
            ["HBM3E", "AI加速器", "高带宽内存", "GPU内存子系统"],
            ["REQUIRES", "PART_OF"],
            "LIKELY",
            "官方技术页面可支持产品/系统关系，仍需封装产能和供应商证据。",
        ),
        _ev(
            "ev_hbm_micron_hbm3e_product",
            "HBM",
            "src_hbm_micron_hbm3e",
            "company_website",
            "HBM3E | Micron Technology Inc.",
            "https://www.micron.com/products/memory/hbm/hbm3e",
            "",
            "Micron 官方产品页把 HBM3E 描述为 AI 内存产品，强调 8-high、24GB、约 1.2TB/s 级别带宽。",
            "Micron 可形成 HBM3E 产品级候选 claim，但该证据不映射中国主板公司。",
            ["Micron", "HBM3E", "AI memory"],
            ["HBM3E", "HBM", "AI内存"],
            ["PRODUCES", "PART_OF"],
            "LIKELY",
            "官方产品页，产品存在性较强；供应关系与客户关系仍需另证。",
        ),
        _ev(
            "ev_hbm_qualcomm_hbc_substitution",
            "HBM",
            "src_hbm_qualcomm_ai200_ai250",
            "company_website",
            "Qualcomm Unveils AI200 and AI250",
            "https://www.qualcomm.com/news/releases/2025/10/qualcomm-unveils-ai200-and-ai250-redefining-rack-scale-data-cent",
            "2025-10-28",
            "Qualcomm 官方发布 AI200/AI250，并说明 AI250 将使用近存计算方向的创新内存架构。",
            "HBC/近存计算可作为 HBM 路线的替代风险或旁路路线，不应把 HBM 需求线性外推。",
            ["Qualcomm", "AI200", "AI250", "HBC"],
            ["近存计算", "HBC", "AI推理加速器", "HBM替代路线"],
            ["SUBSTITUTES", "CONSTRAINED_BY"],
            "SPECULATIVE",
            "官方路线存在，但对 HBM 替代强度和商业化节奏仍待后续验证。",
        ),
        _ev(
            "ev_cpo_broadcom_cpo_integration",
            "CPO/硅光/光互连",
            "src_cpo_broadcom_cpo",
            "company_website",
            "Highest Power Efficiency and Bandwidth Density - Broadcom CPO",
            "https://www.broadcom.com/info/optics/cpo",
            "",
            "Broadcom 官方 CPO 页面将 CPO 描述为把光学和硅集成在同一封装基板上的互连方案。",
            "CPO 路线应拆成 switch ASIC、光引擎、封装基板、光耦合和测试等节点。",
            ["Broadcom", "CPO", "optical engine", "silicon"],
            ["CPO", "光引擎", "交换芯片", "封装基板"],
            ["PART_OF", "REQUIRES", "PRODUCES"],
            "LIKELY",
            "官方产品/技术页面，可支持 CPO 工艺树和产品 claim。",
        ),
        _ev(
            "ev_cpo_openlight_pic_laser_amp",
            "CPO/硅光/光互连",
            "src_cpo_openlight_silicon_photonics",
            "company_website",
            "OpenLight Technology",
            "https://openlightphotonics.com/technology",
            "",
            "OpenLight 官方技术页说明其硅光平台集成激光器、调制器、探测器和光放大器等组件。",
            "硅光链条需要覆盖光产生、调制、放大、接收与电信号接口，不应只写 CPO 概念。",
            ["OpenLight", "PIC", "integrated laser", "optical amplifier"],
            ["PIC", "集成激光器", "光放大器", "光I/O"],
            ["REQUIRES", "PART_OF", "PRODUCES"],
            "LIKELY",
            "官方技术资料，适合抽取器件链节点；客户和收入需另查。",
        ),
        _ev(
            "ev_cpo_coherent_1_6t_transceiver",
            "CPO/硅光/光互连",
            "src_cpo_coherent_1_6t",
            "company_website",
            "Silicon Photonics Based 1.6T Transceiver Modules",
            "https://www.coherent.com/news/press-releases/silicon-photonics-based-transceiver-modules",
            "2025-03-31",
            "Coherent 官方新闻稿说明 1.6T-DR8 硅光收发模块，使用 Marvell Ara 3nm 光 DSP，并具备 200Gbps 电/光接口。",
            "1.6T 光模块可作为硅光收发器产品节点，DSP 与高速接口是关键上游节点。",
            ["Coherent", "Marvell", "1.6T-DR8", "optical DSP"],
            ["1.6T光模块", "硅光收发器", "DSP", "200G接口"],
            ["PRODUCES", "REQUIRES", "SUPPLIES_TO"],
            "LIKELY",
            "官方新闻稿，产品展示强度较高；量产规模和客户仍需验证。",
        ),
        _ev(
            "ev_cpo_arista_xpo_challenge",
            "CPO/硅光/光互连",
            "src_cpo_arista_xpo_whitepaper",
            "technical_doc",
            "XPO: Redefining Pluggable Optics for AI Networking",
            "https://www.arista.com/assets/data/pdf/Whitepapers/XPO-Whitepaper.pdf",
            "2026",
            "Arista 白皮书提出 CPO 在现场可维护、可扩展和可制造性方面有部署挑战，并提出 XPO 插拔路线。",
            "XPO/LPO 等插拔方案是 CPO 的替代分支，CPO 路径需要主动标记部署风险。",
            ["Arista", "XPO", "CPO", "pluggable optics"],
            ["XPO", "CPO", "可维护性", "可制造性"],
            ["SUBSTITUTES", "CONSTRAINED_BY"],
            "SPECULATIVE",
            "技术白皮书可作为反证候选，但来自替代路线推动方，需交叉验证。",
        ),
        _ev(
            "ev_800v_nvidia_architecture",
            "800V HVDC/数据中心电力",
            "src_800v_nvidia_architecture",
            "technical_doc",
            "800 VDC Architecture for AI Data Centers",
            "https://www.nvidia.com/en-us/data-center/technologies/800-vdc-architecture/",
            "2025",
            "NVIDIA 官方页面说明数据中心从多级 AC 转换向集中 AC-to-800VDC 与机架直流分配演进。",
            "800V HVDC 主题应拆成集中整流、直流母线、机架 DC/DC、保护器件和供电可靠性节点。",
            ["NVIDIA", "800 VDC", "AI data center"],
            ["800V HVDC", "集中整流", "直流母线", "AI数据中心供电"],
            ["PART_OF", "REQUIRES", "CONSTRAINED_BY"],
            "LIKELY",
            "官方技术页面，能支持架构假设；实际项目落地需客户和交付证据。",
        ),
        _ev(
            "ev_800v_nvidia_kyber_converter",
            "800V HVDC/数据中心电力",
            "src_800v_nvidia_ecosystem",
            "technical_doc",
            "Building the 800 VDC Ecosystem for Efficient, Scalable AI Factories",
            "https://developer.nvidia.com/blog/building-the-800-vdc-ecosystem-for-efficient-scalable-ai-factories/",
            "2025",
            "NVIDIA 技术博客说明 Kyber 机架拟采用 800 VDC，并在 GPU 附近通过高变比 LLC 转换到低压。",
            "机架级 DC/DC 与高变比 LLC 转换器是 800V 数据中心电力链的关键工艺/设备节点。",
            ["NVIDIA", "Kyber", "LLC converter", "800 VDC"],
            ["Kyber机架", "64:1 LLC转换器", "DC/DC", "GPU供电"],
            ["REQUIRES", "PART_OF", "BOTTLENECK_OF"],
            "LIKELY",
            "官方技术博客，节点方向明确；供应商映射需另证。",
        ),
        _ev(
            "ev_800v_flex_power_shelf",
            "800V HVDC/数据中心电力",
            "src_800v_flex_power_shelf",
            "company_website",
            "Advancing the transition to 800 VDC data centers with NVIDIA",
            "https://flex.com/resources/advancing-the-transition-to-800-vdc-data-centers-with-nvidia",
            "2025",
            "Flex 说明与 NVIDIA 合作的高密度 DC power shelf，用于 GB300 NVL72，强调效率和转换损耗改善。",
            "Power shelf 可作为 800V 数据中心电力设备候选节点，Flex 是非 A 股参考主体。",
            ["Flex", "NVIDIA", "GB300 NVL72", "DC power shelf"],
            ["Power shelf", "800VDC", "GB300 NVL72", "电源效率"],
            ["PRODUCES", "SUPPLIES_TO", "REQUIRES"],
            "LIKELY",
            "公司官方资料，但需要合同、收入和客户交付证据才能进入图谱事实。",
        ),
        _ev(
            "ev_800v_ti_power_conversion",
            "800V HVDC/数据中心电力",
            "src_800v_ti_datacenter_power",
            "technical_doc",
            "Powering data centers for the AI era",
            "https://www.ti.com/document-viewer/lit/html/SSZT083",
            "2025",
            "TI 技术文档讨论 AI 数据中心电源转换效率、功率密度和配电架构演进。",
            "800V/高压直流路径需要拆到电源转换、功率密度、保护和认证约束，不应只写需求增长。",
            ["Texas Instruments", "AI data center", "power conversion", "48V"],
            ["电源转换", "功率密度", "高压配电", "数据中心电力"],
            ["CONSTRAINED_BY", "REQUIRES", "BOTTLENECK_OF"],
            "LIKELY",
            "可访问技术文档，可支持约束节点；800V 直接采用范围仍需项目级证据。",
        ),
    ]


def _claims() -> list[dict[str, Any]]:
    return [
        _claim("claim_hbm_skhynix_tsv_requires", "HBM", "TSV/WLP", "PROCESS", "REQUIRES", "HBM", "PRODUCT", ["HBM", "DRAM堆叠", "TSV/WLP"], ["ev_hbm_skhynix_tsv_wlp"], [], "LIKELY", "官方资料支持 HBM 早期路线依赖 TSV/WLP。", ["缺少具体设备和材料供应商映射"], "reference_only", "000660.KS", "SK hynix"),
        _claim("claim_hbm_nvidia_blackwell_requires_hbm3e", "HBM", "Blackwell AI加速器", "PRODUCT", "REQUIRES", "HBM3E", "PRODUCT", ["HBM", "Blackwell", "HBM3E", "AI加速器内存子系统"], ["ev_hbm_nvidia_blackwell_hbm3e"], [], "LIKELY", "NVIDIA 官方架构页支持 Blackwell 与 HBM3E 内存子系统关系。", ["缺少 HBM 供应商份额", "缺少封装产能分配和订单证据"], "reference_only", "NVDA", "NVIDIA"),
        _claim("claim_hbm_micron_produces_hbm3e", "HBM", "Micron", "COMPANY", "PRODUCES", "HBM3E", "PRODUCT", ["HBM", "HBM3E", "Micron"], ["ev_hbm_micron_hbm3e_product"], [], "LIKELY", "Micron 官方产品页证明其 HBM3E 产品存在。", ["缺少客户关系和供应份额验证"], "reference_only", "MU", "Micron"),
        _claim("claim_hbm_hbc_substitution_risk", "HBM", "HBC近存计算路线", "TECH_ROUTE", "SUBSTITUTES", "HBM路线", "TECH_ROUTE", ["HBM", "AI推理内存瓶颈", "HBC近存计算"], ["ev_hbm_qualcomm_hbc_substitution"], [], "SPECULATIVE", "Qualcomm 官方路线提示替代分支，但商业替代强度未验证。", ["缺少独立性能对比", "缺少量产和客户导入证据"], "reference_only", "QCOM", "Qualcomm"),
        _claim("claim_cpo_broadcom_cpo_requires_optical_engine", "CPO/硅光/光互连", "CPO交换封装", "PROCESS", "REQUIRES", "光引擎", "PRODUCT", ["CPO", "交换芯片", "光引擎", "封装基板"], ["ev_cpo_broadcom_cpo_integration"], [], "LIKELY", "Broadcom 官方 CPO 页面支持光学与硅的共封装关系。", ["缺少良率和供应商证据"], "reference_only", "AVGO", "Broadcom"),
        _claim("claim_cpo_openlight_pic_requires_laser_amp", "CPO/硅光/光互连", "OpenLight硅光PIC", "PRODUCT", "REQUIRES", "集成激光器/光放大器", "EQUIPMENT", ["硅光", "PIC", "集成激光器", "光放大器", "光I/O"], ["ev_cpo_openlight_pic_laser_amp"], [], "LIKELY", "OpenLight 官方技术页支持硅光 PIC 与集成激光器/光放大器关系。", ["缺少中国公司映射", "缺少成本结构验证"], "reference_only", "", "OpenLight"),
        _claim("claim_cpo_coherent_produces_1_6t", "CPO/硅光/光互连", "Coherent", "COMPANY", "PRODUCES", "1.6T硅光收发模块", "PRODUCT", ["硅光", "1.6T光模块", "DSP", "Coherent"], ["ev_cpo_coherent_1_6t_transceiver"], [], "LIKELY", "Coherent 官方新闻稿支持其 1.6T 硅光模块展示。", ["缺少量产规模", "缺少客户订单"], "reference_only", "COHR", "Coherent"),
        _claim("claim_cpo_xpo_substitution_risk", "CPO/硅光/光互连", "XPO插拔光模块路线", "TECH_ROUTE", "SUBSTITUTES", "CPO路线", "TECH_ROUTE", ["CPO", "可维护性约束", "XPO插拔路线"], ["ev_cpo_arista_xpo_challenge"], [], "SPECULATIVE", "Arista 白皮书提示 XPO 作为 CPO 部署约束下的替代路线。", ["需要第三方验证 Arista 观点", "需要比较 TCO 与部署周期"], "reference_only", "ANET", "Arista"),
        _claim("claim_800v_nvidia_arch_requires_busbar", "800V HVDC/数据中心电力", "800VDC数据中心架构", "TECH_ROUTE", "REQUIRES", "直流母线/保护器件", "EQUIPMENT", ["800V HVDC", "集中整流", "直流母线", "保护器件"], ["ev_800v_nvidia_architecture"], [], "LIKELY", "NVIDIA 官方架构支持 800VDC 向机架分配的设备链条。", ["缺少具体供应商份额", "缺少认证周期"], "reference_only", "NVDA", "NVIDIA"),
        _claim("claim_800v_nvidia_kyber_requires_llc", "800V HVDC/数据中心电力", "Kyber机架供电", "PROCESS", "REQUIRES", "高变比LLC转换器", "EQUIPMENT", ["800V HVDC", "Kyber机架", "DC/DC", "LLC转换器"], ["ev_800v_nvidia_kyber_converter"], [], "LIKELY", "NVIDIA 技术博客支持机架内高变比 DC/DC 转换节点。", ["缺少供应商清单", "缺少可靠性和热设计验证"], "reference_only", "NVDA", "NVIDIA"),
        _claim("claim_800v_flex_produces_power_shelf", "800V HVDC/数据中心电力", "Flex", "COMPANY", "PRODUCES", "DC power shelf", "EQUIPMENT", ["800V HVDC", "DC power shelf", "GB300 NVL72", "Flex"], ["ev_800v_flex_power_shelf"], [], "LIKELY", "Flex 官方资料支持其高密度 DC power shelf 候选设备关系。", ["缺少正式合同文本", "缺少收入占比"], "reference_only", "FLEX", "Flex"),
        _claim("claim_800v_ti_constrained_by_conversion", "800V HVDC/数据中心电力", "数据中心高压配电", "TECH_ROUTE", "CONSTRAINED_BY", "电源转换效率/功率密度", "CONSTRAINT", ["800V HVDC", "电源转换", "功率密度", "认证"], ["ev_800v_ti_power_conversion"], [], "LIKELY", "TI 技术文档提示 AI 数据中心电力链的转换效率和功率密度约束。", ["缺少 800V 直接落地项目", "缺少认证周期"], "reference_only", "TXN", "Texas Instruments"),
    ]


def _counter_evidence() -> list[dict[str, Any]]:
    return [
        _counter("counter_hbm_hbc_substitution", "HBM", "claim_hbm_nvidia_blackwell_requires_hbm3e", "src_hbm_qualcomm_ai200_ai250", "substitution_risk", "Qualcomm AI250/HBC 路线可能在部分 AI 推理场景绕开 HBM 路径。", "HBC 是 HBM 路线的替代分支候选，应降低对 HBM 需求线性外推的置信度。", "medium", "keep_but_flag"),
        _counter("counter_cpo_xpo_deployment", "CPO/硅光/光互连", "claim_cpo_broadcom_cpo_requires_optical_engine", "src_cpo_arista_xpo_whitepaper", "substitution_risk", "Arista 白皮书强调 CPO 部署存在现场可维护、扩展和制造挑战，并提出 XPO。", "CPO 不是唯一主线，XPO/LPO 可能在部分数据中心先落地。", "medium", "downgrade_to_speculative"),
        _counter("counter_800v_ecosystem_maturity", "800V HVDC/数据中心电力", "claim_800v_nvidia_arch_requires_busbar", "src_800v_nvidia_ecosystem", "timing_mismatch", "NVIDIA 生态文章说明 800 VDC 将分阶段演进，需要组件生态成熟。", "800VDC 不是当前所有数据中心立即采用的事实，应标记时间和生态成熟度约束。", "medium", "keep_but_flag"),
    ]


def _hypotheses() -> list[dict[str, Any]]:
    return [
        _hyp("hyp_hbm_blackwell_memory_path", "HBM", [{"name": "HBM", "type": "THEME"}, {"name": "HBM3E", "type": "PRODUCT"}, {"name": "Blackwell AI加速器", "type": "PRODUCT"}, {"name": "NVIDIA", "type": "COMPANY"}], ["claim_hbm_nvidia_blackwell_requires_hbm3e"], ["ev_hbm_nvidia_blackwell_hbm3e"], ["counter_hbm_hbc_substitution"], "LIKELY", "medium", "HBM3E 是高性能 AI 加速器内存瓶颈之一，但存在近存计算替代路线。"),
        _hyp("hyp_hbm_company_product_reference", "HBM", [{"name": "HBM", "type": "THEME"}, {"name": "HBM3E", "type": "PRODUCT"}, {"name": "Micron", "type": "COMPANY"}], ["claim_hbm_micron_produces_hbm3e"], ["ev_hbm_micron_hbm3e_product"], [], "LIKELY", "medium", "产品证据强，但非 A 股主板主体，只可作为产业参考。"),
        _hyp("hyp_cpo_pic_optical_io", "CPO/硅光/光互连", [{"name": "硅光", "type": "THEME"}, {"name": "PIC", "type": "PRODUCT"}, {"name": "集成激光器/光放大器", "type": "EQUIPMENT"}, {"name": "OpenLight", "type": "COMPANY"}], ["claim_cpo_openlight_pic_requires_laser_amp"], ["ev_cpo_openlight_pic_laser_amp"], [], "LIKELY", "specific", "硅光的光产生、光调制、放大和电接口集成是高密度光互连关键瓶颈。"),
        _hyp("hyp_cpo_cpo_vs_xpo", "CPO/硅光/光互连", [{"name": "CPO", "type": "THEME"}, {"name": "光引擎", "type": "PRODUCT"}, {"name": "可维护性/可制造性", "type": "CONSTRAINT"}, {"name": "XPO插拔路线", "type": "TECH_ROUTE"}], ["claim_cpo_broadcom_cpo_requires_optical_engine", "claim_cpo_xpo_substitution_risk"], ["ev_cpo_broadcom_cpo_integration", "ev_cpo_arista_xpo_challenge"], ["counter_cpo_xpo_deployment"], "CONFLICTED", "medium", "CPO 具备功耗和密度优势，但部署约束可能使 XPO/LPO 成为阶段性替代。"),
        _hyp("hyp_800v_rack_power_chain", "800V HVDC/数据中心电力", [{"name": "800V HVDC", "type": "THEME"}, {"name": "集中整流", "type": "PROCESS"}, {"name": "直流母线/保护器件", "type": "EQUIPMENT"}, {"name": "Kyber机架DC/DC", "type": "EQUIPMENT"}], ["claim_800v_nvidia_arch_requires_busbar", "claim_800v_nvidia_kyber_requires_llc"], ["ev_800v_nvidia_architecture", "ev_800v_nvidia_kyber_converter"], ["counter_800v_ecosystem_maturity"], "LIKELY", "specific", "高压直流分配、保护断路和机架级高变比转换是主要瓶颈。"),
        _hyp("hyp_800v_power_shelf_reference", "800V HVDC/数据中心电力", [{"name": "800V HVDC", "type": "THEME"}, {"name": "DC power shelf", "type": "EQUIPMENT"}, {"name": "GB300 NVL72", "type": "PRODUCT"}, {"name": "Flex", "type": "COMPANY"}], ["claim_800v_flex_produces_power_shelf", "claim_800v_ti_constrained_by_conversion"], ["ev_800v_flex_power_shelf", "ev_800v_ti_power_conversion"], [], "LIKELY", "medium", "公司官方资料支持设备节点，但缺少收入、合同、认证周期和主板映射。"),
    ]


def _ev(
    evidence_id: str,
    topic: str,
    source_id: str,
    source_type: str,
    title: str,
    url: str,
    publish_date: str,
    extracted_text: str,
    summary: str,
    mentioned_entities: list[str],
    mentioned_chain_nodes: list[str],
    possible_relation_types: list[str],
    evidence_strength_hint: str,
    why_strength: str,
) -> dict[str, Any]:
    return {
        "evidence_id": evidence_id,
        "topic": topic,
        "source_id": source_id,
        "source_type": source_type,
        "title": title,
        "url": url,
        "publish_date": publish_date,
        "extracted_text": extracted_text,
        "summary": summary,
        "mentioned_entities": mentioned_entities,
        "mentioned_chain_nodes": mentioned_chain_nodes,
        "possible_relation_types": possible_relation_types,
        "evidence_strength_hint": evidence_strength_hint,
        "why_strength": why_strength,
        "needs_human_review": True,
    }


def _claim(
    claim_id: str,
    topic: str,
    subject_name: str,
    subject_type: str,
    relation: str,
    object_name: str,
    object_type: str,
    path_hint: list[str],
    supporting_evidence_ids: list[str],
    counter_evidence_ids: list[str],
    confidence: str,
    reason: str,
    missing_verification: list[str],
    main_board_relevance: str,
    stock_code: str,
    stock_name: str,
) -> dict[str, Any]:
    return {
        "claim_id": claim_id,
        "topic": topic,
        "subject": {"name": subject_name, "type": subject_type},
        "relation": relation,
        "object": {"name": object_name, "type": object_type},
        "path_hint": path_hint,
        "supporting_evidence_ids": supporting_evidence_ids,
        "counter_evidence_ids": counter_evidence_ids,
        "suggested_confidence": confidence,
        "suggested_status": "CANDIDATE",
        "reason": reason,
        "missing_verification": missing_verification,
        "main_board_relevance": main_board_relevance,
        "stock_code": stock_code,
        "stock_name": stock_name,
    }


def _counter(
    counter_id: str,
    topic: str,
    target_claim_id: str,
    source_id: str,
    counter_type: str,
    text: str,
    summary: str,
    severity: str,
    suggested_action: str,
) -> dict[str, Any]:
    return {
        "counter_id": counter_id,
        "topic": topic,
        "target_claim_id": target_claim_id,
        "source_id": source_id,
        "counter_type": counter_type,
        "text": text,
        "summary": summary,
        "severity": severity,
        "suggested_action": suggested_action,
    }


def _hyp(
    hypothesis_id: str,
    topic: str,
    benefit_path: list[dict[str, str]],
    claim_ids: list[str],
    supporting_evidence_ids: list[str],
    counter_evidence_ids: list[str],
    confidence: str,
    specificity: str,
    bottleneck_reason: str,
) -> dict[str, Any]:
    return {
        "hypothesis_id": hypothesis_id,
        "topic": topic,
        "benefit_path": benefit_path,
        "claim_ids": claim_ids,
        "supporting_evidence_ids": supporting_evidence_ids,
        "counter_evidence_ids": counter_evidence_ids,
        "suggested_confidence": confidence,
        "suggested_status": "CANDIDATE",
        "path_specificity": specificity,
        "bottleneck_reason": bottleneck_reason,
        "verification_questions": [
            "是否存在官方公告、年报或合同证明该关系？",
            "相关业务收入占比和客户验证进度是多少？",
            "是否存在公司否认、低收入占比、路线替代或交付延期反证？",
        ],
        "do_not_activate_reason": "仍需官方证据、收入占比、客户关系和反证复核；本条仅为候选假设路径。",
    }


def _write_progress(run_dir: Path, stage: str, topic: str, manifest: list[dict[str, Any]], evidence: list[dict[str, Any]], claims: list[dict[str, Any]], counters: list[dict[str, Any]], failure: str, next_step: str) -> None:
    lines = [
        f"## {_now()}",
        f"- 当前阶段: {stage}",
        f"- 当前主题: {topic}",
        f"- 已访问来源数量: {len(manifest)}",
        f"- 已保留证据数量: {len(evidence)}",
        f"- 已抽取 claim 数量: {len(claims)}",
        f"- 已发现反证数量: {len(counters)}",
        f"- 当前失败原因: {failure or '无'}",
        f"- 下一步: {next_step}",
        "",
    ]
    with (run_dir / "progress_log.md").open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines))


def _write_reports(run_dir: Path, manifest: list[dict[str, Any]], evidence: list[dict[str, Any]], claims: list[dict[str, Any]], counters: list[dict[str, Any]], hypotheses: list[dict[str, Any]], elapsed: float) -> None:
    by_topic = {topic: {
        "evidence": [row for row in evidence if row["topic"] == topic],
        "claims": [row for row in claims if row["topic"] == topic],
        "counters": [row for row in counters if row["topic"] == topic],
        "hypotheses": [row for row in hypotheses if row["topic"] == topic],
    } for topic in TOPICS}

    gap_lines = ["# Topic Gap Report", "", "本报告只记录候选证据和待验证关系，不提供买卖建议。", ""]
    for topic, rows in by_topic.items():
        gap_lines.extend([
            f"## {topic}",
            "",
            "### 1. 已发现的高价值节点",
            "- " + "、".join(sorted({node for ev in rows["evidence"] for node in ev["mentioned_chain_nodes"]})) if rows["evidence"] else "- 暂无",
            "",
            "### 2. 证据最强的路径",
        ])
        for hyp in rows["hypotheses"]:
            if hyp["suggested_confidence"] in {"LIKELY", "PROVEN"}:
                gap_lines.append(f"- {hyp['hypothesis_id']}: " + " -> ".join(item["name"] for item in hyp["benefit_path"]))
        gap_lines.extend([
            "",
            "### 3. 证据最弱但潜在价值高的路径",
        ])
        weak = [hyp for hyp in rows["hypotheses"] if hyp["suggested_confidence"] in {"SPECULATIVE", "CONFLICTED"}]
        gap_lines.extend([f"- {hyp['hypothesis_id']}: {hyp['do_not_activate_reason']}" for hyp in weak] or ["- 暂无，当前弱项主要体现在公司映射缺口。"])
        gap_lines.extend([
            "",
            "### 4. 缺少公司映射的瓶颈节点",
            "- " + "、".join(sorted({claim["object"]["name"] for claim in rows["claims"] if claim["object"]["type"] in {"EQUIPMENT", "CONSTRAINT", "MATERIAL"}})),
            "",
            "### 5. 缺少官方证据的公司关系",
        ])
        company_claims = [claim for claim in rows["claims"] if claim["subject"]["type"] == "COMPANY"]
        gap_lines.extend([f"- {claim['claim_id']}: {claim['subject']['name']} {claim['relation']} {claim['object']['name']}；缺口：{'、'.join(claim['missing_verification'])}" for claim in company_claims] or ["- 暂无公司关系 claim。"])
        gap_lines.extend([
            "",
            "### 6. 存在冲突证据的关系",
        ])
        gap_lines.extend([f"- {counter['target_claim_id']}: {counter['summary']}" for counter in rows["counters"]] or ["- 暂无。"])
        gap_lines.extend([
            "",
            "### 7. 需要人工复核的问题",
            "- 所有公司关系都需要公告、年报、合同、客户验证或收入占比复核。",
            "- 所有替代路线需要独立第三方或客户导入证据复核。",
            "",
            "### 8. 下一轮 Evidence Miner query list",
        ])
        for claim in rows["claims"]:
            gap_lines.append(f"- \"{claim['subject']['name']}\" \"{claim['object']['name']}\" 年报 公告 客户 验证 收入占比")
        gap_lines.append("")

    (run_dir / "topic_gap_report.md").write_text("\n".join(gap_lines), encoding="utf-8")

    dist = Counter(claim["suggested_confidence"] for claim in claims)
    main_board = Counter(claim["main_board_relevance"] for claim in claims)
    quality_lines = [
        "# Quality Report",
        "",
        f"- 运行命令: python scripts/run_overnight_evidence_corpus.py",
        f"- 本次运行耗时: {elapsed:.2f} 秒",
        f"- 总来源数: {len(manifest)}",
        f"- 成功来源数: {sum(1 for item in manifest if item['status'] == 'success')}",
        f"- 失败来源数: {sum(1 for item in manifest if item['status'] == 'failed')}",
        f"- raw evidence 数: {len(evidence)}",
        f"- candidate claim 数: {len(claims)}",
        f"- counter evidence 数: {len(counters)}",
        f"- graph hypothesis 数: {len(hypotheses)}",
        f"- PROVEN / LIKELY / SPECULATIVE / DISPROVEN / CONFLICTED 分布: {dict(dist)}",
        f"- 主板 eligible 数量: {main_board.get('eligible', 0)}",
        f"- reference_only 数量: {main_board.get('reference_only', 0)}",
        f"- 无法确认股票代码数量: {sum(1 for claim in claims if not claim['stock_code'])}",
        f"- 高优先级 gap 数量: {sum(len(claim['missing_verification']) for claim in claims)}",
        "",
        "## 不能使用的结论",
        "- 不能把任何 candidate claim 当作 ACTIVE 图谱事实。",
        "- 不能把本目录中的公司字段当作推荐股票列表。",
        "- 不能把概念标签或单一技术博客当作公司受益证据。",
        "",
        "## 可能过期的证据",
        "- 所有 2025-2026 技术路线和生态合作信息都需要在进入 Evidence Miner 前按最新公告复核。",
        "",
        "## 来源质量低或存在偏向的来源",
        "- Arista XPO 白皮书来自替代路线推动方，可作反证候选，不可单独否定 CPO。",
        "- 公司官网和新闻稿偏向产品宣传，需要公告、合同、客户或年报交叉验证。",
        "",
        "## 需要人工复核的 claim",
    ]
    quality_lines.extend([f"- {claim['claim_id']}: {'、'.join(claim['missing_verification'])}" for claim in claims])
    quality_lines.extend([
        "",
        "## 高置信度风险",
        "- 本轮没有 PROVEN claim；所有 LIKELY 仍保持 CANDIDATE，未写入 ACTIVE。",
        "- 最高置信度来自官方产品/技术资料，但只证明技术或产品节点，不证明 A 股公司受益。",
    ])
    (run_dir / "quality_report.md").write_text("\n".join(quality_lines), encoding="utf-8")


def _write_alias_map(run_dir: Path) -> None:
    alias_map = {
        "SK hynix": {"aliases": ["SK海力士", "000660.KS"], "main_board_relevance": "reference_only"},
        "TSMC": {"aliases": ["台积电", "Taiwan Semiconductor", "2330.TW"], "main_board_relevance": "reference_only"},
        "Micron": {"aliases": ["美光", "MU"], "main_board_relevance": "reference_only"},
        "Qualcomm": {"aliases": ["高通", "QCOM"], "main_board_relevance": "reference_only"},
        "Broadcom": {"aliases": ["博通", "AVGO"], "main_board_relevance": "reference_only"},
        "Intel": {"aliases": ["英特尔", "INTC"], "main_board_relevance": "reference_only"},
        "OpenLight": {"aliases": ["OpenLight Photonics"], "main_board_relevance": "reference_only"},
        "Coherent": {"aliases": ["COHR"], "main_board_relevance": "reference_only"},
        "Arista": {"aliases": ["ANET"], "main_board_relevance": "reference_only"},
        "NVIDIA": {"aliases": ["英伟达", "NVDA"], "main_board_relevance": "reference_only"},
        "Flex": {"aliases": ["FLEX"], "main_board_relevance": "reference_only"},
        "Eaton": {"aliases": ["ETN"], "main_board_relevance": "reference_only"},
        "Texas Instruments": {"aliases": ["TI", "TXN"], "main_board_relevance": "reference_only"},
    }
    (run_dir / "company_alias_map.json").write_text(json.dumps(alias_map, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def build_run(output_root: Path, run_id: str | None = None) -> Path:
    start = time.perf_counter()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = output_root / (run_id or f"overnight_evidence_{timestamp}")
    run_dir.mkdir(parents=True, exist_ok=True)

    manifest = _manifest()
    failed = [row for row in manifest if row["status"] == "failed"]
    evidence = _evidence()
    claims = _claims()
    counters = _counter_evidence()
    hypotheses = _hypotheses()

    (run_dir / "progress_log.md").write_text("# Progress Log\n\n", encoding="utf-8")
    stages = [
        ("[1/8] 初始化主题和关键词", "全部主题", "", "收集公开来源"),
        ("[2/8] 收集公开来源", "全部主题", "", "去重和来源归档"),
        ("[3/8] 去重和来源归档", "全部主题", "", "抽取证据片段"),
        ("[4/8] 抽取证据片段", "全部主题", "", "抽取 candidate claims"),
        ("[5/8] 抽取 candidate claims", "全部主题", "", "搜索反证和冲突证据"),
        ("[6/8] 搜索反证和冲突证据", "全部主题", "部分论文或旧 URL 未能抓取，已记录 failed_sources。", "生成 graph hypotheses"),
        ("[7/8] 生成 graph hypotheses", "全部主题", "", "生成质量报告和缺口报告"),
        ("[8/8] 生成质量报告和缺口报告", "全部主题", "", "运行 schema validator"),
    ]
    for stage, topic, failure, next_step in stages:
        _write_progress(run_dir, stage, topic, manifest, evidence, claims, counters, failure, next_step)

    (run_dir / "source_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    write_jsonl(run_dir / "raw_evidence.jsonl", evidence)
    write_jsonl(run_dir / "candidate_claims.jsonl", claims)
    write_jsonl(run_dir / "counter_evidence_candidates.jsonl", counters)
    write_jsonl(run_dir / "graph_hypotheses.jsonl", hypotheses)
    write_jsonl(run_dir / "failed_sources.jsonl", failed)
    _write_alias_map(run_dir)
    elapsed = time.perf_counter() - start
    _write_reports(run_dir, manifest, evidence, claims, counters, hypotheses, elapsed)
    validation = validate_run_directory(run_dir)
    (run_dir / "validation_summary.json").write_text(json.dumps(validation, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return run_dir


def main() -> int:
    parser = argparse.ArgumentParser(description="Build overnight evidence corpus run artifacts.")
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--run-id", default="")
    args = parser.parse_args()

    run_dir = build_run(args.output_root, args.run_id or None)
    print(run_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
