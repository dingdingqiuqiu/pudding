#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
简历改造脚本：把「布丁智服」写成第二个项目经历
==============================================

输入：仓库根目录下的 张丁-简历-后端10.17.pptx（单页 A4）
输出：布丁智服/简历/张丁-简历-布丁智服版.pptx

做的事：
1. 把 5 个「组合」（组合 4/7/16/20/27）摊平成绝对坐标形状，方便后续排版；
2. 在「项目经历」正文框里追加「布丁智服」条目，格式与「听风生活志」完全一致
   （12pt 标题行 + 项目描述 + 技术架构 + 主要工作 + 项目符号列表）；
3. 调整行距 / 字号，让两个项目 + 专业技能 + 证书奖项仍然装在一页 A4 内。

排版位置由 tools/resume_layout.ps1 用 PowerPoint 实测文本高度后写回。
"""
from __future__ import annotations

import copy
import os
import re
import shutil
import sys
import zipfile
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC_PPTX = os.path.join(ROOT, "张丁-简历-后端10.17.pptx")
OUT_DIR = os.path.join(ROOT, "布丁智服", "简历")
OUT_PPTX = os.path.join(OUT_DIR, "张丁-简历-布丁智服版.pptx")
BAK_PPTX = os.path.join(OUT_DIR, "张丁-简历-后端10.17（改造前）.pptx")

NS = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
}
A = "{%s}" % NS["a"]
P = "{%s}" % NS["p"]

# ── 排版参数（可调） ──────────────────────────────────────────────────────────
BODY_FONT_SZ = 900        # 项目正文/技能正文 字号（百分之一磅）
LN_SPC = 120000           # 正文行距百分比
TITLE_LN_SPC = 150000     # 项目标题行行距（与「听风生活志」保持一致）
TITLE_GAP = 1200          # 新项目标题前的段前距（百分之一磅，12pt）

# 顺手修掉原文里的两处错别字（只改错别字，不改简历内容）
TEXT_FIXES = [
    ("网络与信息安全术", "网络与信息安全"),
    ("熟悉计算网络", "熟悉计算机网络"),
]

BODY_ID = "23"            # 项目经历正文
SKILLS_ID = "47"          # 专业技能正文
AWARDS_ID = "27"          # 证书奖项正文
LINK_TPL_ID = "14"        # 「听风生活志」的项目地址链接形状（作为模板克隆）

NEW_PROJECT = {
    "name": "布丁智服",
    "link": "https://github.com/dingdingqiuqiu/pudding",
    "role": "后端开发",
    "desc": "项目描述：布丁智服是面向电商客服的多 Agent 智能服务台，提供订单售后、故障排障和转人工自助服务。",
    "stack": "技术架构：Java 21 + Spring Boot 3.5 + Spring AI + LangChain4j + Redis + ChromaDB 等",
    "bullets": [
        "意图识别：融合 LLM Few-shot、字符 n-gram 相似度和关键词规则三路投票，输出意图、置信度、实体与紧急度；",
        "多 Agent 编排：按意图和实体给主辅 Agent 打分路由，复合问题并行处理后合并，返回路由原因与置信度；",
        "知识增强：文档切片后走 BM25 + 向量 + LLM 重排混合召回，按意图触发检索，减少无效检索和幻觉；",
        "动态 Skills：从 SKILL.md 热加载业务规范注入提示词，隔离越权话术，支持运行时热更新；",
        "分层记忆：Redis 存会话工作记忆，JSON 与向量库存摘要和用户画像，向量操作走线程池避免阻塞主链路；",
        "工具治理：检索工具加查询改写、TTL 缓存、超时、熔断与兜底，失败自动降级不影响最终回复；",
        "可观测与评测：Prometheus 采集 Agent 成功率与延迟并回写路由降权，LLM-as-Judge 四维打分做回归检测；",
        "一键部署：Docker Compose 编排 Redis、ChromaDB 和 Nginx，Vue3 调试台对接 Swagger 在线联调。",
    ],
    # 与「听风生活志」对齐：项目名后 10 个空格，项目地址后 82 个空格把角色顶到右边
    "pad_after_name": " " * 10,
    "pad_after_link": " " * 72,
    "link_left_pt": 185.0,   # 链接文本框左边距（pt），由排版脚本写入
}


# ── 命名空间保持：把原文档里所有 xmlns 前缀注册进 ElementTree ──────────────────
def register_namespaces(xml_bytes: bytes) -> None:
    head = xml_bytes[:4000].decode("utf-8", "ignore")
    for prefix, uri in re.findall(r'xmlns:([A-Za-z0-9]+)="([^"]+)"', head):
        ET.register_namespace(prefix, uri)


def find_shape(spTree, shape_id: str):
    for sp in spTree:
        cNvPr = sp.find("./*/" + P + "cNvPr")
        if cNvPr is not None and cNvPr.get("id") == shape_id:
            return sp
    raise SystemExit(f"找不到形状 id={shape_id}")


def flatten_groups(spTree) -> None:
    """把 p:grpSp 摊平成绝对坐标的 p:sp / p:cxnSp，插回原位置。"""
    for group in [g for g in spTree if g.tag == P + "grpSp"]:
        xfrm = group.find(P + "grpSpPr/" + A + "xfrm")
        off = xfrm.find(A + "off")
        ext = xfrm.find(A + "ext")
        chOff = xfrm.find(A + "chOff")
        chExt = xfrm.find(A + "chExt")
        ox, oy = int(off.get("x")), int(off.get("y"))
        cx, cy = int(ext.get("cx")), int(ext.get("cy"))
        chx, chy = int(chOff.get("x")), int(chOff.get("y"))
        chcx, chcy = int(chExt.get("cx")), int(chExt.get("cy"))
        sx = cx / chcx if chcx else 1.0
        sy = cy / chcy if chcy else 1.0

        children = [c for c in group if c.tag in (P + "sp", P + "cxnSp", P + "pic")]
        idx = list(spTree).index(group)
        spTree.remove(group)
        for offset, child in enumerate(children):
            child_xfrm = child.find(
                {P + "sp": P + "spPr/" + A + "xfrm",
                 P + "cxnSp": P + "spPr/" + A + "xfrm",
                 P + "pic": P + "spPr/" + A + "xfrm"}[child.tag])
            c_off = child_xfrm.find(A + "off")
            c_ext = child_xfrm.find(A + "ext")
            nx = int(round(ox + (int(c_off.get("x")) - chx) * sx))
            ny = int(round(oy + (int(c_off.get("y")) - chy) * sy))
            c_off.set("x", str(nx))
            c_off.set("y", str(ny))
            if c_ext is not None:
                c_ext.set("cx", str(int(round(int(c_ext.get("cx")) * sx))))
                c_ext.set("cy", str(int(round(int(c_ext.get("cy")) * sy))))
            spTree.insert(idx + offset, child)


def set_ln_spc_single(para, ln_spc: int) -> None:
    pPr = para.find(A + "pPr")
    if pPr is None:
        pPr = ET.Element(A + "pPr")
        para.insert(0, pPr)
    ln = pPr.find(A + "lnSpc")
    if ln is None:
        ln = ET.Element(A + "lnSpc")
        pPr.insert(0, ln)
    pct = ln.find(A + "spcPct")
    if pct is None:
        pct = ET.SubElement(ln, A + "spcPct")
    pct.set("val", str(ln_spc))


def mk_run(text: str, rpr):
    """用给定的 a:rPr 模板造一个 run。"""
    r = ET.Element(A + "r")
    r.append(copy.deepcopy(rpr))
    t = ET.SubElement(r, A + "t")
    t.text = text
    return r


def build_paragraph(kind: str, text: str, template_rpr, ln_spc: int, bullets: bool):
    para = ET.Element(A + "p")
    pPr = ET.SubElement(para, A + "pPr")
    ln = ET.SubElement(pPr, A + "lnSpc")
    ET.SubElement(ln, A + "spcPct", {"val": str(ln_spc)})
    if bullets:
        pPr.set("marL", "171450")
        pPr.set("indent", "-171450")
        ET.SubElement(pPr, A + "buFont", {
            "typeface": "Arial", "panose": "020B0604020202020204",
            "pitchFamily": "34", "charset": "0"})
        ET.SubElement(pPr, A + "buChar", {"char": "•"})

    rPr = copy.deepcopy(template_rpr)
    rPr.set("sz", str(BODY_FONT_SZ))
    if kind == "body":
        rPr.set("b", "1")
    else:
        rPr.attrib.pop("b", None)
    para.append(mk_run(text, rPr))
    return para


def main() -> int:
    if not os.path.exists(SRC_PPTX):
        print(f"找不到原始简历：{SRC_PPTX}", file=sys.stderr)
        return 1
    os.makedirs(OUT_DIR, exist_ok=True)
    shutil.copy2(SRC_PPTX, BAK_PPTX)

    with zipfile.ZipFile(SRC_PPTX) as zin:
        slide_name = "ppt/slides/slide1.xml"
        slide_bytes = zin.read(slide_name)
        register_namespaces(slide_bytes)
        root = ET.fromstring(slide_bytes)
        spTree = root.find(f"./{P}cSld/{P}spTree")
        flatten_groups(spTree)

        # 1. 行距统一到 LN_SPC、字号统一到 BODY_FONT_SZ
        #    项目经历的第一段是「听风生活志」的项目标题行，保持 12pt / 150% 不动
        for sid in (BODY_ID, SKILLS_ID, AWARDS_ID):
            sp = find_shape(spTree, sid)
            txBody = sp.find(P + "txBody")
            for index, para in enumerate(txBody.findall(A + "p")):
                keep = (sid == BODY_ID and index == 0)
                if not keep:
                    set_ln_spc_single(para, LN_SPC)
                for rPr in para.iter(A + "rPr"):
                    if rPr.get("sz") and not keep:
                        rPr.set("sz", str(BODY_FONT_SZ))

        # 2. 追加「布丁智服」项目
        sp = find_shape(spTree, BODY_ID)
        txBody = sp.find(P + "txBody")
        paras = txBody.findall(A + "p")
        title_rPr = copy.deepcopy(paras[0].find(A + "r/" + A + "rPr"))
        body_rPr = copy.deepcopy(paras[1].find(A + "r/" + A + "rPr"))
        bullet_rPr = copy.deepcopy(paras[4].find(A + "r/" + A + "rPr"))
        body_rPr.set("b", "1")
        bullet_rPr.attrib.pop("b", None)

        # 标题行：项目名 + 项目地址 + 角色（URL 用独立浮动文本框，和听风一致）
        title_para = ET.Element(A + "p")
        pPr = ET.SubElement(title_para, A + "pPr")
        spc_bef = ET.SubElement(pPr, A + "spcBef")
        ET.SubElement(spc_bef, A + "spcPts", {"val": str(TITLE_GAP)})
        ln = ET.SubElement(pPr, A + "lnSpc")
        ET.SubElement(ln, A + "spcPct", {"val": str(TITLE_LN_SPC)})
        name_rPr = copy.deepcopy(title_rPr)
        name_rPr.set("sz", "1200")
        title_para.append(mk_run(NEW_PROJECT["name"], name_rPr))
        title_para.append(mk_run(NEW_PROJECT["pad_after_name"] + "项目地址：", name_rPr))
        title_para.append(mk_run(NEW_PROJECT["pad_after_link"] + NEW_PROJECT["role"], name_rPr))

        new_paras = [title_para]
        new_paras.append(build_paragraph("body", NEW_PROJECT["desc"], body_rPr, LN_SPC, False))
        new_paras.append(build_paragraph("body", NEW_PROJECT["stack"], body_rPr, LN_SPC, False))
        new_paras.append(build_paragraph("body", "主要工作：", body_rPr, LN_SPC, False))
        for item in NEW_PROJECT["bullets"]:
            new_paras.append(build_paragraph("bullet", item, bullet_rPr, LN_SPC, True))

        end = txBody.find(A + "endParaRPr")
        insert_at = list(txBody).index(end) if end is not None else len(txBody)
        for i, para in enumerate(new_paras):
            txBody.insert(insert_at + i, para)

        # 3. 克隆「听风生活志」的链接文本框，放布丁智服的项目地址
        link_tpl = find_shape(spTree, LINK_TPL_ID)
        link = copy.deepcopy(link_tpl)
        cNvPr = link.find("./*/" + P + "cNvPr")
        cNvPr.set("id", "91")
        cNvPr.set("name", "文本框 91")
        link_body = link.find(P + "txBody")
        for extra in link_body.findall(A + "p")[1:]:
            link_body.remove(extra)
        para = link_body.find(A + "p")
        for r in para.findall(A + "r")[1:]:
            para.remove(r)
        para.find(A + "r/" + A + "t").text = NEW_PROJECT["link"]
        xfrm = link.find(P + "spPr/" + A + "xfrm")
        xfrm.find(A + "off").set("x", str(int(NEW_PROJECT["link_left_pt"] * 12700)))
        xfrm.find(A + "off").set("y", "0")
        xfrm.find(A + "ext").set("cx", str(int(300 * 12700)))
        spTree.append(link)

        # 4. 修错别字
        for t in root.iter(A + "t"):
            if not t.text:
                continue
            for old, new in TEXT_FIXES:
                if old in t.text:
                    t.text = t.text.replace(old, new)

        new_slide = ET.tostring(root, encoding="UTF-8", xml_declaration=True)

        with zipfile.ZipFile(OUT_PPTX, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                data = new_slide if item.filename == slide_name else zin.read(item.filename)
                zout.writestr(item, data)

    print(f"已生成：{OUT_PPTX}")
    print(f"原始备份：{BAK_PPTX}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
