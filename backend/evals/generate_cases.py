"""冻结黄金集生成器。模板对话、无 LLM；assign_04 代词 case 单独入库不走本模板。"""

from __future__ import annotations

import json
from pathlib import Path

GOLDEN_DIR = Path(__file__).resolve().parent / "golden"

CASES: list[dict] = [
    {
        "id": "single_01",
        "source_text": (
            "会议目标：Q1 发布冲刺\n参会人：张伟、李娜\n"
            "张伟负责完成用户认证模块重构，下周五前提交代码评审。"
        ),
        "expect": {
            "needs_clarify": False,
            "needs_assignment": False,
            "items": [
                {
                    "title_keywords": ["认证"],
                    "primary_owner": "张伟",
                    "secondary_owners": [],
                }
            ],
        },
        "gold": {"clarify": "", "assignments": []},
    },
    {
        "id": "single_02",
        "source_text": (
            "会议目标：文档同步\n参会人：李娜、王强\n"
            "李娜跟进 API 文档更新，本周内完成并发布。"
        ),
        "expect": {
            "needs_clarify": False,
            "needs_assignment": False,
            "items": [
                {
                    "title_keywords": ["文档"],
                    "primary_owner": "李娜",
                    "secondary_owners": [],
                }
            ],
        },
        "gold": {"clarify": "", "assignments": []},
    },
    {
        "id": "single_03",
        "source_text": (
            "会议目标：预发部署\n参会人：王强、张伟\n"
            "王强安排预发环境部署测试，确认本月底上线。"
        ),
        "expect": {
            "needs_clarify": False,
            "needs_assignment": False,
            "items": [
                {
                    "title_keywords": ["部署"],
                    "primary_owner": "王强",
                    "secondary_owners": [],
                }
            ],
        },
        "gold": {"clarify": "", "assignments": []},
    },
    {
        "id": "single_04",
        "source_text": (
            "会议目标：监控补齐\n参会人：邓凯、孙磊\n"
            "邓凯负责完成告警规则修复，本周五前提交。"
        ),
        "expect": {
            "needs_clarify": False,
            "needs_assignment": False,
            "items": [
                {
                    "title_keywords": ["告警"],
                    "primary_owner": "邓凯",
                    "secondary_owners": [],
                }
            ],
        },
        "gold": {"clarify": "", "assignments": []},
    },
    {
        "id": "multi_01",
        "source_text": (
            "会议目标：新产品上线\n参会人：周婷、孙磊、邓凯、马超\n"
            "新产品上线工作由周婷统筹，孙磊、邓凯和马超配合，截止时间为2026年8月31日。"
        ),
        "expect": {
            "needs_clarify": False,
            "needs_assignment": False,
            "items": [
                {
                    "title_keywords": ["上线"],
                    "primary_owner": "周婷",
                    "secondary_owners": ["孙磊", "邓凯", "马超"],
                }
            ],
        },
        "gold": {"clarify": "", "assignments": []},
    },
    {
        "id": "multi_02",
        "source_text": (
            "会议目标：事故复盘整改\n参会人：张伟、李娜\n"
            "根因报告由张伟牵头，李娜协助整理时间线并跟进通报。"
        ),
        "expect": {
            "needs_clarify": False,
            "needs_assignment": False,
            "items": [
                {
                    "title_keywords": ["根因"],
                    "primary_owner": "张伟",
                    "secondary_owners": ["李娜"],
                }
            ],
        },
        "gold": {"clarify": "", "assignments": []},
    },
    {
        "id": "multi_03",
        "source_text": (
            "会议目标：支付修复联调\n参会人：孙磊、邓凯\n"
            "支付修复由孙磊负责开发，邓凯配合回归测试，本周完成。"
        ),
        "expect": {
            "needs_clarify": False,
            "needs_assignment": False,
            "items": [
                {
                    "title_keywords": ["支付"],
                    "primary_owner": "孙磊",
                    "secondary_owners": ["邓凯"],
                }
            ],
        },
        "gold": {"clarify": "", "assignments": []},
    },
    {
        "id": "assign_01",
        "source_text": (
            "会议目标：发布准备\n参会人：张伟、李娜\n"
            "认证模块这周要完成代码评审并提交。"
        ),
        "expect": {
            "needs_clarify": False,
            "needs_assignment": True,
            "items": [
                {
                    "title_keywords": ["认证"],
                    "primary_owner": "张伟",
                    "secondary_owners": [],
                }
            ],
        },
        "gold": {
            "clarify": "",
            "assignments": [
                {
                    "title_keywords": ["认证"],
                    "primary_owner": "张伟",
                    "secondary_owners": [],
                }
            ],
        },
    },
    {
        "id": "assign_02",
        "source_text": (
            "会议目标：文档补齐\n参会人：李娜、王强\n"
            "接口说明本周内需要完成更新并发布到内部 Wiki。"
        ),
        "expect": {
            "needs_clarify": False,
            "needs_assignment": True,
            "items": [
                {
                    "title_keywords": ["更新"],
                    "primary_owner": "李娜",
                    "secondary_owners": [],
                }
            ],
        },
        "gold": {
            "clarify": "",
            "assignments": [
                {
                    "title_keywords": ["更新"],
                    "primary_owner": "李娜",
                    "secondary_owners": [],
                }
            ],
        },
    },
    {
        "id": "assign_03",
        "source_text": (
            "会议目标：预发验证\n参会人：王强、张伟\n"
            "预发环境部署测试这周要安排上线确认。"
        ),
        "expect": {
            "needs_clarify": False,
            "needs_assignment": True,
            "items": [
                {
                    "title_keywords": ["部署"],
                    "primary_owner": "王强",
                    "secondary_owners": ["张伟"],
                }
            ],
        },
        "gold": {
            "clarify": "",
            "assignments": [
                {
                    "title_keywords": ["部署"],
                    "primary_owner": "王强",
                    "secondary_owners": ["张伟"],
                }
            ],
        },
    },
    {
        "id": "clarify_01",
        "source_text": "参会人：张三\n张三负责完成部署测试。",
        "expect": {
            "needs_clarify": True,
            "needs_assignment": False,
            "items": [
                {
                    "title_keywords": ["部署"],
                    "primary_owner": "张三",
                    "secondary_owners": [],
                }
            ],
        },
        "gold": {"clarify": "明确部署目标", "assignments": []},
    },
    {
        "id": "clarify_02",
        "source_text": "参会人：李娜\n李娜跟进监控看板修复，本周完成。",
        "expect": {
            "needs_clarify": True,
            "needs_assignment": False,
            "items": [
                {
                    "title_keywords": ["监控"],
                    "primary_owner": "李娜",
                    "secondary_owners": [],
                }
            ],
        },
        "gold": {"clarify": "补齐监控看板", "assignments": []},
    },
    {
        "id": "clarify_assign_01",
        "source_text": "参会人：张伟、李娜\n认证模块这周要完成代码评审。",
        "expect": {
            "needs_clarify": True,
            "needs_assignment": True,
            "items": [
                {
                    "title_keywords": ["认证"],
                    "primary_owner": "张伟",
                    "secondary_owners": [],
                }
            ],
        },
        "gold": {
            "clarify": "发布冲刺准备",
            "assignments": [
                {
                    "title_keywords": ["认证"],
                    "primary_owner": "张伟",
                    "secondary_owners": [],
                }
            ],
        },
    },
    {
        "id": "multi_04",
        "source_text": (
            "会议目标：灰度准备\n参会人：周婷、孙磊、马超\n"
            "灰度白名单配置由孙磊负责完成，马超配合提供名单。"
        ),
        "expect": {
            "needs_clarify": False,
            "needs_assignment": False,
            "items": [
                {
                    "title_keywords": ["白名单"],
                    "primary_owner": "孙磊",
                    "secondary_owners": ["马超"],
                }
            ],
        },
        "gold": {"clarify": "", "assignments": []},
    },
]


def write_golden() -> list[Path]:
    """把 CASES 写成 golden/{id}.json。手改 JSON 后不要无脑重跑覆盖。"""
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for case in CASES:
        path = GOLDEN_DIR / f"{case['id']}.json"
        path.write_text(json.dumps(case, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        written.append(path)
    return written


if __name__ == "__main__":
    files = write_golden()
    print(f"wrote {len(files)} cases to {GOLDEN_DIR}")
